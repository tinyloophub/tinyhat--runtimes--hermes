"""Return the platform attestation result for this runtime."""

from __future__ import annotations

from typing import Any


async def run(ctx: Any, _command: dict[str, Any]) -> dict[str, Any]:
    attestation = await ctx.platform.get_json("/hapi/v1/computers/local-dev/whoami")
    return {"attestation": attestation}
