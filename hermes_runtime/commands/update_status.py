"""Report current and locally staged runtime update state.

What it does:
    Returns the runtime code version, the installed runtime release ref, the
    installed commit sha when known, any staged update marker, and the most
    recent update-check result.

Update flow map:
    [pick target release]
        -> check_update     look only; writes updates/last_check.json
        -> stage_update     prepare selected ref; current runtime keeps running
        -> activate_update  request tinyhat-hermes-runtime.service restart
        -> service startup  promote staged ref into current/VERSION

    This command can be run at any point in the flow. It tells you what is
    current now, what is staged, whether that staged ref still needs
    activate_update, and what the latest update check found.

When to use it:
    Use this from Hat admin before or after staging or activating an update to
    see what is currently running and what step is still needed.

Example input:
    {"kind": "update_status", "spec": {}}

Example output:
    {
      "current_version": "v0.0.1",
      "current_commit_sha": "abc1234",
      "staged_version": "v0.0.2",
      "ready_updates": [
        {"version": "v0.0.2", "activation": "requires_activate_update"}
      ]
    }

Side effects:
    None. It reads local state files only.
"""

from __future__ import annotations

import json
from typing import Any

from hermes_runtime import __version__
from hermes_runtime.update_check import read_last_result


def _read_staged_metadata(ctx: Any) -> dict[str, Any] | None:
    try:
        payload = json.loads(ctx.staged_metadata_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _activation_state(ctx: Any) -> str:
    marker = getattr(ctx, "activation_marker", None)
    if marker is not None:
        try:
            if marker.exists():
                return "after_runtime_restart"
        except OSError:
            pass
    return "requires_activate_update"


async def run(ctx: Any, _command: dict[str, Any]) -> dict[str, Any]:
    staged_version = ctx.staged_version()
    staged_metadata = _read_staged_metadata(ctx)
    ready_updates = []
    if staged_version:
        ready_updates.append(
            {
                "version": staged_version,
                "ref": (staged_metadata or {}).get("target_ref") or staged_version,
                "sha": (staged_metadata or {}).get("target_sha"),
                "channel": (staged_metadata or {}).get("channel"),
                "staged_at_unix": (staged_metadata or {}).get("staged_at_unix"),
                "activation": _activation_state(ctx),
            }
        )
    return {
        "schema": "tinyhat_hermes_update_status_v1",
        "runtime_code_version": __version__,
        # The runtime release currently active on this Computer.
        "current_version": ctx.current_version(),
        "current_commit_sha": ctx.current_commit_sha(),
        "staged_version": staged_version,
        "ready_updates": ready_updates,
        "last_update_check": read_last_result(ctx.state_dir),
    }
