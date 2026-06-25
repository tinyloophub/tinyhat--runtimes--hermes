"""Request activation of the staged runtime version.

What it does:
    Marks the staged runtime version for activation and asks the runtime process
    to exit after reporting success. The process manager then restarts it, and
    startup moves the staged version into ``current``.

Update flow map:
    [pick target release]
        -> check_update     look only; writes updates/last_check.json
        -> stage_update     prepare selected ref; current runtime keeps running
        -> activate_update  request tinyhat-hermes-runtime.service restart
        -> service startup  promote staged ref into current/VERSION

    This command is the switch step. After its result is reported, the tinyhat
    Hermes runtime service exits, systemd restarts that service, and startup
    promotes staged -> current. It does not reboot the VPS and does not require
    restarting the Hermes framework separately.

When to use it:
    Use this from Hat admin only after ``stage_update`` or another safe staging
    path has prepared a version you want to run.

Example input:
    {"kind": "activate_update", "spec": {}}

Example output:
    {
      "message": "activation requested for v0.0.2",
      "target_version": "v0.0.2",
      "activation": "after_runtime_restart"
    }

Side effects:
    Writes ``ACTIVATE_ON_RESTART`` and requests a runtime restart after the
    command result is reported.
"""

from __future__ import annotations

from typing import Any


async def run(ctx: Any, _command: dict[str, Any]) -> dict[str, Any]:
    staged = ctx.staged_version()
    if not staged:
        raise ValueError("no staged runtime version is available")
    ctx.activation_marker.write_text(staged + "\n", encoding="utf-8")
    ctx.restart_requested = True
    return {
        "message": f"activation requested for {staged}",
        "target_version": staged,
        "activation": "after_runtime_restart",
    }
