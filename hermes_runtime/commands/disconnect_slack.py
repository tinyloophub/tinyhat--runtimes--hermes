"""Revoke and remove one complete Hermes Slack connection bundle.

The platform sends only generation identifiers. The runtime reads the bot
token locally, asks Slack to revoke that token when possible, removes every
Slack environment value together, and restarts Hermes before reporting a
verified disconnect. Results never contain credential values.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib import error, request

from hermes_runtime.commands import heal_hermes
from hermes_runtime.commands.remove_private_secret import _remove_name_from_env_file
from hermes_runtime.runtime_env import env_file_candidates, read_env_values
from hermes_runtime.terminal_env_passthrough import sync_terminal_env_passthrough
from hermes_runtime.terminal_secret_aliases import force_alias_name

SCHEMA = "tinyhat_hermes_disconnect_slack_v1"
SLACK_AUTH_REVOKE_URL = "https://slack.com/api/auth.revoke"
SLACK_ENV_NAMES = (
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_ALLOWED_USERS",
    "SLACK_HOME_CHANNEL",
    "SLACK_HOME_CHANNEL_NAME",
)
ALREADY_REVOKED_ERRORS = frozenset(
    {
        "account_inactive",
        "invalid_auth",
        "not_authed",
        "token_expired",
        "token_revoked",
    }
)
SAFE_REVOCATION_ERRORS = ALREADY_REVOKED_ERRORS | frozenset(
    {
        "access_denied",
        "accesslimited",
        "enterprise_is_restricted",
        "fatal_error",
        "internal_error",
        "not_allowed_token_type",
        "org_login_required",
        "ratelimited",
        "request_timeout",
        "service_unavailable",
        "team_access_not_granted",
    }
)


def _revoke_slack_bot_access(token: str | None) -> dict[str, Any]:
    """Return only allowlisted revocation state, never the token or raw body."""

    clean_token = str(token or "").strip()
    if not clean_token:
        return {"status": "not_present", "confirmed": False}
    api_request = request.Request(
        SLACK_AUTH_REVOKE_URL,
        data=b"",
        method="POST",
        headers={
            "Authorization": f"Bearer {clean_token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with request.urlopen(api_request, timeout=15) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, error.URLError):
        return {"status": "unconfirmed", "confirmed": False}
    if not isinstance(payload, dict):
        return {"status": "unconfirmed", "confirmed": False}
    if payload.get("ok") is True and payload.get("revoked") is True:
        return {"status": "revoked", "confirmed": True}
    error_code = str(payload.get("error") or "").strip()
    if error_code in ALREADY_REVOKED_ERRORS:
        return {
            "status": "already_revoked",
            "confirmed": True,
            "error_code": error_code,
        }
    result: dict[str, Any] = {"status": "unconfirmed", "confirmed": False}
    if error_code in SAFE_REVOCATION_ERRORS:
        result["error_code"] = error_code
    return result


async def run(ctx: Any, command: dict[str, Any]) -> dict[str, Any]:
    spec = command.get("spec") if isinstance(command.get("spec"), dict) else {}
    paths = env_file_candidates()
    values = read_env_values(paths, names=["SLACK_BOT_TOKEN"])
    bot_token = os.environ.get("SLACK_BOT_TOKEN") or values.get("SLACK_BOT_TOKEN")
    slack_access = _revoke_slack_bot_access(bot_token)

    env_files: list[dict[str, Any]] = []
    for path in paths:
        updates = [_remove_name_from_env_file(path, name) for name in SLACK_ENV_NAMES]
        env_files.append(
            {
                "path": str(path.expanduser()),
                "updated": any(item["updated"] for item in updates),
                "removed": any(item["removed"] for item in updates),
            }
        )

    process_names = {
        candidate
        for name in SLACK_ENV_NAMES
        for candidate in (name, force_alias_name(name))
    }
    process_values_cleared = sorted(name for name in process_names if name in os.environ)
    for name in process_names:
        os.environ.pop(name, None)
    terminal_env_passthrough = sync_terminal_env_passthrough(
        [],
        remove_names=list(SLACK_ENV_NAMES),
    )
    remaining = read_env_values(paths, names=SLACK_ENV_NAMES)
    local_bundle_absent = not any(name in remaining for name in SLACK_ENV_NAMES)
    if not local_bundle_absent:
        raise RuntimeError("Hermes still reports Slack configuration after removal.")

    gateway = await heal_hermes.run(
        ctx,
        {
            "kind": "heal_hermes",
            "spec": {
                "reason": "slack_connection_removed",
                "restart": True,
                "deadline_seconds": 90,
            },
        },
    )
    gateway_ready = bool(gateway.get("healthy"))
    disconnect_verified = local_bundle_absent and gateway_ready

    return {
        "schema": SCHEMA,
        "handoff_public_id": str(spec.get("handoff_public_id") or ""),
        "removal_request_id": str(spec.get("removal_request_id") or ""),
        "local_bundle_absent": local_bundle_absent,
        "disconnect_verified": disconnect_verified,
        "slack_access": slack_access,
        "removed_env_names": list(SLACK_ENV_NAMES),
        "removed_from_files": any(item["removed"] for item in env_files),
        "process_values_cleared": process_values_cleared,
        "env_files": env_files,
        "terminal_env_passthrough": terminal_env_passthrough,
        "gateway": gateway,
        "gateway_ready": gateway_ready,
        "restart_requested": True,
    }
