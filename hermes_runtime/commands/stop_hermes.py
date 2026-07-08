"""Stop Hermes Agent messaging for this Computer.

What it does:
    1. Looks for the public ``hermes`` CLI.
    2. If the CLI exists, runs ``hermes gateway status`` so the operator can
       see the gateway state before shutdown.
    3. Runs ``hermes gateway stop`` through the public Hermes CLI.
    4. On Linux, looks for the foreground ``hermes gateway run`` process used
       by Tinyhat's Docker/local fallback and terminates it.
    5. Runs ``hermes gateway status`` again and returns a small summary.

When to use it:
    Tinyhat queues this before parking an agent that was attached to a Hermes
    Computer. Stopping Hermes first prevents the old long-polling gateway from
    stealing Telegram updates or clearing the parked webhook after the platform
    points the bot back to ``/tinyhat/webhooks/parked``.

Example input:
    {"kind": "stop_hermes", "spec": {"reason": "admin_reset_to_parked"}}

Example output:
    {
      "schema": "tinyhat_hermes_stop_v1",
      "stopped": true,
      "hermes_installed": true,
      "gateway_stop": {"ok": true},
      "terminated_processes": [{"pid": 123, "terminated": true}]
    }

Side effects:
    Stops Hermes Agent's messaging gateway only. It does not restart or stop
    the Tinyhat runtime service, reboot the machine, remove credentials, change
    Telegram webhooks, or unassign the Computer.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from hermes_runtime.hermes_cli import find_hermes_binary, run_process


def _compact_process(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    return {
        "ok": bool(result.get("ok")),
        "returncode": result.get("returncode"),
        "timed_out": bool(result.get("timed_out")),
        "duration_ms": result.get("duration_ms"),
        "stdout": str(result.get("stdout") or "")[:2000],
        "stderr": str(result.get("stderr") or "")[:2000],
        "stdout_truncated": bool(result.get("stdout_truncated")),
        "stderr_truncated": bool(result.get("stderr_truncated")),
    }


def _process_text(result: dict[str, Any] | None) -> str:
    if not isinstance(result, dict):
        return ""
    return f"{result.get('stdout') or ''}\n{result.get('stderr') or ''}".lower()


def _gateway_status_is_stopped(status: dict[str, Any] | None) -> bool:
    text = _process_text(status)
    if not text:
        return False
    stopped_needles = (
        "not running",
        "gateway is not running",
        "no gateway process",
        "gateway stopped",
        "status: stopped",
        "state: stopped",
    )
    return any(needle in text for needle in stopped_needles)


def _read_proc_cmdline(pid: int) -> list[str] | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    parts = [part.decode("utf-8", errors="replace") for part in raw.split(b"\0")]
    return [part for part in parts if part]


def _is_gateway_process(args: list[str], hermes_bin: Path | None) -> bool:
    if not args:
        return False
    lowered = [arg.lower() for arg in args]
    if "gateway" not in lowered or "run" not in lowered:
        return False

    first_name = Path(args[0]).name.lower()
    if first_name == "hermes":
        return True
    if hermes_bin is not None:
        with suppress(OSError):
            if Path(args[0]).resolve() == hermes_bin.resolve():
                return True
    return "hermes" in " ".join(lowered)


def _list_gateway_processes(hermes_bin: Path | None) -> list[dict[str, Any]]:
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return []
    current_pid = os.getpid()
    matches: list[dict[str, Any]] = []
    for child in proc_root.iterdir():
        if not child.name.isdigit():
            continue
        pid = int(child.name)
        if pid == current_pid:
            continue
        args = _read_proc_cmdline(pid)
        if args is None or not _is_gateway_process(args, hermes_bin):
            continue
        matches.append(
            {
                "pid": pid,
                "cmdline": args[:20],
            }
        )
    return matches


def _process_alive(pid: int) -> bool:
    with suppress(ProcessLookupError):
        os.kill(pid, 0)
        return True
    return False


def _send_signal(pid: int, sig: signal.Signals) -> str:
    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = None
    if pgid is not None:
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, sig)
            return "process_group"
    os.kill(pid, sig)
    return "process"


def _terminate_process(process: dict[str, Any]) -> dict[str, Any]:
    pid = int(process["pid"])
    result = dict(process)
    result["terminated"] = False
    result["kill_sent"] = False
    result["signal_target"] = None
    if not _process_alive(pid):
        result["terminated"] = True
        result["already_exited"] = True
        return result

    try:
        result["signal_target"] = _send_signal(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError) as exc:
        result["error"] = str(exc)
        return result

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            result["terminated"] = True
            return result
        time.sleep(0.1)

    with suppress(ProcessLookupError, PermissionError):
        _send_signal(pid, signal.SIGKILL)
        result["kill_sent"] = True

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            result["terminated"] = True
            return result
        time.sleep(0.1)

    result["still_running"] = _process_alive(pid)
    return result


def _terminate_gateway_processes(hermes_bin: Path | None) -> list[dict[str, Any]]:
    return [
        _terminate_process(process)
        for process in _list_gateway_processes(hermes_bin)
    ]


async def run(_ctx: Any, _command: dict[str, Any]) -> dict[str, Any]:
    hermes_bin = find_hermes_binary()
    status_before: dict[str, Any] | None = None
    stop: dict[str, Any] | None = None
    status_after: dict[str, Any] | None = None

    if hermes_bin is not None:
        status_before = await run_process(
            [str(hermes_bin), "gateway", "status"],
            timeout_seconds=45,
        )
        stop = await run_process(
            [str(hermes_bin), "gateway", "stop"],
            timeout_seconds=60,
        )

    terminated = await asyncio.to_thread(_terminate_gateway_processes, hermes_bin)

    if hermes_bin is not None:
        status_after = await run_process(
            [str(hermes_bin), "gateway", "status"],
            timeout_seconds=45,
        )

    foreground_clear = all(bool(item.get("terminated")) for item in terminated)
    gateway_stopped = (
        hermes_bin is None
        or bool(stop and stop.get("ok"))
        or _gateway_status_is_stopped(status_after)
        or bool(terminated)
    )
    stopped = foreground_clear and gateway_stopped
    return {
        "schema": "tinyhat_hermes_stop_v1",
        "stopped": bool(stopped),
        "hermes_installed": hermes_bin is not None,
        "hermes_bin": str(hermes_bin) if hermes_bin is not None else None,
        "gateway_status_before": _compact_process(status_before),
        "gateway_stop": _compact_process(stop),
        "gateway_status_after": _compact_process(status_after),
        "terminated_processes": terminated,
        "message": (
            "Hermes gateway stop requested."
            if hermes_bin is not None
            else "Hermes CLI was not found; checked for foreground gateway processes."
        ),
    }
