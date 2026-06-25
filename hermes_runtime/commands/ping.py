"""Return a minimal liveness proof.

What it does:
    Confirms that the runtime command loop can receive a command, run a tiny
    function, and report a result back to the platform.

When to use it:
    Use this from Hat admin after creating a Computer or when checking whether
    command delivery still works.

Example input:
    {"kind": "ping", "spec": {}}

Example output:
    {"message": "pong"}

Side effects:
    None. It only returns ``pong``.
"""

from __future__ import annotations

from typing import Any


async def run(_ctx: Any, _command: dict[str, Any]) -> dict[str, Any]:
    return {"message": "pong"}
