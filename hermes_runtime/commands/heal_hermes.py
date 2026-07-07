"""Repair the local Hermes messaging runtime without fetching new secrets.

The command is intentionally bounded to already-configured Computers. It reloads
Tinyhat-managed env files, verifies that Telegram credentials are present, and
then uses the same durable gateway start path as ``start_hermes``. It does not
call the Telegram setup endpoint, mint bot tokens, unassign the Computer, or
restart the Tinyhat runtime service.
"""

from __future__ import annotations

import os
from typing import Any

from hermes_runtime.commands import start_hermes
from hermes_runtime.commands.configure_telegram import _env_file_candidates
from hermes_runtime.hermes_cli import find_hermes_binary, probe_hermes_status
from hermes_runtime.runtime_env import read_env_values

TELEGRAM_ENV_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USERS",
    "TELEGRAM_HOME_CHANNEL",
)


def _telegram_env_status() -> dict[str, Any]:
    values = read_env_values(_env_file_candidates(), names=TELEGRAM_ENV_KEYS)
    present = {
        key: bool((os.getenv(key) or values.get(key) or "").strip())
        for key in TELEGRAM_ENV_KEYS
    }
    return {
        "configured": bool(present["TELEGRAM_BOT_TOKEN"]),
        "present_keys": sorted(key for key, value in present.items() if value),
        "missing_keys": sorted(key for key, value in present.items() if not value),
    }


async def run(ctx: Any, command: dict[str, Any]) -> dict[str, Any]:
    telegram = _telegram_env_status()
    hermes_bin = find_hermes_binary()
    if hermes_bin is None:
        return {
            "schema": "tinyhat_hermes_heal_v1",
            "healthy": False,
            "healed": False,
            "reason": "hermes_cli_missing",
            "telegram": telegram,
            "gateway": None,
            "hermes": await probe_hermes_status(),
            "message": "Hermes CLI was not found; run install_hermes first.",
        }
    if not telegram["configured"]:
        return {
            "schema": "tinyhat_hermes_heal_v1",
            "healthy": False,
            "healed": False,
            "reason": "telegram_not_configured",
            "telegram": telegram,
            "gateway": None,
            "hermes": await probe_hermes_status(),
            "message": "Telegram is not configured; run configure_telegram first.",
        }

    start_result = await start_hermes.run(
        ctx,
        {
            "kind": "start_hermes",
            "spec": {
                "reason": (
                    command.get("spec", {}).get("reason")
                    if isinstance(command.get("spec"), dict)
                    else None
                )
                or "heal_hermes"
            },
        },
    )
    healthy = bool(start_result.get("healthy"))
    return {
        "schema": "tinyhat_hermes_heal_v1",
        "healthy": healthy,
        "healed": healthy,
        "reason": "gateway_healthy" if healthy else "gateway_unhealthy",
        "telegram": telegram,
        "gateway": start_result.get("gateway"),
        "hermes": start_result.get("hermes"),
        "env_reload": start_result.get("env_reload"),
        "start_hermes": start_result,
        "message": (
            "Hermes Telegram gateway is healthy."
            if healthy
            else "Hermes Telegram gateway did not report healthy after repair."
        ),
    }
