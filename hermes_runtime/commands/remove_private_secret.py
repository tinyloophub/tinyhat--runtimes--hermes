"""Remove one Tinyhat private credential from Hermes' public env surface.

The platform sends only a validated env name and generation identifiers.  The
runtime removes matching assignments locally, unregisters terminal passthrough,
clears the current process value, and restarts the gateway.  Results contain
names and paths only, never credential values.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from hermes_runtime.commands import heal_hermes
from hermes_runtime.runtime_env import env_file_candidates, read_env_values
from hermes_runtime.terminal_env_passthrough import sync_terminal_env_passthrough
from hermes_runtime.terminal_secret_aliases import force_alias_name

SCHEMA = "tinyhat_hermes_remove_private_secret_v1"
ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,126}$")


def _assignment_name(line: str) -> str | None:
    clean = line.strip()
    if not clean or clean.startswith("#") or "=" not in clean:
        return None
    if clean.startswith("export "):
        clean = clean[len("export ") :].lstrip()
    key, _, _value = clean.partition("=")
    key = key.strip()
    return key or None


def _remove_name_from_env_file(path: Path, name: str) -> dict[str, Any]:
    expanded = path.expanduser()
    try:
        before = expanded.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"path": str(expanded), "updated": False, "removed": False}
    lines = before.splitlines()
    removed_names = {name, force_alias_name(name)}
    next_lines = [line for line in lines if _assignment_name(line) not in removed_names]
    after = "\n".join(next_lines).rstrip() + "\n"
    changed = after != before
    if changed:
        expanded.write_text(after, encoding="utf-8")
        expanded.chmod(0o600)
    return {
        "path": str(expanded),
        "updated": changed,
        "removed": changed,
    }


async def run(ctx: Any, command: dict[str, Any]) -> dict[str, Any]:
    _ = ctx
    spec = command.get("spec") if isinstance(command.get("spec"), dict) else {}
    name = str(spec.get("env_name") or "").strip().upper()
    if ENV_NAME_RE.fullmatch(name) is None:
        raise RuntimeError("remove_private_secret requires a valid env-style name.")

    paths = env_file_candidates()
    env_files = [_remove_name_from_env_file(path, name) for path in paths]
    process_had_value = name in os.environ or force_alias_name(name) in os.environ
    os.environ.pop(name, None)
    os.environ.pop(force_alias_name(name), None)
    terminal_env_passthrough = sync_terminal_env_passthrough(
        [],
        remove_names=[name],
    )
    local_env_absent = name not in read_env_values(paths, names=[name])
    if not local_env_absent:
        raise RuntimeError("Hermes still reports the credential name after removal.")

    alias_files = terminal_env_passthrough.get("terminal_secret_aliases", {}).get(
        "env_files", []
    )
    terminal_updated = bool(
        terminal_env_passthrough.get("config", {}).get("updated")
    ) or any(
        isinstance(item, dict) and item.get("updated") is True for item in alias_files
    )
    changed = (
        process_had_value
        or any(item["updated"] for item in env_files)
        or terminal_updated
    )
    # Even when this retry finds the files already clean, an older gateway
    # process may still hold the prior value in memory after an earlier restart
    # failure. Always require Hermes's generation-bound functional restart
    # proof before the platform may delete its value-less metadata.
    gateway = await heal_hermes.run(
        ctx,
        {
            "kind": "heal_hermes",
            "spec": {
                "reason": "private_credential_removed",
                "restart": True,
                "deadline_seconds": 90,
            },
        },
    )
    gateway_ready = bool(gateway.get("healthy"))
    removal_verified = local_env_absent and gateway_ready

    return {
        "schema": SCHEMA,
        "env_name": name,
        "handoff_public_id": str(spec.get("handoff_public_id") or ""),
        "removal_request_id": str(spec.get("removal_request_id") or ""),
        "local_env_absent": True,
        "removal_verified": removal_verified,
        "removed_from_files": any(item["removed"] for item in env_files),
        "process_value_cleared": process_had_value,
        "env_files": env_files,
        "terminal_env_passthrough": terminal_env_passthrough,
        "gateway": gateway,
        "gateway_ready": gateway_ready,
        "restart_requested": True,
        "local_files_changed": changed,
    }
