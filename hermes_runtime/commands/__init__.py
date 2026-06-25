"""Whitelisted platform commands for the Hermes runtime."""

from __future__ import annotations

from importlib import import_module
from typing import Any

COMMAND_MODULES = {
    "ping": "hermes_runtime.commands.ping",
    "whoami": "hermes_runtime.commands.whoami",
    "check_update": "hermes_runtime.commands.check_update",
    "stage_update": "hermes_runtime.commands.stage_update",
    "activate_update": "hermes_runtime.commands.activate_update",
}


async def run_command(ctx: Any, command: dict[str, Any]) -> dict[str, Any]:
    kind = str(command.get("kind") or "")
    module_name = COMMAND_MODULES.get(kind)
    if module_name is None:
        raise ValueError(f"unsupported command: {kind}")
    module = import_module(module_name)
    return await module.run(ctx, command)
