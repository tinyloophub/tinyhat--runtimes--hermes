"""Start Hermes Agent messaging for this Computer.

What it does:
    1. Looks for the public ``hermes`` CLI.
    2. Runs ``hermes gateway status`` to see whether the gateway is already
       healthy.
    3. If the gateway is not already healthy, runs ``hermes gateway start``.
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
    _process_text,
    _start_gateway_foreground,
)
from hermes_runtime.hermes_cli import (
    find_hermes_binary,
    probe_hermes_status,
    run_process,
)
from hermes_runtime.gateway_desired_state import clear_desired_stopped
from hermes_runtime.runtime_env import load_env_files_into_process

GATEWAY_SERVICE_NAME = "hermes-gateway.service"


def _systemctl_manager_commands(systemctl: str) -> list[dict[str, Any]]:
    commands: list[dict[str, Any]] = [
        {
            "manager": "system",
            "is_failed": [systemctl, "is-failed", GATEWAY_SERVICE_NAME],
            "reset_failed": [systemctl, "reset-failed", GATEWAY_SERVICE_NAME],
        }
    ]
    if os.getenv("XDG_RUNTIME_DIR") or os.getenv("DBUS_SESSION_BUS_ADDRESS"):
        commands.append(
            {
                "manager": "user",
                "is_failed": [systemctl, "--user", "is-failed", GATEWAY_SERVICE_NAME],
                "reset_failed": [
                    systemctl,
                    "--user",
                    "reset-failed",
                    GATEWAY_SERVICE_NAME,
                ],
            }
        )
    return commands


def _systemctl_is_failed(result: dict[str, Any] | None) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("returncode") != 0:
        return False
    return "failed" in _process_text(result)


async def _reset_failed_gateway_service() -> dict[str, Any] | None:
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return None
    for manager in _systemctl_manager_commands(systemctl):
        is_failed = await run_process(manager["is_failed"], timeout_seconds=30)
        if not _systemctl_is_failed(is_failed):
            continue
        reset = await run_process(manager["reset_failed"], timeout_seconds=30)
        return {
            "manager": manager["manager"],
            "ok": bool(reset.get("ok")),
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
    if (
        not _gateway_status_is_healthy(status_after)
        and reset_failed is None
    ):
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
    if gateway.get("healthy"):
        state_dir = getattr(_ctx, "state_dir", None)
        if isinstance(state_dir, Path):
            clear_desired_stopped(state_dir)
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
