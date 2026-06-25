"""Check whether the configured runtime update target has changed."""

from __future__ import annotations

from typing import Any

from hermes_runtime.platform_paths import context_computer_api_path
from hermes_runtime.update_check import run_update_check


async def run(ctx: Any, command: dict[str, Any]) -> dict[str, Any]:
    spec = command.get("spec")
    current_sha = None
    if hasattr(ctx, "current_commit_sha"):
        current_sha = ctx.current_commit_sha()
    result = await run_update_check(
        state_dir=ctx.state_dir,
        current_version=ctx.current_version(),
        current_sha=current_sha,
        spec=spec if isinstance(spec, dict) else {},
        reason="admin_check_update",
    )
    await ctx.platform.post_json(
        context_computer_api_path(ctx, "update-check-results/v1"),
        {"result": result},
    )
    return {"message": "update check complete", **result}
