"""Start the plugin-owned email channel using public Hermes configuration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError

from hermes_runtime.agent_systems import _write_atomic
from hermes_runtime.hermes_cli import find_hermes_binary
from hermes_runtime.openrouter_stt import hermes_python
from hermes_runtime.platform_paths import context_computer_api_path
from hermes_runtime.plugin_manager import hermes_home

logger = logging.getLogger(__name__)


def configure(home: Path, values: dict[str, str]) -> bool:
    """Idempotently add email defaults without changing user model selection."""
    if values.get("TINYHAT_EMAIL_CHANNEL_ENABLED") != "1":
        return False
    if not all(
        values.get(key)
        for key in (
            "TINYHAT_EMAIL_OWNER",
            "TINYHAT_MAILBOX_ADDRESS",
            "TINYHAT_MAILBOX_USERNAME",
            "TINYHAT_MAILBOX_PASSWORD",
            "TINYHAT_MAILBOX_JMAP_URL",
            "OPENROUTER_API_KEY",
            "TINYHAT_EMAIL_INITIAL_MODEL",
        )
    ):
        raise ValueError("Email configuration is incomplete")
    # YAML belongs to Hermes's environment; the runtime stays standard-library-only.
    result = subprocess.run(
        [
            str(hermes_python()),
            "-c",
            "from hermes_runtime.email_onboarding import _configure_file; _configure_file()",
        ],
        input=json.dumps(
            {"home": str(home), "model": values["TINYHAT_EMAIL_INITIAL_MODEL"]}
        ),
        text=True,
        capture_output=True,
        timeout=30,
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1])
            + os.pathsep
            + os.environ.get("PYTHONPATH", ""),
        },
    )
    if result.returncode != 0:
        raise RuntimeError("Hermes email configuration could not be written")
    return json.loads(result.stdout)["changed"] is True


def merge_config(config: dict, model: str) -> dict:
    """Merge only documented email defaults; preserve the owner's other settings."""
    if not isinstance(config, dict):
        raise ValueError("Hermes configuration is not an object")
    # Never replace a model/provider the owner has already selected.
    config.setdefault(
        "model",
        {"default": model, "provider": "openrouter"},
    )
    plugins = config.setdefault("plugins", {})
    active = plugins.setdefault("enabled", [])
    if not isinstance(active, list):
        raise ValueError("Hermes enabled plugins must be a list")
    if "tinyhat" not in active:
        active.append("tinyhat")
    platform = config.setdefault("platforms", {}).setdefault("tinyhat_email", {})
    platform["enabled"] = True
    platform["gateway_restart_notification"] = False
    platform.setdefault(
        "home_channel",
        {"platform": "tinyhat_email", "chat_id": "owner", "name": "Owner email"},
    )
    config.setdefault("platform_toolsets", {}).setdefault(
        "tinyhat_email", ["hermes-cli", "tinyhat"]
    )
    # Email has no editable progress bubbles: one final response per turn.
    display = (
        config.setdefault("display", {})
        .setdefault("platforms", {})
        .setdefault("tinyhat_email", {})
    )
    display.update(
        {
            "streaming": False,
            "tool_progress": "off",
            "interim_assistant_messages": False,
            "long_running_notifications": False,
            "show_reasoning": False,
            "busy_ack_detail": False,
        }
    )
    return config


def _configure_file():
    # Executed only by Hermes's Python, which owns its public YAML dependency.
    import yaml

    payload = json.load(sys.stdin)
    path = Path(payload["home"]) / "config.yaml"
    config = yaml.safe_load(path.read_text()) if path.exists() else {}
    if config is None:
        config = {}
    before = yaml.safe_dump(config, sort_keys=False)
    after = yaml.safe_dump(merge_config(config, payload["model"]), sort_keys=False)
    changed = before != after
    if changed:
        _write_atomic(path, after, 0o600)
    print(json.dumps({"changed": changed}))


def configuration_lock(ctx):
    if not hasattr(ctx, "email_configuration_lock"):
        ctx.email_configuration_lock = asyncio.Lock()
    return ctx.email_configuration_lock


async def reconcile(ctx):
    async with configuration_lock(ctx):
        await _reconcile_locked(ctx)


async def _reconcile_locked(ctx):
    from hermes_runtime.commands.apply_config import (
        _clean_secret_map,
        _env_file_candidates,
        _write_runtime_secret_env_file,
        load_env_files_into_process,
        sync_terminal_env_passthrough,
    )
    from hermes_runtime.commands.configure_telegram import _run_gateway

    stage = "channel status"
    try:
        # The v2 route accepts both attested and local-dev Computer identities.
        channel = await ctx.platform.get_json("/hapi/v2/computers/me/email")
        if channel.get("status") != "ready":
            return
        stage = "runtime credentials"
        payload = await ctx.platform.get_json(
            context_computer_api_path(ctx, "runtime-secrets")
        )
        values = _clean_secret_map(payload)
        fingerprint = hashlib.sha256(
            json.dumps(values, sort_keys=True).encode()
        ).hexdigest()
        if getattr(ctx, "email_config_fingerprint", None) == fingerprint:
            return
        stage = "Hermes email configuration"
        await asyncio.to_thread(configure, hermes_home(), values)
        if values.get("TINYHAT_EMAIL_CHANNEL_ENABLED") != "1":
            return
        stage = "environment refresh"
        applied = [
            _write_runtime_secret_env_file(path, values)
            for path in _env_file_candidates()
        ]
        removed = {key for item in applied for key in item.get("removed_keys", [])}
        for key in removed:
            os.environ.pop(key, None)
        load_env_files_into_process(
            [Path(item["path"]) for item in applied], keys=list(values)
        )
        sync_terminal_env_passthrough(list(values), remove_names=sorted(removed))
        stage = "gateway restart"
        binary = find_hermes_binary()
        if not binary:
            raise ValueError("Hermes is not installed")
        # A changed address/key must reach a running gateway even when its YAML
        # is unchanged or the platform's apply_config callback raced readiness.
        result = await _run_gateway(binary)
        ctx.email_gateway_ready = result.get("healthy") is True
        if ctx.email_gateway_ready:
            ctx.email_config_fingerprint = fingerprint
            ctx.email_setup_failure = None
        else:
            raise RuntimeError("Gateway did not become healthy")
    except Exception as exc:
        status = exc.__cause__.code if isinstance(exc.__cause__, HTTPError) else None
        ctx.email_setup_failure = {
            "stage": stage,
            "error_type": type(exc).__name__,
            "http_status": status,
        }
        log = (
            logger.debug
            if stage == "channel status" and status == 404
            else logger.warning
        )
        # Never log response bodies, environment values or YAML fragments.
        log(
            "Email onboarding retry at %s (%s, HTTP %s)",
            stage,
            type(exc).__name__,
            status,
        )
        ctx.email_gateway_ready = False
    finally:
        ctx.email_checked_at = time.monotonic()


def schedule(ctx):
    """At most one setup attempt per minute, independently of heartbeat I/O."""
    if not getattr(ctx, "agent_api_context_ready", False):
        return
    task = getattr(ctx, "email_setup_task", None)
    if task and not task.done():
        return
    if time.monotonic() - getattr(ctx, "email_checked_at", 0) < 60:
        return
    # An active platform command can own the gateway restart/config files.
    if getattr(ctx, "command_task", None) and not ctx.command_task.done():
        return
    ctx.email_setup_task = asyncio.create_task(reconcile(ctx))
