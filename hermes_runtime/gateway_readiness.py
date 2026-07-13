"""Functional readiness probing for the Hermes Telegram gateway.

"Functionally ready" means two things at once:

1. ``hermes gateway status`` reports healthy (the same
   ``_gateway_status_is_healthy`` check the rest of the runtime uses), and
2. there is affirmative Telegram evidence tied to that gateway process. The
   preferred source is Hermes' atomic ``gateway_state.json`` record: its PID
   must match the exact systemd or runtime-owned foreground generation, the
   gateway lifecycle must be running, and Telegram's freshly-written platform
   state must be connected. A Tinyhat-owned foreground generation may also
   use fresh markers in the runtime-managed foreground log, but only after
   its persisted process identity matches the exact live gateway generation.

Telegram evidence collection is best-effort, but affirmative evidence is
required for functional readiness. It is reported by level:

- ``"log"`` — the foreground/fallback gateway log file this runtime manages
  (``<state dir>/hermes-gateway.log``) received new bytes after the restart
  began, and only those appended bytes are scanned for the marker. The public
  Hermes CLI exposes no ``gateway log`` / log-path facility, and in service
  mode the gateway logs to the systemd journal, so this level only fires for
  the runtime's own foreground gateway. A stale foreground log that never
  grows carries no signal and does not count as this level.
- ``"runtime_state"`` — Hermes' atomic ``gateway_state.json`` record matches
  the new service MainPID or exact non-systemd PID/start-time/argv generation
  and reports a fresh, connected Telegram platform.
- ``"journal"`` — ``journalctl --user -u hermes-gateway.service`` output for
  the exact new ``_SYSTEMD_INVOCATION_ID`` scanned for the marker (service
  mode).
- ``"unavailable"`` — neither source is usable. Callers can distinguish that
  weaker status-only result from a fresh source that is available but has not
  emitted a Telegram-ready marker yet.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from hermes_runtime.commands.configure_telegram import (
    _compact_process,
    _gateway_status_is_healthy,
)
from hermes_runtime.hermes_cli import run_process
from hermes_runtime.openrouter_stt import hermes_python
from hermes_runtime.runtime_env import hermes_home

GATEWAY_SERVICE_NAME = "hermes-gateway.service"
STATUS_PROBE_TIMEOUT_SECONDS = 15
JOURNAL_PROBE_TIMEOUT_SECONDS = 15
# Positive markers for an established Telegram connection.  A readable source
# with fresh output but no marker returns False so a restart transaction can
# keep polling.  None is reserved for a source that cannot provide evidence.
TELEGRAM_CONNECTED_MARKERS = (
    "connected to telegram",
    "[telegram] connected",
)
_LOG_SCAN_MAX_BYTES = 262_144
PROCESS_IDENTITY_PROBE_TIMEOUT_SECONDS = 2.0
_HERMES_PROCESS_START_SCRIPT = """
import sys
from gateway.status import get_process_start_time

value = get_process_start_time(int(sys.argv[1]))
if value is None:
    raise SystemExit(3)
print(value)
"""


def _gateway_argv_tail(argv: Any) -> list[str] | None:
    """Return a profile-preserving gateway runtime argv identity.

    This mirrors Hermes' own gateway command recognition: profile selectors
    may appear anywhere (and may themselves name a profile ``gateway``), the
    bare ``hermes gateway`` command defaults to ``run``, and dedicated
    ``hermes-gateway`` / ``gateway/run.py`` entrypoints host the runtime without
    an explicit subcommand. Management-only subcommands still fail closed.
    """
    if not isinstance(argv, list) or not argv:
        return None
    values = [str(part) for part in argv]
    recognized = [part.strip("\"'").replace("\\", "/").lower() for part in values]

    selectors: list[str] = []
    filtered_values: list[str] = []
    filtered_recognized: list[str] = []
    cursor = 0
    while cursor < len(values):
        current = values[cursor]
        current_recognized = recognized[cursor]
        if current_recognized in {"-p", "--profile"}:
            if cursor + 1 >= len(values) or not values[cursor + 1]:
                return None
            selectors.extend(["--profile", values[cursor + 1]])
            cursor += 2
            continue
        if current_recognized.startswith(("--profile=", "-p=")):
            value = current.split("=", 1)[1]
            if not value:
                return None
            selectors.extend(["--profile", value])
            cursor += 1
            continue
        if current_recognized == "hermes_home":
            if cursor + 1 >= len(values) or not values[cursor + 1]:
                return None
            selectors.extend(["HERMES_HOME", values[cursor + 1]])
            cursor += 2
            continue
        if current_recognized.startswith("hermes_home="):
            value = current.split("=", 1)[1]
            if not value:
                return None
            selectors.extend(["HERMES_HOME", value])
            cursor += 1
            continue
        filtered_values.append(current)
        filtered_recognized.append(current_recognized)
        cursor += 1

    for index, token in enumerate(filtered_recognized):
        if (
            token == "gateway/run.py"
            or token.endswith("/gateway/run.py")
            or token.rsplit("/", 1)[-1]
            in {"hermes-gateway", "hermes-gateway.exe"}
        ):
            return [
                *selectors,
                "gateway",
                "run",
                *filtered_values[index + 1 :],
            ]

    joined = " ".join(filtered_recognized)
    has_gateway_entry = (
        "hermes_cli.main" in joined
        or "hermes_cli/main.py" in joined
        or any(
            token.rsplit("/", 1)[-1] in {"hermes", "hermes.exe"}
            for token in filtered_recognized
        )
    )
    if not has_gateway_entry:
        return None

    for index, token in enumerate(filtered_recognized):
        if token != "gateway":
            continue
        subcommand = (
            filtered_recognized[index + 1]
            if index + 1 < len(filtered_recognized)
            else "run"
        )
        if subcommand not in {"run", "restart"}:
            return None
        return [
            *selectors,
            "gateway",
            subcommand,
            *filtered_values[index + 2 :],
        ]
    return None


def _gateway_command_kind(argv: Any) -> str | None:
    if not isinstance(argv, list) or not argv:
        return None
    values = [str(part).strip("\"'").casefold() for part in argv]
    for index, value in enumerate(values[:-1]):
        if value == "gateway" and values[index + 1] in {"run", "restart"}:
            return f"gateway_{values[index + 1]}"
    return None


def public_gateway_runtime_generation(
    generation: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Project exact local identity without exposing arbitrary argv values."""
    if not isinstance(generation, dict):
        return None
    command_kind = _gateway_command_kind(generation.get("argv"))
    if not command_kind:
        return None
    return {
        "pid": generation.get("pid"),
        "process_start_time": generation.get("start_time"),
        "started_at_unix": generation.get("started_at_unix"),
        "command_kind": command_kind,
        "identity_verified": True,
    }


def public_gateway_foreground_generation(
    generation: dict[str, Any] | None,
    *,
    matches_runtime: bool,
) -> dict[str, Any] | None:
    """Return non-sensitive Tinyhat foreground-generation metadata."""
    if not isinstance(generation, dict):
        return None
    command_kind = _gateway_command_kind(generation.get("argv"))
    if not command_kind:
        return None
    return {
        "pid": generation.get("pid"),
        "process_start_time": generation.get("process_start_time"),
        "started_at_unix": generation.get("started_at_unix"),
        "command_kind": command_kind,
        "identity_verified": True,
        "matches_runtime": matches_runtime,
    }


def public_gateway_runtime_generation_same(
    live: dict[str, Any] | None,
    public_or_live: dict[str, Any] | None,
) -> bool:
    """Compare live identity with either current public or legacy raw state."""
    if not isinstance(live, dict) or not isinstance(public_or_live, dict):
        return False
    live_kind = _gateway_command_kind(live.get("argv")) or str(
        live.get("command_kind") or ""
    )
    other_kind = _gateway_command_kind(public_or_live.get("argv")) or str(
        public_or_live.get("command_kind") or ""
    )
    live_start = live.get("start_time") or live.get("process_start_time")
    other_start = public_or_live.get("start_time") or public_or_live.get(
        "process_start_time"
    )
    return bool(
        live.get("pid")
        and live_start
        and live.get("pid") == public_or_live.get("pid")
        and live_start == other_start
        and live_kind
        and live_kind == other_kind
        and public_or_live.get("identity_verified", True) is True
    )


def _gateway_argv_belongs_to_home(argv: list[str], expected_home: Path) -> bool:
    """Whether one normalized gateway argv belongs to ``expected_home``.

    A named Hermes profile lives at ``<root>/profiles/<name>`` and must
    advertise that profile with ``-p``/``--profile`` or an explicit matching
    ``HERMES_HOME``. The default/root profile must not advertise any profile
    flag and rejects a conflicting explicit home. Multiple or malformed
    selectors fail closed instead of letting a later flag hide a conflict.
    """
    profiles: list[str] = []
    homes: list[str] = []
    cursor = 0
    while cursor < len(argv):
        current = argv[cursor]
        if current in {"-p", "--profile"}:
            if cursor + 1 >= len(argv) or not argv[cursor + 1]:
                return False
            profiles.append(argv[cursor + 1])
            cursor += 2
            continue
        if current.startswith("--profile="):
            value = current.split("=", 1)[1]
            if not value:
                return False
            profiles.append(value)
        elif current == "HERMES_HOME":
            if cursor + 1 >= len(argv) or not argv[cursor + 1]:
                return False
            homes.append(argv[cursor + 1])
            cursor += 2
            continue
        elif current.startswith("HERMES_HOME="):
            value = current.split("=", 1)[1]
            if not value:
                return False
            homes.append(value)
        cursor += 1

    # Home paths are process-ownership evidence. Preserve the host platform's
    # path semantics: POSIX paths are case-sensitive, while Windows' normcase
    # applies its documented case normalization. Do not casefold POSIX paths;
    # that could authorize a different profile for the pidfd kill gate.
    expected_home_text = os.path.normcase(
        os.path.abspath(os.path.expanduser(str(expected_home)))
    )
    homes_match = all(
        os.path.normcase(os.path.abspath(os.path.expanduser(value)))
        == expected_home_text
        for value in homes
    )
    if not homes_match:
        return False

    profile_name = (
        expected_home.name
        if expected_home.parent.name == "profiles"
        and expected_home.name != "default"
        else None
    )
    if profile_name is None:
        return not profiles

    profiles_match = all(
        value.casefold() == profile_name.casefold() for value in profiles
    )
    return profiles_match and bool(profiles or homes)


def _read_proc_start_time(pid: int) -> int | None:
    """Return Linux ``/proc`` field-22 process-start ticks."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return None
    end = raw.rfind(")")
    if end < 0:
        return None
    fields_after_comm = raw[end + 1 :].split()
    try:
        start_time = int(fields_after_comm[19])
    except (IndexError, TypeError, ValueError):
        return None
    return start_time if start_time > 0 else None


def _identity_probe_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    env.update({"LC_ALL": "C", "LANG": "C"})
    return env


def _read_ps_process_argv(pid: int) -> list[str] | None:
    ps = shutil.which("ps")
    if not ps:
        return None
    try:
        result = subprocess.run(
            [ps, "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=PROCESS_IDENTITY_PROBE_TIMEOUT_SECONDS,
            check=False,
            env=_identity_probe_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    command = str(result.stdout or "").strip()
    if result.returncode != 0 or not command:
        return None
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return None
    return argv or None


def _hermes_process_start_time(pid: int) -> int | None:
    """Ask Hermes' own environment for its cross-platform fingerprint."""
    python_bin = hermes_python()
    if not python_bin.is_file():
        return None
    try:
        result = subprocess.run(
            [str(python_bin), "-c", _HERMES_PROCESS_START_SCRIPT, str(pid)],
            capture_output=True,
            text=True,
            timeout=PROCESS_IDENTITY_PROBE_TIMEOUT_SECONDS,
            check=False,
            env=_identity_probe_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        start_time = int(str(result.stdout or "").strip())
    except (TypeError, ValueError):
        return None
    return start_time if start_time > 0 else None


def _live_process_identity(pid: int) -> tuple[int, list[str], float] | None:
    """Return live start fingerprint, argv, and Unix start time.

    Linux uses the same ``/proc`` field-22 fingerprint Hermes records. On
    other hosts, Hermes' own venv computes the public cross-platform
    fingerprint; Tinyhat does not import or own Hermes' psutil dependency.
    """
    if pid <= 0:
        return None
    from hermes_runtime.commands.stop_hermes import _read_proc_cmdline

    proc_start_ticks = _read_proc_start_time(pid)
    proc_argv = _read_proc_cmdline(pid)
    if proc_start_ticks is not None:
        live_argv = proc_argv or _read_ps_process_argv(pid)
        if not live_argv:
            return None
        try:
            uptime_seconds = float(
                Path("/proc/uptime").read_text(encoding="utf-8").split()[0]
            )
            ticks_per_second = int(os.sysconf("SC_CLK_TCK"))
            started_at_unix = (
                time.time()
                - uptime_seconds
                + (proc_start_ticks / ticks_per_second)
            )
        except (OSError, ValueError, IndexError):
            return None
        return proc_start_ticks, live_argv, started_at_unix

    # Hermes' Linux value is /proc clock ticks, not epoch centiseconds. If
    # /proc is unexpectedly unreadable there, do not reinterpret the helper's
    # Linux units as a Unix timestamp. The helper fallback is for the upstream
    # non-/proc contract (psutil creation-time centiseconds) only.
    if sys.platform.startswith("linux"):
        return None
    start_time = _hermes_process_start_time(pid)
    live_argv = proc_argv or _read_ps_process_argv(pid)
    if start_time is None or not live_argv:
        return None
    return start_time, live_argv, start_time / 100.0


def read_gateway_runtime_generation(
    path: Path | None = None,
) -> dict[str, Any] | None:
    """Return one live, profile-bound Hermes gateway-state generation.

    Hermes' atomic state includes ``kind``, ``pid``, ``start_time`` and
    ``argv``. The same PID must still have the recorded process-start
    fingerprint and an identical ``gateway run|restart`` argv tail (including
    profile flags). Stale state, PID reuse, another profile, and malformed
    records all fail closed.
    """
    state_path = path or (hermes_home() / "gateway_state.json")
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("kind") != "hermes-gateway":
        return None
    try:
        pid = int(payload.get("pid") or 0)
        recorded_start = int(payload.get("start_time"))
    except (TypeError, ValueError):
        return None
    recorded_argv = _gateway_argv_tail(payload.get("argv"))
    live = _live_process_identity(pid)
    if recorded_argv is None or live is None:
        return None
    if not _gateway_argv_belongs_to_home(recorded_argv, state_path.parent):
        return None
    live_start, live_argv, started_at_unix = live
    if live_start != recorded_start:
        return None
    if _gateway_argv_tail(live_argv) != recorded_argv:
        return None
    return {
        "pid": pid,
        "start_time": recorded_start,
        "argv": recorded_argv,
        "started_at_unix": started_at_unix,
    }


def gateway_runtime_generation_same(
    first: dict[str, Any] | None,
    second: dict[str, Any] | None,
) -> bool:
    """Whether two observations identify the same gateway process."""
    if not isinstance(first, dict) or not isinstance(second, dict):
        return False
    if (
        not first.get("pid")
        or not first.get("start_time")
        or not isinstance(first.get("argv"), list)
        or not first.get("argv")
    ):
        return False
    return all(
        first.get(field) == second.get(field)
        for field in ("pid", "start_time", "argv")
    )


def gateway_runtime_generation_active(
    generation: dict[str, Any] | None,
    *,
    expected_home: Path | None = None,
) -> bool | None:
    """Whether the exact runtime generation still owns its PID.

    ``False`` proves that the PID no longer exists or has been reused by a
    different process generation. ``None`` means identity could not be read
    safely, so mutating callers must fail closed rather than treating an
    unreadable process as gone.
    """
    if not isinstance(generation, dict):
        return None
    try:
        pid = int(generation.get("pid") or 0)
        expected_start = int(generation.get("start_time"))
    except (TypeError, ValueError):
        return None
    expected_argv = generation.get("argv")
    if (
        pid <= 0
        or not isinstance(expected_argv, list)
        or not expected_argv
        or not _gateway_argv_belongs_to_home(
            [str(part) for part in expected_argv],
            expected_home or hermes_home(),
        )
    ):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return None

    live = _live_process_identity(pid)
    if live is None:
        return None
    live_start, live_argv, _started_at_unix = live
    return bool(
        live_start == expected_start
        and _gateway_argv_tail(live_argv) == expected_argv
    )


def _parse_iso_timestamp(value: Any) -> float | None:
    """Parse Hermes' UTC ISO timestamps without leaking their raw values."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def _runtime_state_telegram_evidence(
    path: Path | None,
    *,
    service_main_pid: int | None,
    since_unix: float,
    expected_start_time: int | None = None,
    expected_argv: list[str] | None = None,
) -> bool | None:
    """Return invocation-scoped Telegram state; ``None`` means unusable.

    Hermes writes this record atomically from inside the gateway process. A
    PID match binds it to the same MainPID systemd reported for the new
    generation. Requiring both the gateway and Telegram platform timestamps to
    be fresh prevents a new process from inheriting an old ``connected`` entry
    during the first read/merge/write at startup.
    """
    if path is None or not service_main_pid or service_main_pid <= 0:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        recorded_pid = int(payload.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    if recorded_pid != service_main_pid:
        return None
    if expected_start_time is not None:
        try:
            recorded_start_time = int(payload.get("start_time"))
        except (TypeError, ValueError):
            return None
        if (
            payload.get("kind") != "hermes-gateway"
            or recorded_start_time != expected_start_time
            or _gateway_argv_tail(payload.get("argv")) != expected_argv
        ):
            return None

    gateway_updated_at = _parse_iso_timestamp(payload.get("updated_at"))
    platforms = payload.get("platforms")
    telegram = platforms.get("telegram") if isinstance(platforms, dict) else None
    if not isinstance(telegram, dict):
        return False
    telegram_updated_at = _parse_iso_timestamp(telegram.get("updated_at"))
    # Heal supplies its restart time and heartbeat inspection converts the
    # current service's monotonic start time into this wall-clock domain. That
    # makes an inherited platform row fail closed even after the new PID has
    # been written into the shared record.
    if since_unix > 0 and (
        gateway_updated_at is None
        or telegram_updated_at is None
        or gateway_updated_at < since_unix
        or telegram_updated_at < since_unix
    ):
        return False

    gateway_state = str(payload.get("gateway_state") or "").strip().lower()
    telegram_state = str(
        telegram.get("state") or telegram.get("status") or ""
    ).strip().lower()
    return gateway_state == "running" and telegram_state == "connected"


def gateway_status_reports_telegram_fatal(result: dict[str, Any] | None) -> bool:
    """Whether Hermes' official status reports a fatal Telegram adapter."""
    if not isinstance(result, dict):
        return False
    text = f"{result.get('stdout') or ''}\n{result.get('stderr') or ''}".lower()
    return any(
        line.strip().startswith(("⚠ telegram:", "warning: telegram:"))
        for line in text.splitlines()
    )


def gateway_log_size(path: Path | None) -> int:
    """Return the gateway log's current size (0 when missing/unreadable).

    Callers snapshot this before a restart so a later probe only scans bytes
    the gateway appended after the restart began.
    """
    if path is None:
        return 0
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _log_telegram_evidence(path: Path | None, offset: int) -> bool | None:
    """Marker present in bytes appended after ``offset``; None = unusable."""
    if path is None:
        return None
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size <= offset:
        # No new content since the restart began. In service mode this file
        # is never written, so it carries no signal either way.
        return None
    start = max(offset, size - _LOG_SCAN_MAX_BYTES)
    try:
        with path.open("rb") as handle:
            handle.seek(start)
            appended = handle.read(_LOG_SCAN_MAX_BYTES)
    except OSError:
        return None
    text = appended.decode("utf-8", errors="replace").lower()
    return bool(any(m in text for m in TELEGRAM_CONNECTED_MARKERS))


async def _journal_telegram_evidence(
    since_unix: float,
    *,
    service_manager: str = "user",
    service_invocation_id: str | None = None,
    timeout_seconds: float = JOURNAL_PROBE_TIMEOUT_SECONDS,
) -> bool | None:
    """Marker from one exact service invocation; None = unusable."""
    journalctl = shutil.which("journalctl")
    invocation_id = str(service_invocation_id or "").strip()
    if not journalctl or not invocation_id:
        return None
    command = [journalctl]
    if service_manager == "user":
        command.append("--user")
    command.extend(
        [
            "-u",
            GATEWAY_SERVICE_NAME,
            f"_SYSTEMD_INVOCATION_ID={invocation_id}",
            f"--since=@{int(since_unix)}",
            "--no-pager",
            "--output=cat",
        ]
    )
    result = await run_process(
        command,
        timeout_seconds=timeout_seconds,
    )
    if not result.get("ok"):
        return None
    text = str(result.get("stdout") or "").lower()
    return bool(any(m in text for m in TELEGRAM_CONNECTED_MARKERS))


async def probe_functional_readiness(
    hermes_bin: Path,
    *,
    since_unix: float,
    log_path: Path | None = None,
    log_offset: int = 0,
    service_manager: str = "user",
    service_invocation_id: str | None = None,
    service_main_pid: int | None = None,
    expected_process_start_time: int | None = None,
    expected_gateway_argv: list[str] | None = None,
    runtime_state_path: Path | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """One functional-readiness probe (status + best-effort Telegram evidence).

    Returns ``ready`` true only when both the status probe and affirmative
    Telegram evidence are present.  A missing evidence source is unknown, not
    success.
    """
    probe_budget = max(
        0.2,
        float(
            timeout_seconds
            if timeout_seconds is not None
            else STATUS_PROBE_TIMEOUT_SECONDS + JOURNAL_PROBE_TIMEOUT_SECONDS
        ),
    )
    step_timeout = max(0.1, probe_budget / 2)
    status = await run_process(
        [str(hermes_bin), "gateway", "status"],
        timeout_seconds=min(STATUS_PROBE_TIMEOUT_SECONDS, step_timeout),
    )
    telegram_fatal = gateway_status_reports_telegram_fatal(status)
    status_healthy = _gateway_status_is_healthy(status) and not telegram_fatal

    telegram_evidence = "unavailable"
    telegram_connected: bool | None = None
    invocation_id = str(service_invocation_id or "").strip()
    # A service restart must be tied to the new systemd invocation. An
    # unfiltered foreground log can still receive bytes from the old process
    # during shutdown, so it is not valid evidence for a systemd generation.
    log_result = (
        None if invocation_id else _log_telegram_evidence(log_path, log_offset)
    )
    state_result = (
        _runtime_state_telegram_evidence(
            runtime_state_path or (hermes_home() / "gateway_state.json"),
            service_main_pid=service_main_pid,
            since_unix=since_unix,
            expected_start_time=expected_process_start_time,
            expected_argv=expected_gateway_argv,
        )
        if service_main_pid
        else None
    )
    if state_result is not None:
        telegram_evidence, telegram_connected = "runtime_state", state_result
    elif invocation_id:
        journal_result = await _journal_telegram_evidence(
            since_unix,
            service_manager=service_manager,
            service_invocation_id=invocation_id,
            timeout_seconds=min(JOURNAL_PROBE_TIMEOUT_SECONDS, step_timeout),
        )
        if journal_result is not None:
            telegram_evidence, telegram_connected = "journal", journal_result
    elif log_result is True:
        telegram_evidence, telegram_connected = "log", True
    else:
        journal_result = await _journal_telegram_evidence(
            since_unix,
            service_manager=service_manager,
            service_invocation_id=None,
            timeout_seconds=min(JOURNAL_PROBE_TIMEOUT_SECONDS, step_timeout),
        )
        if journal_result is not None:
            telegram_evidence, telegram_connected = "journal", journal_result
        elif log_result is not None:
            telegram_evidence, telegram_connected = "log", log_result

    functionally_ready = status_healthy and telegram_connected is True
    return {
        "ready": functionally_ready,
        "functionally_ready": functionally_ready,
        "status_healthy": status_healthy,
        "telegram_fatal": telegram_fatal,
        "telegram_evidence": telegram_evidence,
        "telegram_connected": telegram_connected,
        "telegram_evidence_available": telegram_connected is not None,
        "status": _compact_process(status),
    }
