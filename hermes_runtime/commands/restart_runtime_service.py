"""Restart the Tinyhat Hermes runtime service.

What it does:
    Asks the small Tinyhat runtime process to exit after it reports this command
    result back to the platform. The process manager should then start the
    runtime again. This is useful after ``activate_update`` because startup is
    where a staged runtime version becomes the current version.

Update flow map:
    [pick target release]
        -> check_update             look only; writes updates/last_check.json
        -> stage_update             prepare selected ref; current runtime keeps running
        -> activate_update          mark staged ref for startup promotion
        -> restart_runtime_service  restart this service/process if needed
        -> service startup          promote staged ref into current/VERSION

    The new runtime version is used after the tinyhat Hermes runtime service
    starts again. This command does not reboot the VPS and does not restart the
    Hermes framework separately.

When to use it:
    Use this from Hat admin when you want to restart the Tinyhat runtime process
    itself, for example after an update has been activated. It requires a
    process manager such as systemd or Docker restart policy to bring the
    process back. Without one, the runtime exits and must be started manually.

Example input:
    {"kind": "restart_runtime_service", "spec": {}}

Example output:
    {
      "message": "runtime service restart requested",
      "restart_target": "tinyhat-hermes-runtime.service",
      "effect": "after_command_result"
    }

Side effects:
    Requests a runtime process exit after the command result is reported. No
    state files are changed by this command.
"""

from __future__ import annotations

from typing import Any


async def run(ctx: Any, _command: dict[str, Any]) -> dict[str, Any]:
    ctx.restart_requested = True
    return {
        "message": "runtime service restart requested",
        "restart_target": "tinyhat-hermes-runtime.service",
        "effect": "after_command_result",
    }
