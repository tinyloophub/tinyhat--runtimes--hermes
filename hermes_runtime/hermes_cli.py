"""Small helpers for calling the public Hermes Agent command line."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import os
import shlex
import shutil
from pathlib import Path
from typing import Any

HERMES_INSTALL_URL = "https://hermes-agent.nousresearch.com/install.sh"
DEFAULT_HERMES_INSTALL_ARGS = ("--skip-browser",)
MAX_OUTPUT_CHARS = 12_000
DEBIAN_PREREQUISITE_COMMANDS: dict[str, str] = {
    "curl": "curl",
    "git": "git",
    "xz": "xz-utils",
    "pip": "python3-pip",
    "g++": "build-essential",
    "ffmpeg": "ffmpeg",
    "rg": "ripgrep",
    "xclip": "xclip",
    "wl-paste": "wl-clipboard",
}


def find_hermes_binary() -> Path | None:
    """Return the Hermes CLI path if it is visible to this process."""
    explicit = (os.getenv("HERMES_BIN") or "").strip()
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    discovered = shutil.which("hermes")
    if discovered:
        candidates.append(discovered)
    home = Path.home()
    candidates.extend(
        [
            str(home / ".local" / "bin" / "hermes"),
            # Best-effort fallback for the current upstream installer layout.
            # The documented interface remains the global ``hermes`` command.
            str(home / ".hermes" / "hermes-agent" / "venv" / "bin" / "hermes"),
            "/usr/local/bin/hermes",
        ]
    )
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


def root_user_manager_env() -> dict[str, str]:
    """Bus environment so a root *system* process can reach uid 0's *user*
    systemd manager.

    ``hermes gateway install`` under root registers the gateway as a **user**
    unit for uid 0, and the Hermes CLI drives it with ``systemctl --user``.
    The Tinyhat runtime, however, runs as a root **system** service with no
    ``XDG_RUNTIME_DIR``/``DBUS_SESSION_BUS_ADDRESS``, so those user-manager
    calls silently fail and ``hermes gateway start`` falls back to an
    unmanaged foreground gateway that never picks up freshly-saved env. Inject
    the bus vars for every subprocess (best-effort, only when we are root and
    no runtime dir is already set) so the CLI's internal ``systemctl --user``
    reaches the running user manager. Verified live: a platform-queued secret
    restart on a GCE Hermes Computer left the gateway foreground-degraded
    without these vars.
    """
    if getattr(os, "geteuid", lambda: -1)() != 0:
        return {}
    if (os.getenv("XDG_RUNTIME_DIR") or "").strip():
        return {}
    return {
        "XDG_RUNTIME_DIR": "/run/user/0",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/0/bus",
    }


async def run_process(
    args: list[str],
    *,
    timeout_seconds: int,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    started = asyncio.get_running_loop().time()
    merged_env = os.environ.copy()
    merged_env.update(root_user_manager_env())
    if env:
        merged_env.update(env)
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout_seconds,
        )
        returncode = process.returncode
        timed_out = False
    except asyncio.TimeoutError:
        with suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        returncode = None
        stdout = b""
        stderr = f"command timed out after {timeout_seconds}s".encode()
        timed_out = True
    duration_ms = int((asyncio.get_running_loop().time() - started) * 1000)
    stdout_text = stdout.decode("utf-8", errors="replace")
    stderr_text = stderr.decode("utf-8", errors="replace")
    return {
        "args": args,
        "returncode": returncode,
        "ok": returncode == 0,
        "timed_out": timed_out,
        "duration_ms": duration_ms,
        "stdout": stdout_text[:MAX_OUTPUT_CHARS],
        "stderr": stderr_text[:MAX_OUTPUT_CHARS],
        "stdout_truncated": len(stdout_text) > MAX_OUTPUT_CHARS,
        "stderr_truncated": len(stderr_text) > MAX_OUTPUT_CHARS,
    }


async def run_shell(
    script: str,
    *,
    timeout_seconds: int,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    return await run_process(
        ["bash", "-lc", script],
        timeout_seconds=timeout_seconds,
        env=env,
    )


def hermes_install_args() -> list[str]:
    raw = os.getenv("TINYHAT_HERMES_INSTALL_ARGS")
    if raw is None:
        return list(DEFAULT_HERMES_INSTALL_ARGS)
    return shlex.split(raw)


def hermes_install_script() -> str:
    args = " ".join(shlex.quote(arg) for arg in hermes_install_args())
    suffix = f" -s -- {args}" if args else ""
    return f"curl -fsSL {shlex.quote(HERMES_INSTALL_URL)} | bash{suffix}"


async def maybe_install_debian_prerequisites() -> dict[str, Any]:
    required = list(DEBIAN_PREREQUISITE_COMMANDS)
    missing = [
        name
        for name in required
        if (
            shutil.which(name) is None
            and not (name == "pip" and shutil.which("pip3") is not None)
        )
    ]
    result: dict[str, Any] = {
        "required": required,
        "missing_before": missing,
        "attempted": False,
        "result": None,
    }
    if not missing:
        return result
    if os.name != "posix" or getattr(os, "geteuid", lambda: -1)() != 0:
        return result
    if shutil.which("apt-get") is None:
        return result
    packages = ["ca-certificates"]
    for command in missing:
        package = DEBIAN_PREREQUISITE_COMMANDS[command]
        if package not in packages:
            packages.append(package)
    install = await run_shell(
        "export DEBIAN_FRONTEND=noninteractive\n"
        "apt-get update\n"
        "apt-get install -y --no-install-recommends "
        + " ".join(shlex.quote(package) for package in packages),
        timeout_seconds=240,
    )
    result["attempted"] = True
    result["result"] = install
    result["missing_after"] = [
        name
        for name in required
        if (
            shutil.which(name) is None
            and not (name == "pip" and shutil.which("pip3") is not None)
        )
    ]
    return result


def _first_stdout_line(result: dict[str, Any] | None) -> str | None:
    if not isinstance(result, dict):
        return None
    stdout = str(result.get("stdout") or "").strip()
    if not stdout:
        return None
    return stdout.splitlines()[0].strip() or None


async def probe_hermes_status(
    *,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    hermes_bin = find_hermes_binary()
    if hermes_bin is None:
        return {
            "schema": "tinyhat_hermes_status_v1",
            "installed": False,
            "ok": False,
            "hermes_bin": None,
            "version": None,
            "commands": {},
            "message": "Hermes CLI was not found in PATH or known install locations.",
        }

    version = await run_process(
        [str(hermes_bin), "--version"],
        timeout_seconds=timeout_seconds,
    )
    status = await run_process(
        [str(hermes_bin), "status"],
        timeout_seconds=timeout_seconds,
    )
    status_all = await run_process(
        [str(hermes_bin), "status", "--all"],
        timeout_seconds=max(timeout_seconds, 45),
    )
    commands = {
        "version": version,
        "status": status,
        "status_all": status_all,
    }
    ok = all(bool(item.get("ok")) for item in commands.values())
    return {
        "schema": "tinyhat_hermes_status_v1",
        "installed": True,
        "ok": ok,
        "hermes_bin": str(hermes_bin),
        "version": _first_stdout_line(version),
        "commands": commands,
        "message": (
            "Hermes CLI responded to --version, status, and status --all."
            if ok
            else "Hermes CLI is installed but at least one status command failed."
        ),
    }
