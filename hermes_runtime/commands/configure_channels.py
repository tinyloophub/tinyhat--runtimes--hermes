"""Apply saved chat channels without reinstalling Hermes or changing its model."""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import sys
import time
from types import ModuleType
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
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
logger = logging.getLogger(__name__)


class AssignmentChanged(RuntimeError):
    """A confirmed assignment rejection, distinct from unavailable transport."""


class AssignmentUnavailable(RuntimeError):
    """A transport failure while checking ownership must preserve working chat."""


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
    try:
        for install in (
            _install_codex_auth_quick_commands,
            _install_codex_auth_plugin_commands,
            _install_telegram_command_menu_priority,
        ):
            if not install().get("installed"):
                raise ChannelSetupError("settings_unavailable")
    except Exception:
        raise ChannelSetupError("settings_unavailable") from None


def _telegram_transport(token, method, payload=None):
    """Value-free failures; webhook URLs are credentials and stay in memory."""
    try:
        body = json.dumps(payload or {}).encode()
        req = Request(
            f"https://api.telegram.org/bot{token}/{method}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=10) as response:
            result = json.loads(response.read(65536))
        if not result.get("ok"):
            raise ValueError()
        return result.get("result")
    except Exception:
        raise ChannelSetupError("network_unavailable") from None


def _snapshot_webhook(token):
    result = _telegram_transport(token, "getWebhookInfo")
    if not isinstance(result, dict):
        raise ChannelSetupError("network_unavailable")
    url = str(result.get("url") or "")
    if not url:
        return None
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or result.get("has_custom_certificate")
    ):
        raise ChannelSetupError("network_unavailable")
    # Tinyhat managed-bot parking authenticates in its private URL, not with a
    # secret header. Preserve that observed destination and queued updates.
    return {
        "url": url,
        "allowed_updates": result.get("allowed_updates", ["message"]),
        "max_connections": result.get("max_connections", 40),
        "drop_pending_updates": False,
    }


def _restore_webhook(token, snapshot):
    if snapshot:
        _telegram_transport(token, "setWebhook", snapshot)


async def _telegram_fallback(hermes, since, budget):
    from hermes_runtime.gateway_service import (
        discover_gateway_service,
        gateway_generation_active,
        gateway_generation_same,
        snapshot_gateway_service,
    )
    from hermes_runtime.gateway_readiness import probe_functional_readiness
    from hermes_runtime.commands.configure_telegram import (
        _active_gateway_foreground_generation,
        _gateway_log_path,
    )

    discovery = await discover_gateway_service()
    generation = discovery.get("generation")
    if discovery.get("ok") and gateway_generation_active(generation):
        owner = discovery["owner"]
        result = await probe_functional_readiness(
            hermes,
            since_unix=since,
            service_manager=owner.get("manager", "user"),
            service_invocation_id=generation.get("invocation_id"),
            service_main_pid=generation.get("main_pid"),
            timeout_seconds=budget,
        )
        after = await snapshot_gateway_service(owner, timeout_seconds=budget)
        if gateway_generation_same(generation, after):
            return (
                result.get("telegram_connected")
                if result.get("status_healthy")
                else False
            )
        return None
    foreground = await asyncio.to_thread(_active_gateway_foreground_generation, hermes)
    if not foreground:
        return None
    result = await probe_functional_readiness(
        hermes,
        since_unix=since,
        log_path=_gateway_log_path(),
        log_offset=foreground["log_offset"],
        service_main_pid=foreground["pid"],
        expected_process_start_time=foreground["process_start_time"],
        expected_gateway_argv=foreground["argv"],
        timeout_seconds=budget,
    )
    after = await asyncio.to_thread(_active_gateway_foreground_generation, hermes)
    return (
        result.get("telegram_connected")
        if foreground == after and result.get("status_healthy")
        else None
    )


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
        remaining = deadline - time.monotonic()
        if "telegram" in providers and states.get("telegram") is None and remaining > 0:
            try:
                states["telegram"] = await asyncio.wait_for(
                    _telegram_fallback(find_hermes_binary(), since, remaining),
                    timeout=remaining,
                )
            except (TimeoutError, OSError):
                pass
        if all(states.values()) or time.monotonic() >= deadline:
            return states
        await asyncio.sleep(1)


async def run(ctx: Any, command: dict[str, Any]) -> dict[str, Any]:
    """Activate providers independently; a failed sibling cannot undo success.

    Restore confirmed failures, retain healthy-but-unverified configuration,
    and fail the command honestly. Model, email and files are not replaced.
    """
    path = api_path(ctx)
    assignment = str((command.get("spec") or {}).get("assignment") or "")
    if not assignment:
        raise RuntimeError("A current Computer assignment is required.")
    configuration_path = path + "?" + urlencode({"assignment": assignment})

    async def current():
        try:
            config = await ctx.platform.get_json(configuration_path)
        except Exception as exc:
            if getattr(exc, "status_code", None) in {401, 403, 404, 409}:
                raise
            raise AssignmentUnavailable(
                "Computer assignment could not be checked."
            ) from None
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
            channels = []
            for channel in config.get("channels") or []:
                if channel.get("provider") not in SUPPORTED_PROVIDERS:
                    continue
                if not channel.get("revision"):
                    raise RuntimeError("A channel revision is required.")
                channels.append({**channel, "revision": str(channel["revision"])})
            pending = []
            for channel in channels:
                applied = await asyncio.to_thread(
                    adapter.applied_revision, assignment, channel["provider"]
                )
                if (
                    channel.get("status") != "connected"
                    or applied != channel["revision"]
                ):
                    pending.append(channel)
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
            outcomes = []
            for channel in pending:
                provider = channel["provider"]
                await current()
                # An unreadable snapshot raises before installation; recovery
                # must never confuse unknown old values with absent values.
                try:
                    snapshot = await asyncio.to_thread(
                        adapter.snapshot_channel, provider
                    )
                except (OSError, ValueError):
                    await acknowledge(channel, False, "setup_failed")
                    outcomes.append(
                        {
                            "provider": provider,
                            "status": "failed",
                            "error": "setup_failed",
                        }
                    )
                    continue
                wrote = restarted = released = False
                webhook = None
                try:
                    if provider == "telegram":
                        await asyncio.to_thread(_prepare_telegram, channel)
                        webhook = await asyncio.to_thread(
                            _snapshot_webhook, channel["bot_token"]
                        )
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
                        released = True
                        menu = await _configure_tinyhat_menu_button(
                            token=channel["bot_token"],
                            settings_url=channel["settings_miniapp_url"],
                            owner_chat_id=channel["owner_id"],
                        )
                        if not menu.get("configured"):
                            raise ChannelSetupError("settings_unavailable")
                    await current()
                    since = time.time()
                    restarted = True
                    gateway, _ = await _run_gateway_for_managed_setup(hermes)
                    states = (
                        await _connected([provider], since)
                        if gateway.get("healthy")
                        else {}
                    )
                    await current()
                    if not gateway.get("healthy") or states.get(provider) is False:
                        raise ChannelSetupError("gateway_unavailable")
                    if states.get(provider) is not True:
                        # A healthy gateway with unknown evidence may be working.
                        # Keep it running and require an explicit retry to confirm.
                        await acknowledge(channel, False, "readiness_unknown")
                        outcomes.append(
                            {
                                "provider": provider,
                                "status": "failed",
                                "error": "readiness_unknown",
                            }
                        )
                        continue
                except Exception as exc:
                    # Do not undo or restart anything on an uncertain fence.
                    if (
                        isinstance(exc, (AssignmentChanged, AssignmentUnavailable))
                        or getattr(exc, "status_code", None) in {401, 403, 404, 409}
                        or (not isinstance(exc, ChannelSetupError) and restarted)
                    ):
                        raise
                    logger.warning(
                        "Channel installation failed: provider=%s exception_type=%s",
                        provider,
                        type(exc).__name__,
                    )
                    if wrote:
                        await current()
                        await asyncio.to_thread(adapter.restore_channel, snapshot)
                    if restarted:
                        recovery_since = time.time()
                        recovery, _ = await _run_gateway_for_managed_setup(hermes)
                    else:
                        recovery = {"healthy": True}
                    if released:
                        await current()
                        await asyncio.to_thread(
                            _restore_webhook, channel["bot_token"], webhook
                        )
                    code = (
                        exc.code
                        if isinstance(exc, ChannelSetupError)
                        else "invalid_credentials"
                    )
                    await acknowledge(channel, False, code)
                    outcomes.append(
                        {"provider": provider, "status": "failed", "error": code}
                    )
                    if not recovery.get("healthy"):
                        raise RuntimeError(
                            "Channel activation and gateway recovery failed."
                        )
                    # Prior providers keep their values; verify their state after
                    # the recovery restart rather than reporting stale success.
                    survivors = [
                        item for item in outcomes if item["status"] == "connected"
                    ]
                    if restarted and survivors:
                        recovered = await _connected(
                            [item["provider"] for item in survivors], recovery_since
                        )
                        for item in survivors:
                            if recovered.get(item["provider"]) is not True:
                                old = next(
                                    c
                                    for c in channels
                                    if c["provider"] == item["provider"]
                                )
                                code = (
                                    "readiness_unknown"
                                    if recovered.get(item["provider"]) is None
                                    else "gateway_unavailable"
                                )
                                await acknowledge(old, False, code)
                                item.update(status="failed", error=code)
                    continue
                await acknowledge(channel, True)
                await asyncio.to_thread(
                    adapter.record_applied, assignment, provider, channel["revision"]
                )
                outcomes.append({"provider": provider, "status": "connected"})
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
