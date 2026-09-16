"""Whitelisted platform commands for the Hermes runtime."""

from __future__ import annotations

from importlib import import_module
from typing import Any

COMMAND_MODULES = {
    "ping": "hermes_runtime.commands.ping",
    "whoami": "hermes_runtime.commands.whoami",
    "check_update": "hermes_runtime.commands.check_update",
    "check_and_stage_updates": "hermes_runtime.commands.check_and_stage_updates",
    "update_status": "hermes_runtime.commands.update_status",
    "running_version": "hermes_runtime.commands.running_version",
    "recent_commands": "hermes_runtime.commands.recent_commands",
    "setup_snapshot": "hermes_runtime.commands.setup_snapshot",
    "install_hermes": "hermes_runtime.commands.install_hermes",
    "install_desktop_apps": "hermes_runtime.commands.install_desktop_apps",
    "hermes_status": "hermes_runtime.commands.hermes_status",
    "multimodal_status": "hermes_runtime.commands.multimodal_status",
    "tinyhat_plugin_status": "hermes_runtime.commands.tinyhat_plugin_status",
    "check_tinyhat_plugin_update": "hermes_runtime.commands.check_tinyhat_plugin_update",
    "install_tinyhat_plugin": "hermes_runtime.commands.install_tinyhat_plugin",
    "update_tinyhat_plugin": "hermes_runtime.commands.update_tinyhat_plugin",
    "configure_telegram": "hermes_runtime.commands.configure_telegram",
    "configure_channels": "hermes_runtime.commands.configure_channels",
    "onboarding_greeting": "hermes_runtime.commands.onboarding_greeting",
    "apply_config": "hermes_runtime.commands.apply_config",
    "enroll_private_access": "hermes_runtime.commands.enroll_private_access",
    "import_openclaw_state": "hermes_runtime.commands.import_openclaw_state",
    "activate_codex_auth_models": "hermes_runtime.commands.activate_codex_auth_models",
    "import_legacy_tinyhat_secrets": "hermes_runtime.commands.import_legacy_tinyhat_secrets",
    "remove_private_secret": "hermes_runtime.commands.remove_private_secret",
    "configure_hat_credentials": "hermes_runtime.commands.configure_hat_credentials",
    "resume_hat_installation": (
        "hermes_runtime.commands.resume_hat_installation"
    ),
    "complete_hat_credential_transfer": (
        "hermes_runtime.commands.complete_hat_credential_transfer"
    ),
    "start_hermes": "hermes_runtime.commands.start_hermes",
    "stop_hermes": "hermes_runtime.commands.stop_hermes",
    "heal_hermes": "hermes_runtime.commands.heal_hermes",
    "codex_limits": "hermes_runtime.commands.codex_limits",
    "stage_update": "hermes_runtime.commands.stage_update",
    "activate_update": "hermes_runtime.commands.activate_update",
    "restart_runtime_service": "hermes_runtime.commands.restart_runtime_service",
    "channels_status": "hermes_runtime.commands.channel_agent",
    "channels_use_hermes": "hermes_runtime.commands.channel_agent",
    "channels_use_codex": "hermes_runtime.commands.channel_agent",
    "channels_use_claude_code": "hermes_runtime.commands.channel_agent",
    "install_agent_framework": "hermes_runtime.commands.channel_agent",
}


async def run_command(ctx: Any, command: dict[str, Any]) -> dict[str, Any]:
    kind = str(command.get("kind") or "")
    module_name = COMMAND_MODULES.get(kind)
    if module_name is None:
        raise ValueError(f"unsupported command: {kind}")
    from hermes_runtime.channel_agent.control import selected, snapshot, suspend, mode
    if hasattr(ctx, "state_dir") and kind == "stop_hermes":
        await suspend(ctx)
    if hasattr(ctx, "state_dir") and kind == "start_hermes" and selected(ctx) != "hermes":
        from hermes_runtime.channel_agent.control import request_switch
        return await request_switch(ctx, selected(ctx))
    if hasattr(ctx, "state_dir") and (selected(ctx) != "hermes" or mode(ctx)["desired"] != "hermes") and kind in {
        "start_hermes", "heal_hermes", "configure_telegram", "activate_codex_auth_models",
    }:
        return dict(changed=False, reason="native_framework_selected", **snapshot(ctx))
    module = import_module(module_name)
    result = await module.run(ctx, command)
    if hasattr(ctx, "state_dir") and kind == "start_hermes" and result.get("healthy") and selected(ctx) == "hermes":
        from hermes_runtime.channel_agent.control import save

        save(ctx, {"active": "hermes", "desired": "hermes", "status": "running"})
    return result
