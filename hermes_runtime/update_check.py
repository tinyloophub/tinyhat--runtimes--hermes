"""Scheduled update discovery for the Tinyhat Hermes runtime.

The runtime checks the target that the platform or local config asks it to
check. For channel checks, such as ``lts`` or ``latest``, "update available" is
based on a concrete final SemVer tag (``vX.Y.Z``) that is newer than the
installed final version. Exact commit equality can only prove that the Computer
already matches the target. A bare channel selector such as ``channels/lts`` is
installable, but it is not enough evidence to report "a newer LTS is available"
because protected channel branches may point at merge commits instead of the
release tag commit. Dev and RC tags are still supported, but they must be
requested through the explicit ``custom`` path so a newer prerelease is never
reported as an available LTS update.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, parse, request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


REPO = "tinyloophub/tinyhat--runtimes--hermes"
GITHUB_API_BASE = f"https://api.github.com/repos/{REPO}"
DEFAULT_CHECK_TIME = "02:35"
DEFAULT_CHECK_TIMEZONE = "America/Los_Angeles"
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
FINAL_RELEASE_RE = re.compile(r"^v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)$")


@dataclass(frozen=True)
class UpdateCheckConfig:
    local_time: str
    timezone: str
    channel: str
    target_ref: str


def _read_file(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return value or None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_last_result(state_dir: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads((state_dir / "updates" / "last_check.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def read_config(state_dir: Path) -> UpdateCheckConfig:
    config_dir = state_dir / "config"
    local_time = _read_file(config_dir / "update_check_time") or DEFAULT_CHECK_TIME
    if TIME_RE.fullmatch(local_time) is None:
        local_time = DEFAULT_CHECK_TIME

    timezone = _read_file(config_dir / "update_check_timezone") or DEFAULT_CHECK_TIMEZONE
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        timezone = DEFAULT_CHECK_TIMEZONE

    channel = _read_file(config_dir / "update_check_channel") or "lts"
    if channel not in {"lts", "latest", "custom"}:
        channel = "lts"

    target_ref = _read_file(config_dir / "update_check_ref")
    if not target_ref:
        target_ref = f"channels/{channel}" if channel in {"lts", "latest"} else ""

    return UpdateCheckConfig(
        local_time=local_time,
        timezone=timezone,
        channel=channel,
        target_ref=target_ref,
    )


def scheduled_check_due(
    *,
    state_dir: Path,
    now_utc: datetime | None = None,
) -> tuple[bool, UpdateCheckConfig, str]:
    config = read_config(state_dir)
    now = (now_utc or datetime.now(timezone.utc)).astimezone(ZoneInfo(config.timezone))
    today_key = now.date().isoformat()
    last_key = _read_file(state_dir / "updates" / "last_scheduled_check_date")
    due_clock = now.strftime("%H:%M") >= config.local_time
    return due_clock and last_key != today_key, config, today_key


def mark_scheduled_check_started(*, state_dir: Path, date_key: str) -> None:
    path = state_dir / "updates" / "last_scheduled_check_date"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(date_key + "\n", encoding="utf-8")


def _fetch_github_commit(ref: str) -> dict[str, Any]:
    encoded = parse.quote(ref, safe="")
    req = request.Request(
        f"{GITHUB_API_BASE}/commits/{encoded}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "tinyhat-hermes-runtime-update-check/0.0.1",
        },
        method="GET",
    )
    try:
        with request.urlopen(req, timeout=20) as response:
            raw = response.read()
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return {
            "ok": False,
            "status": "unavailable",
            "http_status": exc.code,
            "message": detail[:500],
        }
    except error.URLError as exc:
        return {
            "ok": False,
            "status": "unavailable",
            "message": str(exc.reason),
        }
    payload = json.loads(raw.decode("utf-8"))
    sha = str(payload.get("sha") or "").strip() if isinstance(payload, dict) else ""
    return {
        "ok": bool(sha),
        "status": "ok" if sha else "malformed",
        "sha": sha or None,
        "html_url": payload.get("html_url") if isinstance(payload, dict) else None,
    }


def _final_version_tuple(value: str | None) -> tuple[int, int, int] | None:
    match = FINAL_RELEASE_RE.fullmatch(str(value or "").strip())
    if match is None:
        return None
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
    )


def _is_channel_eligible_target(*, channel: str, target_ref: str) -> bool:
    if channel == "custom":
        return True
    if target_ref == f"channels/{channel}":
        return True
    return _final_version_tuple(target_ref) is not None


def _is_channel_selector(*, channel: str, target_ref: str) -> bool:
    return channel in {"lts", "latest"} and target_ref == f"channels/{channel}"


def _versions_match(left: str | None, right: str | None) -> bool:
    clean_left = str(left or "").strip()
    clean_right = str(right or "").strip()
    if clean_left == clean_right and clean_left:
        return True
    left_final = _final_version_tuple(clean_left)
    right_final = _final_version_tuple(clean_right)
    return left_final is not None and left_final == right_final


def _is_strictly_newer_final(
    *,
    current_version: str,
    target_ref: str,
) -> bool | None:
    current = _final_version_tuple(current_version)
    target = _final_version_tuple(target_ref)
    if current is None or target is None:
        return None
    return target > current


def _update_decision(
    *,
    resolved_ok: bool,
    channel: str,
    target_ref: str,
    target_sha: str | None,
    current_version: str,
    current_sha: str | None,
) -> dict[str, Any]:
    channel_eligible = _is_channel_eligible_target(
        channel=channel,
        target_ref=target_ref,
    )
    final_version_is_newer = _is_strictly_newer_final(
        current_version=current_version,
        target_ref=target_ref,
    )
    current_matches_target = _versions_match(current_version, target_ref)
    if not current_matches_target and target_sha and current_sha:
        current_matches_target = target_sha == current_sha
    elif not current_matches_target and target_sha:
        current_matches_target = _versions_match(current_version, target_sha)

    if not resolved_ok:
        decision = "target_unavailable"
        update_available = False
    elif not channel_eligible:
        decision = "target_not_allowed_for_channel"
        update_available = False
    elif current_matches_target:
        decision = "current_matches_target"
        update_available = False
    elif _is_channel_selector(channel=channel, target_ref=target_ref):
        decision = "channel_selector_needs_concrete_release"
        update_available = False
    elif channel in {"lts", "latest"}:
        if final_version_is_newer is True:
            decision = "newer_final_release"
            update_available = True
        elif final_version_is_newer is False:
            decision = "target_final_not_newer"
            update_available = False
        else:
            decision = "target_is_not_final_release"
            update_available = False
    else:
        decision = "custom_target_differs"
        update_available = True

    return {
        "channel_eligible": channel_eligible,
        "target_final_version_is_newer": final_version_is_newer,
        "current_matches_target": current_matches_target,
        "decision": decision,
        "update_available": update_available,
    }


async def run_update_check(
    *,
    state_dir: Path,
    current_version: str,
    current_sha: str | None = None,
    spec: dict[str, Any] | None = None,
    reason: str = "scheduled",
) -> dict[str, Any]:
    config = read_config(state_dir)
    command_spec = spec if isinstance(spec, dict) else {}
    channel = str(command_spec.get("channel") or config.channel or "lts").strip() or "lts"
    target_ref = str(command_spec.get("target_ref") or config.target_ref or "").strip()
    if not target_ref and channel in {"lts", "latest"}:
        target_ref = f"channels/{channel}"
    if not target_ref:
        raise ValueError("check_update requires target_ref for custom update checks")

    if os.getenv("TINYHAT_LOCAL_DEV_TOKEN"):
        resolved = {
            "ok": True,
            "status": "dev_ref_check",
            "sha": None,
            "html_url": None,
            "message": (
                "Local dev update checks compare the platform-supplied ref "
                "with the installed ref without calling GitHub from the "
                "runtime container."
            ),
        }
    else:
        resolved = await asyncio.to_thread(_fetch_github_commit, target_ref)
    target_sha = str(resolved.get("sha") or "").strip() or None
    current_sha = (current_sha or "").strip() or None
    decision = _update_decision(
        resolved_ok=bool(resolved.get("ok")),
        channel=channel,
        target_ref=target_ref,
        target_sha=target_sha,
        current_version=current_version,
        current_sha=current_sha,
    )
    result = {
        "schema": "tinyhat_hermes_update_check_v1",
        "reason": reason,
        "status": resolved.get("status") or "unknown",
        "repo": REPO,
        "channel": channel,
        "target_ref": target_ref,
        "target_sha": target_sha,
        "target_url": resolved.get("html_url"),
        "current_version": current_version,
        "current_sha": current_sha,
        **decision,
        "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "schedule": {
            "time": config.local_time,
            "timezone": config.timezone,
            "config_files": {
                "time": "config/update_check_time",
                "timezone": "config/update_check_timezone",
                "channel": "config/update_check_channel",
                "target_ref": "config/update_check_ref",
            },
        },
    }
    if resolved.get("message"):
        result["message"] = resolved.get("message")
    if not resolved.get("ok"):
        result["http_status"] = resolved.get("http_status")
    _write_json(state_dir / "updates" / "last_check.json", result)
    return result
