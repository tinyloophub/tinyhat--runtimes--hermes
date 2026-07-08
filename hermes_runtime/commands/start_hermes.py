"""Start Hermes Agent messaging for this Computer.

What it does:
    1. Looks for the public ``hermes`` CLI.
    2. Runs ``hermes gateway status`` to see whether the gateway is already
       healthy.
    3. If the gateway is not already healthy, checks whether systemd reports
       the gateway unit failed/start-limited — probing the system manager
       first and then the user manager, detected by exit code (``systemctl
       is-failed`` exits 0 exactly when the unit is failed) — and runs
       ``reset-failed`` on the manager that reports the failure, then runs
       ``hermes gateway start``. If the start still leaves the unit failed,
       it resets and retries the start once.
    4. If Hermes says service-mode gateway start is not usable in this
       environment, starts the foreground ``hermes gateway run`` process used
       by Tinyhat's Docker/local fallback.
    5. Runs ``hermes gateway status`` again and returns a small summary.

When to use it:
    Use this after an operator intentionally stopped Hermes Agent messaging on
    an already-configured Computer and wants to start the same gateway again.
    This is not the assignment/setup command: it does not fetch bot tokens,
    write Telegram credentials, or clear Telegram webhooks.

Example input:
    {"kind": "start_hermes", "spec": {"reason": "admin_start_hermes"}}

Example output:
    {
      "schema": "tinyhat_hermes_start_v1",
      "started": true,
      "healthy": true,
      "already_running": false,
      "gateway": {"mode": "service", "healthy": true}
    }

Side effects:
    Starts Hermes Agent's messaging gateway only. It does not restart or stop
    the Tinyhat runtime service, fetch credentials from Tinyhat, change
    Telegram webhooks, remove credentials, reboot the machine, or unassign the
    Computer.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from hermes_runtime.commands.configure_telegram import (
    _compact_hermes_status,
    _compact_process,
    _env_file_candidates,
    _gateway_log_has_adapter_failure,
    _gateway_needs_foreground_run,
    _gateway_service_is_missing,
    _gateway_status_is_healthy,
    _start_gateway_foreground,
)
from hermes_runtime.gateway_readiness import GATEWAY_SERVICE_NAME
from hermes_runtime.hermes_cli import (
    find_hermes_binary,
    probe_hermes_status,
    run_process,
)
from hermes_runtime.runtime_env import load_env_files_into_process


def _user_systemd_env() -> dict[str, str] | None:
    """Bus environment for ``systemctl --user`` when running as root.

    ``hermes gateway install`` under root creates a *user* unit for uid 0;
    ``systemctl --user`` can only reach that manager when ``XDG_RUNTIME_DIR``
    points at the root user's runtime directory. Best-effort: only injected
    when we are root and no runtime dir is already set.
    """
    if getattr(os, "geteuid", lambda: -1)() != 0:
        return None
    if (os.getenv("XDG_RUNTIME_DIR") or "").strip():
        return None
    return {
        "XDG_RUNTIME_DIR": "/run/user/0",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/0/bus",
    }


def _systemctl_manager_commands(systemctl: str) -> list[dict[str, Any]]:
    """Manager candidates for the gateway unit, most reachable first.

    ``hermes gateway install`` may register the unit with the system manager
    or with a user manager depending on how Hermes was installed, so probe
    both: the system manager first (reachable without any bus environment),
    then the user manager. The user-manager calls carry the best-effort root
    bus environment from ``_user_systemd_env`` so a root-installed runtime
    can still reach uid 0's user manager.
    """
    user_env = _user_systemd_env()
    return [
        {
            "manager": "system",
            "is_failed": [systemctl, "is-failed", GATEWAY_SERVICE_NAME],
            "reset_failed": [systemctl, "reset-failed", GATEWAY_SERVICE_NAME],
            "env": None,
        },
        {
            "manager": "user",
            "is_failed": [systemctl, "--user", "is-failed", GATEWAY_SERVICE_NAME],
            "reset_failed": [
                systemctl,
                "--user",
                "reset-failed",
                GATEWAY_SERVICE_NAME,
            ],
            "env": user_env,
        },
    ]


def _service_reported_failed(result: dict[str, Any] | None) -> bool:
    """``systemctl is-failed`` exits 0 exactly when the unit is failed."""
    return isinstance(result, dict) and result.get("returncode") == 0


async def _reset_failed_gateway_service() -> dict[str, Any] | None:
    """Reset a failed/start-limited gateway unit on the manager that owns it.

    Detection is by exit code only (never by matching CLI prose). Returns a
    summary for the manager that was reset, or ``None`` when no manager
    reports the unit failed or ``systemctl`` is unavailable.
    """
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return None
    for manager in _systemctl_manager_commands(systemctl):
        is_failed = await run_process(
            manager["is_failed"],
            timeout_seconds=30,
            env=manager["env"],
        )
        if not _service_reported_failed(is_failed):
            continue
        reset = await run_process(
            manager["reset_failed"],
            timeout_seconds=30,
            env=manager["env"],
        )
        return {
            "manager": manager["manager"],
            "ok": bool(reset.get("ok")),
            "bus_env_injected": manager["env"] is not None,
            "is_failed": _compact_process(is_failed),
            "reset": _compact_process(reset),
        }
    return None


async def _start_gateway(hermes_bin: Path) -> dict[str, Any]:
    status_before = await run_process(
        [str(hermes_bin), "gateway", "status"],
        timeout_seconds=45,
    )
    if _gateway_status_is_healthy(status_before):
        return {
            "already_running": True,
            "started": True,
            "healthy": True,
            "mode": "existing",
            "adapter_ready": True,
            "status_before": _compact_process(status_before),
            "start": None,
            "foreground": None,
            "status_after": _compact_process(status_before),
        }

    reset_failed = await _reset_failed_gateway_service()

    start = await run_process(
        [str(hermes_bin), "gateway", "start"],
        timeout_seconds=180,
    )
    status_after = await run_process(
        [str(hermes_bin), "gateway", "status"],
        timeout_seconds=45,
    )
    if not _gateway_status_is_healthy(status_after) and reset_failed is None:
        reset_failed = await _reset_failed_gateway_service()
        if reset_failed is not None:
            start = await run_process(
                [str(hermes_bin), "gateway", "start"],
                timeout_seconds=180,
            )
            status_after = await run_process(
                [str(hermes_bin), "gateway", "status"],
                timeout_seconds=45,
            )
    install: dict[str, Any] | None = None
    if (
        not _gateway_status_is_healthy(status_after)
        and _gateway_service_is_missing(start, status_after)
    ):
        install = await run_process(
            [str(hermes_bin), "gateway", "install"],
            timeout_seconds=180,
        )
        if install.get("ok"):
            start = await run_process(
                [str(hermes_bin), "gateway", "start"],
                timeout_seconds=180,
            )
            status_after = await run_process(
                [str(hermes_bin), "gateway", "status"],
                timeout_seconds=45,
            )
    foreground: dict[str, Any] | None = None
    if _gateway_needs_foreground_run(
        start=start,
        status=status_after,
        install=install,
    ):
        foreground = await _start_gateway_foreground(hermes_bin)
        status_after = await run_process(
            [str(hermes_bin), "gateway", "status"],
            timeout_seconds=45,
        )
    foreground_log = (
        Path(str(foreground.get("log_path")))
        if isinstance(foreground, dict) and foreground.get("log_path")
        else None
    )
    adapter_failure = _gateway_log_has_adapter_failure(foreground_log)
    healthy = _gateway_status_is_healthy(status_after) and not adapter_failure
    return {
        "already_running": False,
        "started": bool(start.get("ok"))
        or bool(foreground and foreground.get("started")),
        "healthy": healthy,
        "mode": (
            str(foreground.get("mode"))
            if isinstance(foreground, dict) and foreground.get("mode")
            else "service"
        ),
        "adapter_ready": not adapter_failure,
        "status_before": _compact_process(status_before),
        "reset_failed": reset_failed,
        "start": _compact_process(start),
        "install": _compact_process(install),
        "foreground": foreground,
        "status_after": _compact_process(status_after),
    }


async def run(_ctx: Any, _command: dict[str, Any]) -> dict[str, Any]:
    hermes_bin = find_hermes_binary()
    if hermes_bin is None:
        return {
            "schema": "tinyhat_hermes_start_v1",
            "started": False,
            "healthy": False,
            "already_running": False,
            "hermes_installed": False,
            "hermes_bin": None,
            "gateway": None,
            "hermes": None,
            "message": "Hermes CLI was not found; install Hermes first.",
        }

    env_reload = load_env_files_into_process(_env_file_candidates())
    gateway = await _start_gateway(hermes_bin)
    hermes_status = await probe_hermes_status()
    return {
        "schema": "tinyhat_hermes_start_v1",
        "started": bool(gateway.get("started")),
        "healthy": bool(gateway.get("healthy")),
        "already_running": bool(gateway.get("already_running")),
        "hermes_installed": True,
        "hermes_bin": str(hermes_bin),
        "env_reload": env_reload,
        "gateway": gateway,
        "hermes": _compact_hermes_status(hermes_status),
        "message": (
            "Hermes gateway was already running."
            if gateway.get("already_running")
            else "Hermes gateway start requested."
        ),
    }
