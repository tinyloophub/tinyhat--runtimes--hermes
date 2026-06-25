"""Check whether the configured runtime update target has changed.

What it does:
    Resolves the requested update target, compares it with the version or
    commit currently installed on the Computer, writes the latest check result
    to local state, and reports the result to the platform update-check API.

Update flow map:
    [pick target release]
        -> check_update     look only; writes updates/last_check.json
        -> stage_update     prepare selected ref; current runtime keeps running
        -> activate_update  request tinyhat-hermes-runtime.service restart
        -> service startup  promote staged ref into current/VERSION

    The new version is used after the tinyhat Hermes runtime service restarts.
    Activating does not reboot the VPS and does not require restarting the
    Hermes framework separately.

When to use it:
    Use this from Hat admin when you want an immediate answer instead of
    waiting for the once-a-day scheduled update check. LTS/latest checks should
    point at the matching channel or final release tag. Dev and RC tags should
    use the custom channel.

Example input:
    {"kind": "check_update", "spec": {"channel": "lts", "target_ref": "v0.0.2"}}

Example output:
    {
      "message": "update check complete",
      "channel": "lts",
      "target_ref": "v0.0.2",
      "current_version": "v0.0.1",
      "update_available": true,
      "report_delivered": true
    }

Side effects:
    Writes ``updates/last_check.json`` and best-effort posts the same result to
    the platform. A platform delivery failure is reported in the command result
    but does not turn a successful local check into a failed command. It does
    not download, stage, activate, or restart anything.
"""

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
    report_delivered = True
    report_error = None
    try:
        await ctx.platform.post_json(
            context_computer_api_path(ctx, "update-check-results/v1"),
            {"result": result},
        )
    except Exception as exc:
        report_delivered = False
        report_error = str(exc)[:300]
    result_detail = result.get("message") if isinstance(result.get("message"), str) else None
    return {
        **result,
        "message": "update check complete",
        "detail": result_detail,
        "report_delivered": report_delivered,
        "report_error": report_error,
    }
