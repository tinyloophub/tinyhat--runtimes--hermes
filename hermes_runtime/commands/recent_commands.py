"""Return recent local runtime command ledger entries.

What it does:
    Reads the runtime's small local command ledger and returns the newest
    command entries. This lets a maintainer compare what the platform thinks
    happened with what the Computer recorded locally.

When to use it:
    Use this from Hat admin or on the Computer when debugging command delivery,
    runtime restarts, or update checks.

Example input:
    {"kind": "recent_commands", "spec": {"limit": 10}}

Example output:
    {
      "count": 1,
      "commands": [{"command_id": "cmd-1", "kind": "ping", "status": "applied"}]
    }

Side effects:
    None. It reads ``commands/ledger.jsonl`` only.
"""

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
