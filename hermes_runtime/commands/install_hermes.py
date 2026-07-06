"""Install Hermes Agent using the official installer when it is missing.

What it does:
    Checks whether the ``hermes`` CLI is already installed. If it is present,
    the command returns the current Hermes status and does not reinstall. If it
    is missing, the command installs the small Debian prerequisites when it can
    do so safely as root, then runs the official Hermes Agent installer:

        curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash

    By default Tinyhat passes ``--skip-browser`` to keep the first managed
    Computer setup minimal. Set ``TINYHAT_HERMES_INSTALL_ARGS`` on the machine
    to override those installer arguments.

    After Hermes is present, the command verifies the Hermes venv can import
    the Telegram gateway adapter and voice-transcription dependencies. If not,
    it installs Hermes' official ``messaging`` and ``voice`` extras into the same
    Hermes project venv. This keeps Tinyhat Computers warm: the later
    agent-assignment step only writes the bot settings and starts the gateway.
    It also warms faster-whisper's selected local STT model cache so a Computer
    still has an on-box multilingual model ready if an operator switches Hermes
    to the local STT provider.

    The command also preinstalls Tinyhat's OpenAI Codex auth quick commands and
    matching Hermes plugin slash-command registrations in ``~/.hermes``. They
    are inert until Telegram is connected, but this keeps the later assignment
    path short and lets Hermes add the Codex commands to Telegram's menu.

When to use it:
    Hat admin queues this automatically during Computer creation after the
    Tinyhat runtime has started heartbeating. You can also run it manually if a
    machine was created before Hermes was installed.

Example input:
    {"kind": "install_hermes", "spec": {}}

Example output:
    {
      "installed_before": false,
      "installed_now": true,
      "installed_after": true,
      "changed": true,
      "status": {"ok": true, "version": "Hermes Agent 0.1.0"}
    }

    ``installed_now`` means the installer ran during this command. If Hermes
    was already present, ``installed_now`` is false, ``installed_after`` is
    true, and ``changed`` is false.

Side effects:
    May install Debian packages ``ca-certificates``, ``curl``, ``git``, and
    ``python3-pip``, ``xz-utils``, ``build-essential``, ``ffmpeg``,
    ``ripgrep``, ``xclip``, and ``wl-clipboard`` when running as root on
    Debian/Ubuntu.
    Runs the public Hermes installer if Hermes is missing. May install Hermes'
    ``messaging``/``voice`` extras into the Hermes venv and download the selected
    local STT model weights. Prefetch failures are reported but do not
    fail provisioning because OpenRouter is the active day-one STT provider.
    Does not configure Tinyhat platform state.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from hermes_runtime.commands.configure_telegram import (
    _configure_day_one_multimedia,
    _install_codex_auth_plugin_commands,
    _install_codex_auth_quick_commands,
    local_stt_model,
)
from hermes_runtime.hermes_cli import (
    find_hermes_binary,
    hermes_install_script,
    maybe_install_debian_prerequisites,
    probe_hermes_status,
    run_process,
    run_shell,
)


def _hermes_project_candidates() -> list[Path]:
    candidates: list[Path] = []
    explicit = (os.getenv("HERMES_PROJECT_DIR") or "").strip()
    if explicit:
        candidates.append(Path(explicit))
    candidates.extend(
        [
            Path("/usr/local/lib/hermes-agent"),
            Path.home() / ".hermes" / "hermes-agent",
        ]
    )
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.expanduser())
        if key not in seen:
            unique.append(candidate.expanduser())
            seen.add(key)
    return unique


def _find_hermes_project_dir() -> Path | None:
    for candidate in _hermes_project_candidates():
        if (candidate / "pyproject.toml").is_file() and (
            candidate / "venv" / "bin" / "python"
        ).is_file():
            return candidate
    return None


async def _probe_messaging_dependencies(project_dir: Path) -> dict[str, Any]:
    python_bin = project_dir / "venv" / "bin" / "python"
    probe = await run_process(
        [
            str(python_bin),
            "-c",
            (
                "import importlib.util\n"
                "missing=[name for name in ('telegram','telegram.ext','faster_whisper') "
                "if importlib.util.find_spec(name) is None]\n"
                "print('ok' if not missing else 'missing:' + ','.join(missing))\n"
                "raise SystemExit(0 if not missing else 1)\n"
            ),
        ],
        timeout_seconds=30,
    )
    return {
        "ok": bool(probe.get("ok")),
        "project_dir": str(project_dir),
        "python": str(python_bin),
        "probe": probe,
    }


def _pip_command_for_python(python_bin: Path) -> str:
    if (python_bin.parent / "pip").is_file():
        return f"{shlex.quote(str(python_bin))} -m pip"

    pip_bin = shutil.which("pip") or shutil.which("pip3")
    # ``pip --python`` can install into a venv that does not have pip
    # bootstrapped yet, but older distro pips do not support the flag. Prefer
    # the Hermes venv's own pip when present, then fall back only when the
    # system pip advertises the option.
    if pip_bin and _pip_supports_python_option(pip_bin):
        return (
            f"{shlex.quote(pip_bin)} --python {shlex.quote(str(python_bin))}"
        )
    return f"{shlex.quote(str(python_bin))} -m pip"


def _pip_supports_python_option(pip_bin: str) -> bool:
    try:
        result = subprocess.run(
            [pip_bin, "--help"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "--python" in f"{result.stdout}\n{result.stderr}"


async def _ensure_messaging_dependencies() -> dict[str, Any]:
    project_dir = _find_hermes_project_dir()
    if project_dir is None:
        return {
            "ok": False,
            "changed": False,
            "message": "Hermes project venv was not found.",
        }

    before = await _probe_messaging_dependencies(project_dir)
    if before.get("ok"):
        return {
            "ok": True,
            "changed": False,
            "project_dir": str(project_dir),
            "before": before,
            "after": before,
            "install": None,
        }

    prerequisites: dict[str, Any] | None = None
    if shutil.which("pip") is None and shutil.which("pip3") is None:
        prerequisites = await maybe_install_debian_prerequisites()

    python_bin = project_dir / "venv" / "bin" / "python"
    package_spec = f"{project_dir}[messaging,voice]"
    install = await run_shell(
        (
            f"cd {shlex.quote(str(project_dir))}\n"
            f"{_pip_command_for_python(python_bin)} install -e "
            f"{shlex.quote(package_spec)}"
        ),
        timeout_seconds=900,
        env={"PIP_DISABLE_PIP_VERSION_CHECK": "1"},
    )
    after = await _probe_messaging_dependencies(project_dir)
    return {
        "ok": bool(after.get("ok")),
        "changed": bool(install.get("ok")) and bool(after.get("ok")),
        "project_dir": str(project_dir),
        "before": before,
        "after": after,
        "install": install,
        "prerequisites": prerequisites,
    }


def _skip_local_stt_model_prefetch() -> bool:
    value = (os.getenv("TINYHAT_SKIP_LOCAL_STT_MODEL_PREFETCH") or "").strip().lower()
    return value in {
        "1",
        "true",
        "yes",
        "on",
    }


def _status_probe_attempts() -> int:
    raw = (os.getenv("TINYHAT_HERMES_STATUS_PROBE_ATTEMPTS") or "").strip()
    if not raw:
        return 5
    try:
        attempts = int(raw)
    except ValueError:
        return 5
    return max(1, min(attempts, 10))


def _status_probe_timeout_seconds() -> int:
    raw = (os.getenv("TINYHAT_HERMES_STATUS_PROBE_TIMEOUT_SECONDS") or "").strip()
    if not raw:
        return 90
    try:
        timeout = int(raw)
    except ValueError:
        return 90
    return max(30, min(timeout, 300))


def _status_probe_total_timeout_seconds() -> int:
    raw = (os.getenv("TINYHAT_HERMES_STATUS_PROBE_TOTAL_TIMEOUT_SECONDS") or "").strip()
    if not raw:
        return 300
    try:
        timeout = int(raw)
    except ValueError:
        return 300
    return max(60, min(timeout, 900))


def _status_probe_retry_delay_seconds(attempt: int) -> int:
    raw = (os.getenv("TINYHAT_HERMES_STATUS_PROBE_RETRY_DELAY_SECONDS") or "").strip()
    if not raw:
        base = 5
    else:
        try:
            base = int(raw)
        except ValueError:
            base = 5
    return max(1, min(base * attempt, 30))


def _failed_status_command_summary(status: dict[str, Any]) -> str:
    commands = status.get("commands")
    if not isinstance(commands, dict):
        return str(status.get("message") or "status probe failed")

    failures: list[str] = []
    for name, result in commands.items():
        if not isinstance(result, dict) or result.get("ok"):
            continue
        detail = str(result.get("stderr") or result.get("stdout") or "").strip()
        if detail:
            detail = detail.splitlines()[0][:240]
        elif result.get("timed_out"):
            detail = "timed out"
        else:
            detail = f"returncode={result.get('returncode')}"
        failures.append(f"{name}: {detail}")
    if failures:
        return "; ".join(failures)
    return str(status.get("message") or "status probe failed")


def _status_probe_timed_out(status: dict[str, Any]) -> bool:
    commands = status.get("commands")
    if not isinstance(commands, dict):
        return False
    return any(
        isinstance(result, dict) and bool(result.get("timed_out"))
        for result in commands.values()
    )


async def _probe_hermes_status_with_retries() -> dict[str, Any]:
    attempts = _status_probe_attempts()
    timeout_seconds = _status_probe_timeout_seconds()
    total_timeout_seconds = _status_probe_total_timeout_seconds()
    loop = asyncio.get_running_loop()
    started_at = loop.time()
    probe_attempts: list[dict[str, Any]] = []
    status: dict[str, Any] = {}
    for attempt in range(1, attempts + 1):
        status = await probe_hermes_status(timeout_seconds=timeout_seconds)
        probe_attempts.append(
            {
                "attempt": attempt,
                "ok": bool(status.get("ok")),
                "installed": bool(status.get("installed")),
                "message": status.get("message"),
                "failure_summary": (
                    None if status.get("ok") else _failed_status_command_summary(status)
                ),
            }
        )
        if status.get("installed") and status.get("ok"):
            break
        if _status_probe_timed_out(status):
            status["probe_stopped_reason"] = "command_timeout"
            break
        elapsed_seconds = loop.time() - started_at
        if elapsed_seconds >= total_timeout_seconds:
            status["probe_stopped_reason"] = "total_timeout"
            break
        if attempt < attempts:
            delay_seconds = _status_probe_retry_delay_seconds(attempt)
            if elapsed_seconds + delay_seconds >= total_timeout_seconds:
                status["probe_stopped_reason"] = "total_timeout"
                break
            await asyncio.sleep(delay_seconds)
    status["probe_attempts"] = probe_attempts
    status["probe_attempt_count"] = len(probe_attempts)
    status["probe_timeout_seconds"] = timeout_seconds
    status["probe_total_timeout_seconds"] = total_timeout_seconds
    return status


def _local_stt_model_prefetch_timeout_seconds() -> int:
    raw = (os.getenv("TINYHAT_HERMES_STT_MODEL_PREFETCH_TIMEOUT_SECONDS") or "").strip()
    if not raw:
        return 900
    try:
        timeout = int(raw)
    except ValueError:
        return 900
    return max(60, timeout)


async def _prefetch_local_stt_model() -> dict[str, Any]:
    """Warm faster-whisper's model cache during provisioning.

    Hermes downloads local STT model weights on first use. Doing that while a
    user waits on their first Telegram voice note makes voice look broken, so
    Tinyhat warms the selected local model during ``install_hermes`` instead.
    """
    if _skip_local_stt_model_prefetch():
        return {
            "ok": True,
            "changed": False,
            "skipped": True,
            "skip_env": "TINYHAT_SKIP_LOCAL_STT_MODEL_PREFETCH",
            "model": local_stt_model(),
        }

    project_dir = _find_hermes_project_dir()
    if project_dir is None:
        return {
            "ok": False,
            "changed": False,
            "skipped": False,
            "message": "Hermes project venv was not found.",
            "model": local_stt_model(),
        }

    python_bin = project_dir / "venv" / "bin" / "python"
    model = local_stt_model()
    result = await run_process(
        [
            str(python_bin),
            "-c",
            (
                "import os\n"
                "from faster_whisper import WhisperModel\n"
                "model = os.environ['TINYHAT_LOCAL_STT_MODEL']\n"
                "WhisperModel(model, device='cpu', compute_type='int8')\n"
                "print('cached:' + model)\n"
            ),
        ],
        timeout_seconds=_local_stt_model_prefetch_timeout_seconds(),
        env={
            "TINYHAT_LOCAL_STT_MODEL": model,
            "HF_HUB_DISABLE_TELEMETRY": "1",
        },
    )
    return {
        "ok": bool(result.get("ok")),
        "changed": bool(result.get("ok")),
        "skipped": False,
        "model": model,
        "project_dir": str(project_dir),
        "python": str(python_bin),
        "result": result,
    }


async def run(_ctx: Any, _command: dict[str, Any]) -> dict[str, Any]:
    installed_before = find_hermes_binary() is not None
    prerequisites: dict[str, Any] | None = None
    install_result: dict[str, Any] | None = None

    if not installed_before:
        prerequisites = await maybe_install_debian_prerequisites()
        install_result = await run_shell(
            hermes_install_script(),
            timeout_seconds=900,
            env={"CI": "1"},
        )
        if not install_result.get("ok"):
            raise RuntimeError(
                "Hermes installer failed with returncode="
                f"{install_result.get('returncode')}"
            )

    status = await _probe_hermes_status_with_retries()
    if not status.get("installed"):
        raise RuntimeError("Hermes installer completed, but hermes CLI was not found.")
    if not status.get("ok"):
        attempts = status.get("probe_attempt_count") or _status_probe_attempts()
        raise RuntimeError(
            "Hermes CLI is installed, but status checks failed after "
            f"{attempts} attempt(s): {_failed_status_command_summary(status)}"
        )

    hermes_bin_value = status.get("hermes_bin")
    hermes_bin = (
        Path(str(hermes_bin_value))
        if hermes_bin_value
        else find_hermes_binary()
    )
    if hermes_bin is None:
        raise RuntimeError("Hermes CLI is installed, but hermes binary was not found.")

    messaging = await _ensure_messaging_dependencies()
    if not messaging.get("ok"):
        raise RuntimeError("Hermes messaging dependencies are not available.")
    multimodal_defaults = await _configure_day_one_multimedia(hermes_bin)
    local_stt_model_prefetch = await _prefetch_local_stt_model()
    local_stt_model_prefetch_warning = None
    if not local_stt_model_prefetch.get("ok"):
        local_stt_model_prefetch_warning = (
            "Hermes local STT model prefetch failed; provisioning "
            "continues because OpenRouter STT is the active provider."
        )
    codex_auth = {
        "quick_commands": _install_codex_auth_quick_commands(),
        "plugin_commands": _install_codex_auth_plugin_commands(),
    }

    installed_after = bool(status.get("installed"))
    installed_by_command = not installed_before

    return {
        "schema": "tinyhat_hermes_install_v1",
        "installed_before": installed_before,
        "installed_now": installed_by_command,
        "installed_after": installed_after,
        "already_installed": installed_before,
        "changed": installed_by_command,
        "install_url": "https://hermes-agent.nousresearch.com/install.sh",
        "install_args_source": "TINYHAT_HERMES_INSTALL_ARGS",
        "prerequisites": prerequisites,
        "install": install_result,
        "messaging": messaging,
        "multimodal_defaults": multimodal_defaults,
        "local_stt_model_prefetch": local_stt_model_prefetch,
        "local_stt_model_prefetch_warning": local_stt_model_prefetch_warning,
        "codex_auth": codex_auth,
        "status": status,
    }
