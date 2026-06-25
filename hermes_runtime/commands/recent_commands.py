"""Return recent local runtime command ledger entries."""

from __future__ import annotations

from typing import Any

from hermes_runtime.local_ledger import report


async def run(ctx: Any, command: dict[str, Any]) -> dict[str, Any]:
    spec = command.get("spec")
    limit = 50
    if isinstance(spec, dict):
        try:
            limit = int(spec.get("limit") or limit)
        except (TypeError, ValueError):
            limit = 50
    return report(state_dir=ctx.state_dir, limit=max(1, min(limit, 200)))
