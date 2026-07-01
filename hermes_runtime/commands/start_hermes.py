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

from pathlib import Path
from typing import Any

from hermes_runtime.commands.configure_telegram import (
    _compact_hermes_status,
    _compact_process,
    _env_file_candidates,
    _gateway_log_has_adapter_failure,
    _gateway_needs_foreground_run,
    _gateway_status_is_healthy,
    _start_gateway_foreground,
)
from hermes_runtime.hermes_cli import (
    find_hermes_binary,
    probe_hermes_status,
    run_process,
)
from hermes_runtime.runtime_env import load_env_files_into_process
from hermes_runtime.terminal_env_hook import install_terminal_env_reload_hook


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

    start = await run_process(
        [str(hermes_bin), "gateway", "start"],
        timeout_seconds=180,
    )
    status_after = await run_process(
        [str(hermes_bin), "gateway", "status"],
        timeout_seconds=45,
    )
    foreground: dict[str, Any] | None = None
    if _gateway_needs_foreground_run(start=start, status=status_after):
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
        "start": _compact_process(start),
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

    terminal_env_hook = install_terminal_env_reload_hook()
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
        "terminal_env_hook": terminal_env_hook,
        "env_reload": env_reload,
        "gateway": gateway,
        "hermes": _compact_hermes_status(hermes_status),
        "message": (
            "Hermes gateway was already running."
            if gateway.get("already_running")
            else "Hermes gateway start requested."
        ),
    }
