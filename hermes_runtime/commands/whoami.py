"""Return the platform attestation result for this runtime."""

from __future__ import annotations

from typing import Any

from hermes_runtime.platform_paths import context_computer_api_path


async def run(ctx: Any, _command: dict[str, Any]) -> dict[str, Any]:
    attestation = await ctx.platform.get_json(context_computer_api_path(ctx, "whoami"))
    return {"attestation": attestation}
