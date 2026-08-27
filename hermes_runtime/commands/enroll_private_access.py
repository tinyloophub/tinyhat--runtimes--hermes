"""Enroll this Computer in Tinyhat's private network."""

from __future__ import annotations

import asyncio
from typing import Any

from hermes_runtime import private_access
from hermes_runtime.platform_paths import context_computer_api_path

SCHEMA = "tinyhat_hermes_private_access_enrollment_v1"


async def run(ctx: Any, command: dict[str, Any]) -> dict[str, Any]:
    """Pull one-time enrollment material directly and apply it locally."""

    payload = await ctx.platform.get_json(
        context_computer_api_path(ctx, "private-access/enrollment")
    )
    enrollment = await asyncio.to_thread(private_access.enroll_from_payload, payload)
    report = await asyncio.to_thread(private_access.private_access_report)
    state = str(report.get("state") or "").strip().lower()
    tailnet_ip = str(report.get("tailnet_ip") or "").strip()
    if state != "ready" or not tailnet_ip:
        diagnostic = str(
            report.get("diagnostic")
            or enrollment.get("diagnostic")
            or "private access enrollment did not become ready"
        )[:500]
        raise RuntimeError(diagnostic)
    spec = command.get("spec") if isinstance(command.get("spec"), dict) else {}
    return {
        "schema": SCHEMA,
        "provider": "tailscale",
        "reason": spec.get("reason") or "private_access_enrollment",
        "enrollment": enrollment,
        "private_access": report,
        "restart_requested": False,
        "systemd_restart_requested": False,
    }
