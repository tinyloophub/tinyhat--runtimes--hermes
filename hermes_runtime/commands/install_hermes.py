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
    the Telegram gateway adapter dependencies. If not, it installs Hermes'
    official ``messaging`` extra into the same Hermes project venv. This keeps
    Tinyhat Computers warm: the later agent-assignment step only writes the bot
    settings and starts the gateway.

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
    ``python3-pip``, and ``xz-utils`` when running as root on Debian/Ubuntu.
    Runs the public Hermes installer if Hermes is missing. May install Hermes'
    ``messaging`` extra into the Hermes venv. Does not configure Tinyhat
    platform state.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from hermes_runtime.commands.configure_telegram import (
    _install_codex_auth_plugin_commands,
    _install_codex_auth_quick_commands,
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
                "missing=[name for name in ('telegram','telegram.ext') "
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
    package_spec = f"{project_dir}[messaging]"
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

    status = await probe_hermes_status()
    if not status.get("installed"):
        raise RuntimeError("Hermes installer completed, but hermes CLI was not found.")
    if not status.get("ok"):
        raise RuntimeError("Hermes CLI is installed, but status checks failed.")

    messaging = await _ensure_messaging_dependencies()
    if not messaging.get("ok"):
        raise RuntimeError("Hermes messaging dependencies are not available.")
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
        "codex_auth": codex_auth,
        "status": status,
    }
