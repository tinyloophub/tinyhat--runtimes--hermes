"""Apply saved chat channels without reinstalling Hermes or changing its model."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
import time
from types import ModuleType
from urllib.parse import urlencode
from contextlib import contextmanager
from typing import Any

from hermes_runtime.hermes_cli import find_hermes_binary
from hermes_runtime.plugin_manager import DEFAULT_TINYHAT_PLUGIN_NAME, plugin_dir
from hermes_runtime.commands.configure_telegram import (
    _configure_tinyhat_menu_button,
    _run_gateway_for_managed_setup,
    _telegram_delete_webhook,
)

SCHEMA = "tinyhat_configure_channels_v1"
_PACKAGE = "_tinyhat_runtime_channels_plugin"


@contextmanager
def channel_adapter():
    root = plugin_dir(DEFAULT_TINYHAT_PLUGIN_NAME)
    if not (root / "capabilities/channels/runtime.py").is_file():
        raise RuntimeError("Install the Tinyhat plugin before configuring channels.")
    # The runtime adapter is standard-library-only. Loading the Hermes plugin
    # entrypoint would import every tool and its Hermes-only dependencies into
    # the supervisor's independent Python environment.
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


async def _connected(providers: list[str], since: float) -> dict[str, bool]:
    from hermes_runtime.gateway_readiness import connected_channel_states

    deadline = time.monotonic() + 20
    while True:
        states = await asyncio.to_thread(
            connected_channel_states, providers, since_unix=since
        )
        if all(states.values()) or time.monotonic() >= deadline:
            return states
        await asyncio.sleep(1)


async def run(ctx: Any, command: dict[str, Any]) -> dict[str, Any]:
    path = api_path(ctx)
    assignment = str((command.get("spec") or {}).get("assignment") or "")
    if not assignment:
        raise RuntimeError("A current Computer assignment is required.")
    configuration_path = path + "?" + urlencode({"assignment": assignment})

    async def current():
        config = await ctx.platform.get_json(configuration_path)
        if config.get("assignment") != assignment:
            raise RuntimeError("Computer assignment changed.")
        return config

    changed = False
    activation_fenced = False
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
            channels = config.get("channels") or []
            pending = [
                channel
                for channel in channels
                if channel.get("status") != "connected"
                or adapter.applied_revision(assignment, str(channel.get("provider")))
                != channel.get("revision")
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
            installed, outcomes = [], []
            for channel in pending:
                provider, revision = str(channel["provider"]), str(channel["revision"])
                await current()
                try:
                    # A failed CLI write can be partial; any later fence failure
                    # must stop the gateway before returning.
                    changed = True
                    await asyncio.to_thread(
                        adapter.install_channel, assignment, channel
                    )
                    if provider == "telegram":
                        cleared = await asyncio.to_thread(
                            _telegram_delete_webhook, channel["bot_token"]
                        )
                        if not cleared.get("ok"):
                            raise RuntimeError(
                                "Telegram webhook could not be released."
                            )
                        menu = await _configure_tinyhat_menu_button(
                            token=channel["bot_token"],
                            settings_url=str(channel.get("settings_miniapp_url") or ""),
                            owner_chat_id=channel["owner_id"],
                        )
                        if not menu.get("configured"):
                            raise RuntimeError(
                                "The Computer menu could not be configured."
                            )
                    installed.append(channel)
                except Exception:
                    # Provider failures may contain credentials. Never return
                    # their exception text or subprocess output to the platform.
                    await ctx.platform.post_json(
                        path + "/applied",
                        {
                            "assignment": assignment,
                            "provider": provider,
                            "revision": revision,
                            "connected": False,
                            "error": "invalid_credentials",
                        },
                    )
                    outcomes.append({"provider": provider, "status": "failed"})
            await current()
            if installed:
                since = time.time()
                gateway, _notification = await _run_gateway_for_managed_setup(hermes)
                states = await _connected(
                    [item["provider"] for item in installed], since
                )
                await current()
                activation_fenced = True
                for channel in installed:
                    provider, revision = channel["provider"], channel["revision"]
                    healthy = bool(gateway.get("healthy")) and states.get(
                        provider, False
                    )
                    await ctx.platform.post_json(
                        path + "/applied",
                        {
                            "assignment": assignment,
                            "provider": provider,
                            "revision": revision,
                            "connected": healthy,
                            "error": None if healthy else "gateway_unavailable",
                        },
                    )
                    if healthy:
                        adapter.record_applied(assignment, provider, revision)
                    outcomes.append(
                        {
                            "provider": provider,
                            "status": "connected" if healthy else "failed",
                        }
                    )
            return {
                "schema": SCHEMA,
                "changed": bool(installed),
                "prepared": True,
                "channels": outcomes,
            }
    except Exception as exc:
        # An uncertain acknowledgement must not take down healthy email/chat.
        # A confirmed authentication/assignment rejection still fails closed.
        rejected = getattr(exc, "status_code", None) in {401, 403, 404, 409}
        if changed and (not activation_fenced or rejected):
            from hermes_runtime.commands.stop_hermes import run as stop_gateway

            await stop_gateway(ctx, {})
        raise RuntimeError(
            "Channel setup could not finish. Check the Computer assignment and retry."
        ) from None
