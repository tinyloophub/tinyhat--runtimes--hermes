"""Return the platform attestation result for this runtime.

What it does:
    Calls the platform attestation endpoint and returns what the platform says
    this runtime identity belongs to.

When to use it:
    Use this from Hat admin to prove that the runtime can authenticate as the
    expected Computer. In local development this uses the scoped local-dev
    bearer token. In production this path should use VM identity attestation.

Example input:
    {"kind": "whoami", "spec": {}}

Example output:
    {"attestation": {"verified": true, "computer_id": 123}}

Side effects:
    None on the Computer. It makes one read-only platform API call.
"""

from __future__ import annotations

from typing import Any

from hermes_runtime.platform_paths import context_computer_api_path


async def run(ctx: Any, _command: dict[str, Any]) -> dict[str, Any]:
    attestation = await ctx.platform.get_json(context_computer_api_path(ctx, "whoami"))
    return {"attestation": attestation}
