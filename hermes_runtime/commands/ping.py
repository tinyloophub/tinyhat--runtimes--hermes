"""Return a minimal liveness proof."""

from __future__ import annotations

from typing import Any


async def run(_ctx: Any, _command: dict[str, Any]) -> dict[str, Any]:
    return {"message": "pong"}
