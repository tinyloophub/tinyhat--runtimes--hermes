"""Persistent mode selection and exclusive receiver supervision."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import platform
import secrets
import time
from pathlib import Path

from hermes_runtime.agent_systems import _write_atomic
from hermes_runtime.channel_agent.native import probe
from hermes_runtime.channel_agent.paths import prepare_socket_directory, socket_path
from hermes_runtime.channel_agent.revision import installed_revision
from hermes_runtime.hermes_cli import find_hermes_binary, run_process
from hermes_runtime.openrouter_stt import hermes_python
from hermes_runtime.plugin_manager import DEFAULT_TINYHAT_PLUGIN_NAME, plugin_dir

FRAMEWORKS = {"hermes", "codex", "claude_code"}


def assignment(ctx):
    try:
        return (Path(ctx.state_dir) / "channel-agent-assignment").read_text().strip()
    except FileNotFoundError:
        return None


def directory(ctx):
    binding = assignment(ctx)
    name = (
        hashlib.sha256(binding.encode()).hexdigest()[:24] if binding else "unassigned"
    )
    return Path(ctx.state_dir) / "channel-agent" / name


async def bind(ctx, binding):
    """Called only after the platform confirms the command's assignment."""
    if assignment(ctx) == binding:
        return
    await suspend(ctx)
    _write_atomic(Path(ctx.state_dir) / "channel-agent-assignment", binding, 0o600)
    save(ctx, {"active": "hermes", "desired": "hermes", "status": "starting"})


def mode(ctx):
    if not hasattr(ctx, "state_dir"):
        return {"active": "hermes", "desired": "hermes", "status": "running"}
    try:
        value = json.loads((directory(ctx) / "mode.json").read_text())
        if value["active"] not in FRAMEWORKS or value["desired"] not in FRAMEWORKS:
            raise ValueError("Invalid selected framework.")
        return value
    except FileNotFoundError:
        return {"active": "hermes", "desired": "hermes", "status": "running"}


def selected(ctx):
    return mode(ctx)["active"]


def save(ctx, value):
    _write_atomic(directory(ctx) / "mode.json", json.dumps(value), 0o600)


async def rpc(ctx, action, **values):
    path = directory(ctx)
    prepare_socket_directory(path)
    reader, writer = await asyncio.wait_for(
        asyncio.open_unix_connection(socket_path(path)), 5
    )
    try:
        writer.write(
            (
                json.dumps(
                    {
                        "control": (path / "control").read_text().strip(),
                        "action": action,
                        **values,
                    }
                )
                + "\n"
            ).encode()
        )
        await writer.drain()
        result = json.loads(await asyncio.wait_for(reader.readline(), 45))
        if "error" in result:
            raise RuntimeError("Channel service could not complete the operation.")
        return result["result"]
    finally:
        writer.close()
        await writer.wait_closed()


def snapshot(ctx):
    current = mode(ctx)
    result = {
        "assignment": assignment(ctx) if hasattr(ctx, "state_dir") else None,
        "schema": "tinyhat.channel-agent.v1",
        **current,
        "frameworks": getattr(ctx, "channel_framework_inventory", {}),
        "tasks": [],
        "approvals": [],
        "channels": {},
    }
    if current["active"] != "hermes":
        try:
            status = json.loads((directory(ctx) / "status.json").read_text())
            if status["active"] == current["active"]:
                result.update(
                    {
                        key: status[key]
                        for key in ("tasks", "approvals", "channels", "queued")
                    }
                )
                if time.time() - status["updated_at"] > 20 or (
                    current.get("status") == "running"
                    and not all(status["channels"].values())
                ):
                    result["status"] = "unavailable"
        except (OSError, ValueError, KeyError):
            result["status"] = "starting"
    # Leave room for the rest of the 32 KiB platform heartbeat. Recent tasks
    # are ordered active-first; their complete records remain on the Computer.
    while result["tasks"] and len(json.dumps(result).encode()) > 12 * 1024:
        result["tasks"].pop()
    return result


async def inventory(ctx):
    values = await asyncio.gather(*(probe(name) for name in sorted(FRAMEWORKS)))
    ctx.channel_framework_inventory = dict(zip(sorted(FRAMEWORKS), values, strict=True))
    return ctx.channel_framework_inventory


async def preflight(ctx, framework):
    values = await inventory(ctx)
    target = values[framework]
    if not target["installed"]:
        raise RuntimeError("Install the selected framework first.")
    if not target["authenticated"]:
        raise RuntimeError("Sign in to the selected framework on the Computer first.")
    if framework != "hermes":
        root = plugin_dir(DEFAULT_TINYHAT_PLUGIN_NAME)
        for name in ("tinyhat-route-message", "tinyhat-respond"):
            if not (root / "skills" / name / "SKILL.md").is_file():
                raise RuntimeError(
                    "Update the Tinyhat plugin before switching frameworks."
                )
        check = await run_process(
            [str(hermes_python()), "-c", "import aiohttp"], timeout_seconds=10
        )
        if not check.get("ok"):
            raise RuntimeError("Repair the installed Hermes Python dependencies first.")


async def start_native(ctx, framework):
    path = directory(ctx)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not (path / "control").exists():
        _write_atomic(path / "control", secrets.token_urlsafe(32), 0o600)
    try:
        status = await rpc(ctx, "status")
        if status["active"] != framework:
            raise RuntimeError("Another native framework still owns the channels.")
        if status.get("revision") == installed_revision():
            return status
        # A detached receiver may survive a runtime upgrade. Finish its turns
        # before replacing it; never leave the old provider adapters running.
        status = await rpc(ctx, "drain")
        if status["queued"] or any(
            t["status"] in {"running", "waiting", "queued"} for t in status["tasks"]
        ):
            return status
        await rpc(ctx, "stop")
        for _ in range(100):
            if not socket_path(path).exists():
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("Previous receiver has not stopped after an update.")
    except (TimeoutError, OSError):
        pass
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2]))
    process = await asyncio.create_subprocess_exec(
        str(hermes_python()),
        "-m",
        "hermes_runtime.channel_agent.service",
        "--state-dir",
        str(path),
        "--framework",
        framework,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    ctx.channel_process = process
    for _ in range(40):
        if process.returncode is not None:
            raise RuntimeError("Native channel service did not start.")
        try:
            status = await rpc(ctx, "status")
            if status.get("channels") and all(status["channels"].values()):
                return status
        except (TimeoutError, OSError):
            pass
        await asyncio.sleep(1)
    raise RuntimeError("Native channels did not become ready.")


async def request_switch(ctx, framework):
    if framework not in FRAMEWORKS:
        raise ValueError("Unknown framework.")
    startup = getattr(ctx, "gateway_reconcile_task", None)
    if startup is not None and not startup.done():
        raise RuntimeError("Wait for the current receiver's startup to finish.")
    await preflight(ctx, framework)
    current = mode(ctx)
    if current["active"] == framework:
        if framework != "hermes":
            live = await start_native(ctx, framework)
            if live.get("revision") != installed_revision():
                return snapshot(ctx)  # Existing turns must finish the update.
            await rpc(ctx, "resume")
        elif current.get("status") in {"failed", "starting", "suspended"}:
            from hermes_runtime.commands.start_hermes import run

            result = await run(ctx, {"kind": "start_hermes", "_framework_switch": True})
            if not result.get("healthy"):
                raise RuntimeError("Hermes did not become ready.")
        save(ctx, {"active": framework, "desired": framework, "status": "running"})
    else:
        save(ctx, {**current, "desired": framework, "status": "switching"})
    return snapshot(ctx)


async def reconcile(ctx):
    current = mode(ctx)
    old, target = current["active"], current["desired"]
    if old == target:
        if current.get("status") == "suspended":
            return
        if target != "hermes":
            live = await start_native(ctx, target)
            if live.get("channels") and all(live["channels"].values()):
                save(ctx, {"active": target, "desired": target, "status": "running"})
        elif current.get("status") in {"failed", "starting"}:
            await request_switch(ctx, "hermes")
        return
    await preflight(ctx, target)
    if old != "hermes":
        await start_native(ctx, old)
        status = await rpc(ctx, "drain")
        if status["queued"] or any(
            t["status"] in {"running", "waiting", "queued"} for t in status["tasks"]
        ):
            return
        await rpc(ctx, "stop")
        for _ in range(50):
            if not socket_path(directory(ctx)).exists():
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("Previous receiver has not stopped.")
    else:
        binary = find_hermes_binary()
        manager_args = []
        if platform.system() == "Linux":
            from hermes_runtime.gateway_service import (
                GATEWAY_SERVICE_UNOWNED_REASONS,
                discover_gateway_service,
            )

            ownership = await discover_gateway_service()
            if ownership.get("ok"):
                manager_args = (
                    ["--system"] if ownership["owner"]["manager"] == "system" else []
                )
            elif ownership.get("reason") not in GATEWAY_SERVICE_UNOWNED_REASONS:
                raise RuntimeError(
                    "Cannot determine which service owns the Hermes receiver."
                )
        # Only the supported graceful stop. Never force-kill a working agent
        # merely to make a framework switch succeed.
        result = await run_process(
            [str(binary), "gateway", "stop", *manager_args], timeout_seconds=600
        )
        if not result.get("ok"):
            raise RuntimeError(
                "Hermes could not stop safely; framework was not changed."
            )
        from hermes_runtime.commands.stop_hermes import _gateway_status_is_stopped

        status = await run_process(
            [str(binary), "gateway", "status", *manager_args], timeout_seconds=30
        )
        if not _gateway_status_is_stopped(status):
            raise RuntimeError("Previous receiver has not confirmed that it stopped.")
        # Otherwise an enabled service would return on reboot and compete
        # with the native receiver. The public start command reinstalls it
        # when the user returns to Hermes; sessions/configuration stay intact.
        uninstalled = await run_process(
            [str(binary), "gateway", "uninstall", *manager_args], timeout_seconds=60
        )
        if not uninstalled.get("ok"):
            raise RuntimeError(
                "Could not disable the previous receiver's automatic start."
            )
    # Commit ownership BEFORE starting a receiver. Once it can consume a
    # message, an automatic rollback could duplicate work in another framework.
    # A failed activation stays on this target and is recovered by supervision.
    save(ctx, {"active": target, "desired": target, "status": "starting"})
    if target == "hermes":
        from hermes_runtime.commands.start_hermes import run

        result = await run(ctx, {"kind": "start_hermes", "_framework_switch": True})
        if not result.get("healthy"):
            raise RuntimeError("Hermes did not become ready.")
    else:
        await start_native(ctx, target)
    save(ctx, {"active": target, "desired": target, "status": "running"})


async def suspend(ctx):
    """Fence native work before parking or revoking an assignment."""
    current = mode(ctx)
    save(ctx, {**current, "desired": current["active"], "status": "suspended"})
    if current["active"] == "hermes":
        return
    try:
        await rpc(ctx, "halt")
    except (FileNotFoundError, ConnectionRefusedError):
        return
    for _ in range(100):
        if not socket_path(directory(ctx)).exists():
            return
        await asyncio.sleep(0.1)
    raise RuntimeError("Native receiver has not stopped.")


def schedule(ctx):
    if getattr(ctx, "command_task", None) and not ctx.command_task.done():
        return
    task = getattr(ctx, "channel_reconcile_task", None)
    if task and not task.done():
        return
    if task:
        with contextlib.suppress(Exception):
            task.result()

    async def work():
        from hermes_runtime.email_onboarding import configuration_lock

        if time.monotonic() - getattr(ctx, "channel_inventory_at", 0) > 60:
            await inventory(ctx)
            ctx.channel_inventory_at = time.monotonic()
        if getattr(ctx, "command_task", None) and not ctx.command_task.done():
            return
        async with configuration_lock(ctx):
            if getattr(ctx, "platform_state", "") not in {"assigned", "active"}:
                if selected(ctx) != "hermes":
                    await suspend(ctx)
                return
            try:
                await reconcile(ctx)
            except Exception:
                current = mode(ctx)
                save(
                    ctx,
                    {**current, "status": "failed", "error": "framework_unavailable"},
                )

    ctx.channel_reconcile_task = asyncio.create_task(work())
