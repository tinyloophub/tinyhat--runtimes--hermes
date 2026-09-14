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

from hermes_runtime.client import PlatformError
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


def _validate_telegram_settings(channel):
    url = urlsplit(str(channel.get("settings_miniapp_url") or ""))
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise ChannelSetupError("settings_unavailable")


def _prepare_telegram(channel):
    _validate_telegram_settings(channel)
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
    payload = {
        "url": url,
        "max_connections": result.get("max_connections", 40),
        "drop_pending_updates": False,
    }
    if "allowed_updates" in result:
        payload["allowed_updates"] = result["allowed_updates"]
    return payload


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
                else None
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


async def _connected(
    providers: list[str], since: float, *, survivors: bool = False
) -> dict[str, bool | None]:
    from hermes_runtime.gateway_readiness import connected_channel_states

    deadline = time.monotonic() + CHANNEL_READY_TIMEOUT_SECONDS
    while True:
        states = await asyncio.to_thread(
            connected_channel_states,
            providers,
            since_unix=since,
            stale_is_unknown=survivors,
        )
        if survivors and all(state is not False for state in states.values()):
            return states
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
            unsupported = []
            for channel in config.get("channels") or []:
                if channel.get("provider") not in SUPPORTED_PROVIDERS:
                    if channel.get("status") != "connected":
                        # A newer platform can offer providers this runtime
                        # does not understand. Do not block supported siblings.
                        try:
                            if channel.get("revision"):
                                await acknowledge(channel, False, "setup_failed")
                            else:
                                logger.warning(
                                    "Unsupported channel has no revision; acknowledgement skipped"
                                )
                        except PlatformError as report_error:
                            if getattr(report_error, "status_code", None) in {
                                401,
                                403,
                                404,
                                409,
                            }:
                                raise
                            logger.warning(
                                "Unsupported channel acknowledgement failed: exception_type=%s",
                                type(report_error).__name__,
                            )
                        unsupported.append(
                            {
                                "provider": channel.get("provider"),
                                "status": "failed",
                                "error": "setup_failed",
                            }
                        )
                    else:
                        unsupported.append(
                            {"provider": channel.get("provider"), "status": "connected"}
                        )
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
                    "channels": unsupported
                    + [
                        {"provider": c["provider"], "status": "connected"}
                        for c in channels
                    ],
                }
            hermes = find_hermes_binary()
            if hermes is None:
                raise RuntimeError("Hermes is not installed on this Computer.")
            outcomes = unsupported + [
                {"provider": c["provider"], "status": "connected"}
                for c in channels
                if c not in pending
            ]
            by_provider = {c["provider"]: c for c in channels}

            async def refresh_survivors(gateway, since):
                survivors = [
                    item
                    for item in outcomes
                    if item["status"] == "connected" and item["provider"] in by_provider
                ]
                if not survivors:
                    return
                states = (
                    await _connected(
                        [item["provider"] for item in survivors], since, survivors=True
                    )
                    if gateway.get("healthy")
                    else {}
                )
                await current()
                for item in survivors:
                    state = states.get(item["provider"])
                    # Keep the restart-time fence. Inherited provider rows are
                    # unknown, never proof of a new connection. Preserve prior
                    # success on unknown evidence; fresh failures get the probe
                    # budget to reconnect before they are marked failed.
                    if gateway.get("healthy") and state is not False:
                        continue
                    code = (
                        "setup_failed"
                        if gateway.get("healthy")
                        else "gateway_unavailable"
                    )
                    await acknowledge(by_provider[item["provider"]], False, code)
                    item.update(status="failed", error=code)
                    # Applied revision means values were installed, not that
                    # this gateway generation is connected. Failed platform
                    # status forces an explicit retry despite that local marker.

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
                        _validate_telegram_settings(channel)
                        webhook = await asyncio.to_thread(
                            _snapshot_webhook, channel["bot_token"]
                        )
                        changed = True
                        await asyncio.to_thread(_prepare_telegram, channel)
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
                    # Every restart replaces the generation supporting earlier
                    # success, including providers skipped as already applied.
                    await refresh_survivors(gateway, since)
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
                    code = (
                        exc.code
                        if isinstance(exc, ChannelSetupError)
                        else "invalid_credentials"
                    )
                    # Reporting must not prevent local recovery. A confirmed
                    # ownership rejection still stops all local mutation.
                    report_error = None
                    try:
                        await acknowledge(channel, False, code)
                    except Exception as failure:
                        if getattr(failure, "status_code", None) in {
                            401,
                            403,
                            404,
                            409,
                        }:
                            raise
                        report_error = failure
                    outcome = {"provider": provider, "status": "failed", "error": code}
                    outcomes.append(outcome)
                    recovery = {"healthy": False}
                    recovery_since = time.time()
                    recovery_fenced = False
                    try:
                        if wrote:
                            await current()
                            await asyncio.to_thread(adapter.restore_channel, snapshot)
                        if restarted:
                            recovery, _ = await _run_gateway_for_managed_setup(hermes)
                        if released:
                            await current()
                            await asyncio.to_thread(
                                _restore_webhook, channel["bot_token"], webhook
                            )
                    except Exception as recovery_error:
                        if isinstance(
                            recovery_error, (AssignmentChanged, AssignmentUnavailable)
                        ) or getattr(recovery_error, "status_code", None) in {
                            401,
                            403,
                            404,
                            409,
                        }:
                            recovery_fenced = True
                            raise
                        logger.warning(
                            "Channel recovery failed: provider=%s exception_type=%s",
                            provider,
                            type(recovery_error).__name__,
                        )
                        # Keep the original activation failure in platform state;
                        # recovery diagnostics belong to this failed command.
                        raise
                    finally:
                        # Also runs when webhook restoration fails or the
                        # gateway cannot recover. Never retain stale success.
                        if restarted and not recovery_fenced:
                            await refresh_survivors(recovery, recovery_since)
                    if report_error is not None:
                        raise report_error
                    if restarted and not recovery.get("healthy"):
                        raise RuntimeError(
                            "Channel activation and gateway recovery failed."
                        )
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
