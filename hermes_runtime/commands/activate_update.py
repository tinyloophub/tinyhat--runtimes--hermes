"""Request activation of the staged runtime version."""

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
