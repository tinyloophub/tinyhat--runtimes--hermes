"""Apply saved chat channels without reinstalling Hermes or changing its model."""

from __future__ import annotations

import asyncio
import importlib
import sys
import time
from types import ModuleType
from urllib.parse import urlencode, urlsplit
from contextlib import contextmanager
from typing import Any

from hermes_runtime.hermes_cli import find_hermes_binary
from hermes_runtime.plugin_manager import DEFAULT_TINYHAT_PLUGIN_NAME, plugin_dir
from hermes_runtime.commands.configure_telegram import (
    _configure_tinyhat_menu_button,
    _run_gateway_for_managed_setup,
    _telegram_delete_webhook,
    _env_file_candidates,
    ensure_telegram_network_fallback_env,
    _install_codex_auth_quick_commands,
    _install_codex_auth_plugin_commands,
    _install_telegram_command_menu_priority,
)

SCHEMA = "tinyhat_configure_channels_v1"
_PACKAGE = "_tinyhat_runtime_channels_plugin"
CHANNEL_READY_TIMEOUT_SECONDS = 20
SUPPORTED_PROVIDERS = {"telegram", "slack"}


class AssignmentChanged(RuntimeError):
    """A confirmed assignment rejection, distinct from unavailable transport."""


class ChannelSetupError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _prepare_telegram(channel):
    url = urlsplit(str(channel.get("settings_miniapp_url") or ""))
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise ChannelSetupError("settings_unavailable")
    if not ensure_telegram_network_fallback_env(_env_file_candidates()).get("ok"):
        raise ChannelSetupError("network_unavailable")
    _install_codex_auth_quick_commands()
    _install_codex_auth_plugin_commands()
    _install_telegram_command_menu_priority()


@contextmanager
def channel_adapter():
    root = plugin_dir(DEFAULT_TINYHAT_PLUGIN_NAME)
    if not (root / "capabilities/channels/runtime.py").is_file():
        raise RuntimeError("Install the Tinyhat plugin before configuring channels.")
    # The runtime adapter is standard-library-only. Loading the Hermes plugin
    # entrypoint would import every tool and its Hermes-only dependencies into
    # the supervisor's independent Python environment.
    # Runtime command dispatch serializes scopes that touch this namespace.
    package = ModuleType(_PACKAGE)
    package.__path__ = [str(root)]
    sys.modules[_PACKAGE] = package
    try:
        yield importlib.import_module(f"{_PACKAGE}.capabilities.channels.runtime")
    finally:
        for name in tuple(sys.modules):
            if name == _PACKAGE or name.startswith(_PACKAGE + "."):
                sys.modules.pop(name, None)


def api_path(ctx: Any) -> str:
    kind = "me" if str(getattr(ctx, "platform_auth", "")) == "gcloud" else "local-dev"
    return f"/hapi/v2/computers/{kind}/channels"


async def _connected(providers: list[str], since: float) -> dict[str, bool | None]:
    from hermes_runtime.gateway_readiness import connected_channel_states

    deadline = time.monotonic() + CHANNEL_READY_TIMEOUT_SECONDS
    while True:
        states = await asyncio.to_thread(
            connected_channel_states, providers, since_unix=since
        )
        if all(states.values()) or time.monotonic() >= deadline:
            return states
        await asyncio.sleep(1)


async def run(ctx: Any, command: dict[str, Any]) -> dict[str, Any]:
    """Apply a batch, restoring its provider values if activation is unhealthy.

    Recovery touches only channel configuration; model, email and files are not
    replaced. Confirmed reassignment stops the gateway instead of restoring an
    earlier owner's secrets. Transient platform failures preserve running chat.
    """
    path = api_path(ctx)
    assignment = str((command.get("spec") or {}).get("assignment") or "")
    if not assignment:
        raise RuntimeError("A current Computer assignment is required.")
    configuration_path = path + "?" + urlencode({"assignment": assignment})

    async def current():
        config = await ctx.platform.get_json(configuration_path)
        if config.get("assignment") != assignment:
            raise AssignmentChanged("Computer assignment changed.")
        return config

    async def acknowledge(channel, connected, error=None):
        await ctx.platform.post_json(
            path + "/applied",
            {
                "assignment": assignment,
                "provider": channel["provider"],
                "revision": channel["revision"],
                "connected": connected,
                "error": error,
            },
        )

    changed = False
    try:
        config = await current()
        with channel_adapter() as adapter:
            key = await asyncio.to_thread(adapter.prepare_key, assignment)
            await ctx.platform.post_json(
                path + "/key",
                {
                    "assignment": assignment,
                    "public_key_pem": key["public_key_pem"],
                    "slack_manifest": await asyncio.to_thread(adapter.slack_manifest),
                },
            )
            channels = [
                {**channel, "revision": str(channel["revision"])}
                for channel in config.get("channels", [])
                if channel.get("provider") in SUPPORTED_PROVIDERS
            ]
            pending = [
                channel
                for channel in channels
                if channel.get("status") != "connected"
                or adapter.applied_revision(assignment, channel["provider"])
                != channel["revision"]
            ]
            if not pending:
                return {
                    "schema": SCHEMA,
                    "changed": False,
                    "prepared": True,
                    "channels": [],
                }
            hermes = find_hermes_binary()
            if hermes is None:
                raise RuntimeError("Hermes is not installed on this Computer.")
            installed, outcomes, snapshots = [], [], {}
            for channel in pending:
                provider = channel["provider"]
                await current()
                snapshot = await asyncio.to_thread(adapter.snapshot_channel, provider)
                wrote = False
                try:
                    if provider == "telegram":
                        await asyncio.to_thread(_prepare_telegram, channel)
                    # A failed CLI write may be partial. Retain prior values for
                    # recovery before any secret is changed.
                    changed = wrote = True
                    await asyncio.to_thread(
                        adapter.install_channel, assignment, channel
                    )
                    if provider == "telegram":
                        cleared = await asyncio.to_thread(
                            _telegram_delete_webhook, channel["bot_token"]
                        )
                        if not cleared.get("ok"):
                            raise ChannelSetupError("network_unavailable")
                        menu = await _configure_tinyhat_menu_button(
                            token=channel["bot_token"],
                            settings_url=channel["settings_miniapp_url"],
                            owner_chat_id=channel["owner_id"],
                        )
                        if not menu.get("configured"):
                            raise ChannelSetupError("settings_unavailable")
                    snapshots[provider] = snapshot
                    installed.append(channel)
                except Exception as exc:
                    if wrote:
                        await current()
                        await asyncio.to_thread(adapter.restore_channel, snapshot)
                    code = (
                        exc.code
                        if isinstance(exc, ChannelSetupError)
                        else "invalid_credentials"
                    )
                    await acknowledge(channel, False, code)
                    outcomes.append(
                        {"provider": provider, "status": "failed", "error": code}
                    )
            await current()
            if installed:
                since = time.time()
                gateway, _ = await _run_gateway_for_managed_setup(hermes)
                states = (
                    await _connected([item["provider"] for item in installed], since)
                    if gateway.get("healthy")
                    else {}
                )
                await current()
                if not gateway.get("healthy") or not all(
                    states.get(item["provider"]) is True for item in installed
                ):
                    # Failed activation must not strand existing email/chat on
                    # bad new credentials. Restore this batch and restart the
                    # previous provider configuration after the owner fence.
                    for snapshot in snapshots.values():
                        await asyncio.to_thread(adapter.restore_channel, snapshot)
                    recovery, _ = await _run_gateway_for_managed_setup(hermes)
                    for channel in installed:
                        code = (
                            "readiness_unknown"
                            if gateway.get("healthy")
                            and states.get(channel["provider"]) is None
                            else "gateway_unavailable"
                        )
                        await acknowledge(channel, False, code)
                    raise RuntimeError(
                        "Channel activation failed; previous channel settings restored."
                        if recovery.get("healthy")
                        else "Channel activation and gateway recovery failed."
                    )
                for channel in installed:
                    await acknowledge(channel, True)
                    adapter.record_applied(
                        assignment, channel["provider"], channel["revision"]
                    )
                    outcomes.append(
                        {"provider": channel["provider"], "status": "connected"}
                    )
            if any(item["status"] == "failed" for item in outcomes):
                raise RuntimeError(
                    "A channel could not be configured. Check its reported status."
                )
            return {
                "schema": SCHEMA,
                "changed": changed,
                "prepared": True,
                "channels": outcomes,
            }
    except Exception as exc:
        rejected = isinstance(exc, AssignmentChanged) or getattr(
            exc, "status_code", None
        ) in {401, 403, 404, 409}
        if changed and rejected:
            from hermes_runtime.commands.stop_hermes import run as stop_gateway

            await stop_gateway(ctx, {})
        # Only value-free errors leave the runtime; provider errors can contain
        # credentials or subprocess output. A failed command is never success.
        raise RuntimeError(
            "Channel setup could not finish. Check channel status and retry."
        ) from None
