"""Check and stage exact Tinyhat runtime and plugin update targets."""

from __future__ import annotations

from typing import Any

from hermes_runtime.update_orchestrator import check_and_stage_updates


async def run(ctx: Any, command: dict[str, Any]) -> dict[str, Any]:
    spec = command.get("spec")
    if not isinstance(spec, dict):
        raise ValueError("check_and_stage_updates requires a spec object")
    return await check_and_stage_updates(ctx, spec)
