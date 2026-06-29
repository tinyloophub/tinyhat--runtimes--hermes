"""Report current and locally staged runtime update state.

What it does:
    Returns the runtime code version, the installed runtime release ref, the
    installed commit sha when known, any staged update marker, and the most
    recent update-check result.

Update flow map:
    [pick target release]
        -> check_update             look only; writes updates/last_check.json
        -> stage_update             prepare selected ref; current runtime keeps running
        -> activate_update          mark staged ref and request service restart
        -> restart_runtime_service  optional plain restart; no staging changes
        -> service startup          promote staged ref into current/VERSION

    This command can be run at any point in the flow. It tells you what is
    current now, what is staged, whether that staged ref still needs
    activate_update, and what the latest update check found.

When to use it:
    Use this from Hat admin before or after staging or activating an update to
    see what is currently running and what step is still needed.

Example input:
    {"kind": "update_status", "spec": {}}

Example output:
    {
      "current_version": "v0.0.1",
      "current_commit_sha": "abc1234",
      "staged_version": "v0.0.2",
      "ready_updates": [
        {"version": "v0.0.2", "activation": "requires_activate_update"}
      ]
    }

Side effects:
    None. It reads local state files only.
"""

from __future__ import annotations

import json
import re
from typing import Any

from hermes_runtime import __version__
from hermes_runtime.plugin_manager import DEFAULT_TINYHAT_PLUGIN_NAME, plugin_snapshot
from hermes_runtime.update_check import read_last_result
from hermes_runtime.update_artifacts import staged_package_dir

FINAL_RELEASE_RE = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)$"
)


def _read_staged_metadata(ctx: Any) -> dict[str, Any] | None:
    try:
        payload = json.loads(ctx.staged_metadata_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _activation_state(ctx: Any) -> str:
    marker = getattr(ctx, "activation_marker", None)
    if marker is not None:
        try:
            if marker.exists():
                return "after_runtime_restart"
        except OSError:
            pass
    return "requires_activate_update"


def _read_activation_error(ctx: Any) -> dict[str, Any] | None:
    activation_error_file = getattr(ctx, "activation_error_file", None)
    if activation_error_file is None:
        return None
    try:
        payload = json.loads(activation_error_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _clean_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _final_release_key(value: str | None) -> tuple[int, int, int] | None:
    match = FINAL_RELEASE_RE.fullmatch(value or "")
    if match is None:
        return None
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
    )


def _version_matches(left: str | None, right: str | None) -> bool:
    left = _clean_text(left)
    right = _clean_text(right)
    if not left or not right:
        return False
    if left == right:
        return True
    left_final = _final_release_key(left)
    right_final = _final_release_key(right)
    return left_final is not None and left_final == right_final


def _last_update_check_for_current_state(
    *,
    state_dir: Any,
    current_version: str,
    current_sha: str | None,
) -> dict[str, Any] | None:
    payload = read_last_result(state_dir)
    if payload is None:
        return None

    checked_version = _clean_text(payload.get("current_version"))
    checked_sha = _clean_text(payload.get("current_sha"))
    live_sha = _clean_text(current_sha)
    live_version = _clean_text(current_version)
    stale_reason = None
    if checked_sha is None and checked_version is None:
        stale_reason = "cached_check_missing_current_state"
    elif checked_sha and live_sha and checked_sha != live_sha:
        stale_reason = "current_sha_changed_since_check"
    elif checked_version and not _version_matches(checked_version, live_version):
        stale_reason = "current_version_changed_since_check"

    if stale_reason is None:
        return payload

    stale_payload = dict(payload)
    stale_payload["stale"] = True
    stale_payload["stale_reason"] = stale_reason
    stale_payload["checked_current_version"] = checked_version
    stale_payload["checked_current_sha"] = checked_sha
    stale_payload["live_current_version"] = live_version
    stale_payload["live_current_sha"] = live_sha
    stale_payload["previous_update_available"] = payload.get("update_available")
    stale_payload["update_available"] = None
    if stale_reason == "cached_check_missing_current_state":
        stale_payload["message"] = (
            "Cached update check is stale because it does not record which "
            "installed runtime it checked. Run check_update again for a current "
            "decision."
        )
    else:
        stale_payload["message"] = (
            "Cached update check is stale because the installed runtime changed "
            "after that check. Run check_update again for a current decision."
        )
    return stale_payload


async def run(ctx: Any, _command: dict[str, Any]) -> dict[str, Any]:
    staged_version = ctx.staged_version()
    staged_metadata = _read_staged_metadata(ctx)
    current_version = ctx.current_version()
    current_commit_sha = ctx.current_commit_sha()
    ready_updates = []
    if staged_version:
        code_staged = staged_package_dir(ctx.state_dir).is_dir()
        ready_updates.append(
            {
                "version": staged_version,
                "ref": (staged_metadata or {}).get("target_ref") or staged_version,
                "sha": (staged_metadata or {}).get("target_sha"),
                "channel": (staged_metadata or {}).get("channel"),
                "staged_at_unix": (staged_metadata or {}).get("staged_at_unix"),
                "code_staged": code_staged,
                "activation": _activation_state(ctx),
            }
        )
    last_update_check = _last_update_check_for_current_state(
        state_dir=ctx.state_dir,
        current_version=current_version,
        current_sha=current_commit_sha,
    )
    return {
        "schema": "tinyhat_hermes_update_status_v1",
        "runtime_code_version": __version__,
        # The runtime release currently active on this Computer.
        "current_version": current_version,
        "current_commit_sha": current_commit_sha,
        "staged_version": staged_version,
        "ready_updates": ready_updates,
        "startup_activation_error": _read_activation_error(ctx),
        "last_update_check": last_update_check,
        "plugin": {
            "installed": plugin_snapshot(DEFAULT_TINYHAT_PLUGIN_NAME),
            "last_update_check": (
                last_update_check.get("plugin_update_check")
                if isinstance(last_update_check, dict)
                else None
            ),
        },
    }
