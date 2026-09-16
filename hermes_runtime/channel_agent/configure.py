"""Reuse encrypted channel installation while respecting receiver ownership."""

import asyncio
from urllib.parse import urlencode

from hermes_runtime.channel_agent import control
from hermes_runtime.commands.configure_channels import (
    _restore_webhook,
    _snapshot_webhook,
    _telegram_transport,
    _validate_telegram_settings,
    api_path,
    channel_adapter,
)


async def stop_for_configuration(ctx):
    # Drain first so intake cannot race the idle check and credential writes.
    try:
        status = await control.rpc(ctx, "drain")
    except (FileNotFoundError, ConnectionRefusedError):
        # A stopped receiver has no intake to race. The exclusive service lock
        # still prevents activation alongside a process that is shutting down.
        return
    if status["queued"] or any(
        task["status"] in {"running", "waiting"} for task in status["tasks"]
    ):
        await control.rpc(ctx, "resume")
        raise RuntimeError(
            "Finish active channel tasks before changing channel credentials."
        )
    await control.rpc(ctx, "stop")
    for _ in range(100):
        if not control.socket_path(control.directory(ctx)).exists():
            break
        await asyncio.sleep(0.1)
    else:
        raise RuntimeError("Previous channel receiver has not stopped.")


async def configure_native(ctx, command):
    assignment = str(command.get("spec", {}).get("assignment") or "")
    if not assignment:
        raise ValueError("A current assignment is required.")
    path = api_path(ctx)

    async def current():
        config = await ctx.platform.get_json(
            path + "?" + urlencode({"assignment": assignment})
        )
        if config.get("assignment") != assignment:
            await control.suspend(ctx)
            raise ValueError("Computer assignment changed.")
        return config

    config = await current()
    status = await control.rpc(ctx, "status")
    if status["queued"] or any(
        task["status"] in {"running", "waiting"} for task in status["tasks"]
    ):
        raise RuntimeError("Finish active tasks before configuring another channel.")
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
        pending = [
            c
            for c in config.get("channels", [])
            if c.get("provider") in {"slack", "telegram"}
            and (
                c.get("status") != "connected"
                or await asyncio.to_thread(
                    adapter.applied_revision, assignment, c["provider"]
                )
                != c["revision"]
            )
        ]
        if not pending:
            return {"changed": False, "prepared": True}
        await stop_for_configuration(ctx)
        snapshots, webhooks = [], []
        activating = False
        try:
            for channel in pending:
                await current()
                if channel["provider"] == "telegram":
                    _validate_telegram_settings(channel)
                    snapshot = await asyncio.to_thread(
                        _snapshot_webhook, channel["bot_token"]
                    )
                    webhooks.append((channel["bot_token"], snapshot))
                snapshots.append(
                    await asyncio.to_thread(
                        adapter.snapshot_channel, channel["provider"]
                    )
                )
                await asyncio.to_thread(adapter.install_channel, assignment, channel)
                if channel["provider"] == "telegram":
                    await asyncio.to_thread(
                        _telegram_transport,
                        channel["bot_token"],
                        "deleteWebhook",
                        {"drop_pending_updates": False},
                    )
            await current()
            activating = True
            await control.start_native(ctx, control.selected(ctx))
        except Exception:
            # Before activation no update could have been consumed with the new
            # credentials. Afterwards preserve ownership for an explicit retry.
            if not activating:
                for snapshot in reversed(snapshots):
                    await asyncio.to_thread(adapter.restore_channel, snapshot)
                for token, snapshot in webhooks:
                    await asyncio.to_thread(_restore_webhook, token, snapshot)
                await current()
                await control.start_native(ctx, control.selected(ctx))
            raise
        live = await control.rpc(ctx, "status")
        for channel in pending:
            connected = live["channels"].get(channel["provider"]) is True
            await current()
            await ctx.platform.post_json(
                path + "/applied",
                {
                    "assignment": assignment,
                    "provider": channel["provider"],
                    "revision": channel["revision"],
                    "connected": connected,
                    "error": None if connected else "readiness_unknown",
                },
            )
            if (
                connected
                and channel["provider"] == "slack"
                and channel.get("identity_reporting_supported")
            ):
                identity = await asyncio.to_thread(adapter.slack_identity)
                await current()
                await ctx.platform.post_json(
                    path + "/slack/identity",
                    {
                        "assignment": assignment,
                        "revision": channel["revision"],
                        "workspace_id": identity["workspace_id"],
                        "app_id": identity["app_id"],
                    },
                )
            if connected:
                await asyncio.to_thread(
                    adapter.record_applied,
                    assignment,
                    channel["provider"],
                    channel["revision"],
                )
        return {"changed": True, "prepared": True, "channels": live["channels"]}
