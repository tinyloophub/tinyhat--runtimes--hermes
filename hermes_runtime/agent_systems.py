"""Public CLI inventory and explicit coding-agent Computer context.

The platform control process remains Hermes. A selected system is a terminal
application; model account authentication belongs to its user and is never
inferred from installation. This module does not read provider credentials or
configure a Telegram gateway.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
import shutil
import time
from pathlib import Path
from typing import Any

from hermes_runtime.hermes_cli import find_hermes_binary, run_process

SYSTEM_COMMANDS = {"codex": "codex", "claude_code": "claude", "hermes": "hermes", "openclaw": "openclaw"}
INVENTORY_SCHEMA = "tinyhat.agent-systems.v1"
CONTEXT_SCHEMA = "tinyhat.agent-api-context.v1"
REFRESH_SECONDS = 60


def enabled() -> bool:
    return os.getenv("TINYHAT_AGENT_SYSTEMS_PREINSTALLED", "0") == "1"


async def _probe(system: str, command: str) -> dict[str, Any]:
    binary = str(find_hermes_binary() or "") if system == "hermes" else shutil.which(command)
    if not binary:
        return {"ready": False, "version": None}
    result = await run_process([binary, "--version"], timeout_seconds=5)
    # --version is the supported, unauthenticated public interface. Never
    # include environment, diagnostics, stderr or model auth in inventory.
    output = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", str(result.get("stdout") or "")).strip()
    version = output.splitlines()[0][:120] if output else None
    return {"ready": bool(result.get("ok") and version), "version": version}


async def inventory() -> dict[str, Any]:
    probes = await asyncio.gather(*(_probe(system, command) for system, command in SYSTEM_COMMANDS.items()))
    desktop_ready = all(shutil.which(command) for command in ("tigervncserver", "vncpasswd", "startxfce4", "xfce4-terminal", "dbus-launch"))
    browser_ready = any(shutil.which(command) for command in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser"))
    result = {"schema": INVENTORY_SCHEMA, "desktop_ready": bool(desktop_ready and browser_ready), **dict(zip(SYSTEM_COMMANDS, probes))}
    # This is public build provenance, never credentials or machine identity.
    try:
        manifest = Path("/opt/tinyhat-agent-image/manifest.json").read_bytes()
        value = json.loads(manifest)
        if isinstance(value, dict) and value.get("schema") == "tinyhat.agent-image.v1":
            result["image_manifest_sha256"] = hashlib.sha256(manifest).hexdigest()
    except (OSError, ValueError):
        pass
    return result


def ready(value: dict[str, Any] | None) -> bool:
    return bool(value and value.get("desktop_ready") is True and all(isinstance(value.get(system), dict) and value[system].get("ready") is True for system in SYSTEM_COMMANDS))


async def refresh_inventory(ctx: Any) -> None:
    if not enabled():
        return
    now = time.monotonic()
    if getattr(ctx, "agent_systems_checked_at", None) is not None and now - ctx.agent_systems_checked_at < REFRESH_SECONDS:
        return
    ctx.agent_systems_inventory = await inventory()
    ctx.agent_systems_checked_at = now


def _validated_context(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("schema") != CONTEXT_SCHEMA:
        raise ValueError("Invalid coding-agent context schema")
    if value.get("system") not in SYSTEM_COMMANDS:
        raise ValueError("Unknown coding-agent system")
    for name, pattern in (("computer_id", r"cmp_[A-Za-z0-9_-]{32}"), ("agent_id", r"agt_[A-Za-z0-9_-]{22}")):
        if not isinstance(value.get(name), str) or re.fullmatch(pattern, value[name]) is None:
            raise ValueError("Invalid coding-agent context identity")
    return {key: value[key] for key in ("schema", "computer_id", "agent_id", "system")}


def _write_atomic(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    # Do not follow a previously planted temporary symlink.
    temporary.unlink(missing_ok=True)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(content)
    temporary.replace(path)


def apply_context(ctx: Any, value: Any, *, home: Path | None = None) -> None:
    context = _validated_context(value)
    if context is None:
        ctx.agent_api_context = None
        return
    # Only an explicit authenticated platform assignment changes the mode.
    # Repeated heartbeats do not overwrite the user's launcher or config.
    if getattr(ctx, "agent_api_context", None) == context and getattr(ctx, "agent_api_context_ready", False):
        return
    ctx.agent_api_context = context
    ctx.agent_api_context_ready = False
    if not ready(getattr(ctx, "agent_systems_inventory", None)):
        return
    directory = home or Path.home()
    command = SYSTEM_COMMANDS[context["system"]]
    launcher = directory / ".local/bin/tinyhat-agent"
    _write_atomic(directory / ".config/tinyhat/computer.json", json.dumps(context, indent=2) + "\n", 0o600)
    _write_atomic(launcher, '#!/bin/sh\nset -eu\nexec ' + shlex.quote(command) + ' "$@"\n', 0o700)
    desktop_exec = str(launcher).replace("\\", "\\\\").replace('"', '\\"')
    _write_atomic(directory / "Desktop/Tinyhat Agent.desktop",
        '[Desktop Entry]\nVersion=1.0\nType=Application\nName=Tinyhat Agent\n'
        'Comment=Open ' + context["system"] + '\nExec="' + desktop_exec + '"\n'
        'Icon=utilities-terminal\nTerminal=true\nCategories=Development;\n', 0o755)
    ctx.agent_api_context_ready = True


def acknowledgement(ctx: Any) -> dict[str, Any] | None:
    value = getattr(ctx, "agent_api_context", None)
    if value is None:
        return None
    return {**value, "ready": bool(getattr(ctx, "agent_api_context_ready", False) and ready(getattr(ctx, "agent_systems_inventory", None)))}
