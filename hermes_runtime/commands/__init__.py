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
    "multimodal_status": "hermes_runtime.commands.multimodal_status",
    "tinyhat_plugin_status": "hermes_runtime.commands.tinyhat_plugin_status",
    "check_tinyhat_plugin_update": "hermes_runtime.commands.check_tinyhat_plugin_update",
    "install_tinyhat_plugin": "hermes_runtime.commands.install_tinyhat_plugin",
    "update_tinyhat_plugin": "hermes_runtime.commands.update_tinyhat_plugin",
    "configure_telegram": "hermes_runtime.commands.configure_telegram",
    "apply_config": "hermes_runtime.commands.apply_config",
    "import_openclaw_state": "hermes_runtime.commands.import_openclaw_state",
    "activate_codex_auth_models": "hermes_runtime.commands.activate_codex_auth_models",
    "import_legacy_tinyhat_secrets": "hermes_runtime.commands.import_legacy_tinyhat_secrets",
    "start_hermes": "hermes_runtime.commands.start_hermes",
    "stop_hermes": "hermes_runtime.commands.stop_hermes",
    "heal_hermes": "hermes_runtime.commands.heal_hermes",
    "codex_limits": "hermes_runtime.commands.codex_limits",
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
