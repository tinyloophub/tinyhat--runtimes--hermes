"""Whitelisted platform commands for the Hermes runtime."""

from __future__ import annotations

from importlib import import_module
from typing import Any

COMMAND_MODULES = {
    "ping": "hermes_runtime.commands.ping",
    "whoami": "hermes_runtime.commands.whoami",
    "check_update": "hermes_runtime.commands.check_update",
    "update_status": "hermes_runtime.commands.update_status",
    "running_version": "hermes_runtime.commands.running_version",
    "recent_commands": "hermes_runtime.commands.recent_commands",
    "setup_snapshot": "hermes_runtime.commands.setup_snapshot",
    "install_hermes": "hermes_runtime.commands.install_hermes",
    "hermes_status": "hermes_runtime.commands.hermes_status",
    "configure_telegram": "hermes_runtime.commands.configure_telegram",
    "start_hermes": "hermes_runtime.commands.start_hermes",
    "stop_hermes": "hermes_runtime.commands.stop_hermes",
    "stage_update": "hermes_runtime.commands.stage_update",
    "activate_update": "hermes_runtime.commands.activate_update",
    "restart_runtime_service": "hermes_runtime.commands.restart_runtime_service",
}


async def run_command(ctx: Any, command: dict[str, Any]) -> dict[str, Any]:
    kind = str(command.get("kind") or "")
    module_name = COMMAND_MODULES.get(kind)
    if module_name is None:
        raise ValueError(f"unsupported command: {kind}")
    module = import_module(module_name)
    return await module.run(ctx, command)
