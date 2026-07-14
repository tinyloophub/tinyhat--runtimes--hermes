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
import http.client
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, parse, request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from hermes_runtime.plugin_manager import (
    public_plugin_target_error,
    tinyhat_plugin_status,
)


REPO = "tinyloophub/tinyhat--runtimes--hermes"
GITHUB_API_BASE = f"https://api.github.com/repos/{REPO}"
DEFAULT_CHECK_TIME = "02:35"
DEFAULT_CHECK_TIMEZONE = "America/Los_Angeles"
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
SCHEDULED_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FINAL_RELEASE_RE = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)$"
)
FULL_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
MAX_RUN_REASON_LENGTH = 64
PENDING_SCHEDULED_RESULT_FILE = "pending_scheduled_check.json"
PLUGIN_UPDATE_CHECK_SCHEMA = "tinyhat_hermes_plugin_update_check_v1"
SCHEDULED_PLUGIN_UPDATE_CHECK_SCHEMA = "tinyhat_hermes_plugin_update_check_v2"


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


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    _write_text_atomic(
        path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def read_last_result(state_dir: Path) -> dict[str, Any] | None:
    return _read_json(state_dir / "updates" / "last_check.json")


def read_scheduled_result_for_retry(
    *,
    state_dir: Path,
    date_key: str,
) -> dict[str, Any] | None:
    """Return only the saved result for this exact scheduled local day."""

    payload = _read_json(state_dir / "updates" / PENDING_SCHEDULED_RESULT_FILE)
    expected_run_id = f"scheduled:{date_key}"
    if not isinstance(payload, dict):
        return None
    if payload.get("schema") != "tinyhat_hermes_update_check_v1":
        return None
    if payload.get("reason") != "scheduled":
        return None
    if payload.get("run_id") != expected_run_id:
        return None
    if payload.get("scheduled_local_date") != date_key:
        return None
    return payload


def clear_scheduled_result_for_retry(*, state_dir: Path, date_key: str) -> None:
    if read_scheduled_result_for_retry(state_dir=state_dir, date_key=date_key) is None:
        return
    (state_dir / "updates" / PENDING_SCHEDULED_RESULT_FILE).unlink(missing_ok=True)


def read_config(state_dir: Path) -> UpdateCheckConfig:
    config_dir = state_dir / "config"
    local_time = _read_file(config_dir / "update_check_time") or DEFAULT_CHECK_TIME
    if TIME_RE.fullmatch(local_time) is None:
        local_time = DEFAULT_CHECK_TIME

    timezone = (
        _read_file(config_dir / "update_check_timezone") or DEFAULT_CHECK_TIMEZONE
    )
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
    _write_text_atomic(path, date_key + "\n")


def _bounded_run_reason(reason: Any) -> str:
    clean = str(reason or "scheduled").strip() or "scheduled"
    clean = "".join(char for char in clean if char not in {"\x00", "\r", "\n"})
    return clean[:MAX_RUN_REASON_LENGTH] or "scheduled"


def _scheduled_run_metadata(
    *,
    config: UpdateCheckConfig,
    reason: str,
    scheduled_local_date: str | None,
) -> dict[str, str]:
    date_key = str(scheduled_local_date or "").strip()
    if not date_key:
        date_key = (
            datetime.now(timezone.utc)
            .astimezone(ZoneInfo(config.timezone))
            .date()
            .isoformat()
        )
    if SCHEDULED_DATE_RE.fullmatch(date_key) is None:
        raise ValueError("scheduled_local_date must use YYYY-MM-DD")
    try:
        datetime.strptime(date_key, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("scheduled_local_date must be a valid local date") from exc
    return {
        "run_id": f"{reason}:{date_key}",
        "scheduled_local_date": date_key,
    }


def _bounded_scheduled_text(value: Any, *, max_length: int) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:max_length]


def _scheduled_text_fields(
    value: Any,
    fields: dict[str, int],
) -> dict[str, str | None]:
    source = value if isinstance(value, dict) else {}
    return {
        key: _bounded_scheduled_text(source.get(key), max_length=max_length)
        for key, max_length in fields.items()
    }


def _scheduled_plugin_repo_url(value: Any) -> str | None:
    """Return only a credential-free HTTPS plugin source for platform reports."""

    if not isinstance(value, str):
        return None
    try:
        parsed = parse.urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return "<redacted-plugin-source>"
    if parsed.scheme.lower() != "https" or not hostname:
        return "<redacted-plugin-source>"
    hostname = hostname.lower()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    netloc = hostname if port is None else f"{hostname}:{port}"
    return parse.urlunsplit(("https", netloc, parsed.path, "", ""))[:2_048]


def _scheduled_plugin_source(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "repo_url": _scheduled_plugin_repo_url(value.get("repo_url")),
        **_scheduled_text_fields(value, {"ref": 512, "commit": 40}),
    }


def _scheduled_plugin_report(value: Any) -> dict[str, Any]:
    """Project plugin status onto the public, bounded scheduled-report schema."""

    status = value if isinstance(value, dict) else {}
    installed_value = status.get("installed")
    installed = installed_value if isinstance(installed_value, dict) else {}
    target_value = status.get("target")
    target = target_value if isinstance(target_value, dict) else None
    selection_value = status.get("target_selection")
    selection = selection_value if isinstance(selection_value, dict) else {}

    installed_report: dict[str, Any] = {
        **_scheduled_text_fields(
            installed,
            {"name": 128, "manifest_name": 128, "version": 256},
        ),
        "installed": bool(installed.get("installed")),
        "source": _scheduled_plugin_source(installed.get("source")),
    }
    target_report = (
        {
            "repo_url": _scheduled_plugin_repo_url(target.get("repo_url")),
            **_scheduled_text_fields(
                target,
                {
                    "ref": 512,
                    "commit": 40,
                    "requested_commit": 40,
                    "version": 256,
                    "manifest_name": 128,
                },
            ),
        }
        if target is not None
        else None
    )
    return {
        **_scheduled_text_fields(
            status,
            {
                "plugin_name": 128,
                "plugin_ref": 512,
                "installed_version": 256,
                "installed_commit": 40,
                "target_version": 256,
                "target_commit": 40,
                "decision": 128,
                "checked_at": 64,
            },
        ),
        "plugin_repo_url": _scheduled_plugin_repo_url(status.get("plugin_repo_url")),
        "target_selection": {
            **_scheduled_text_fields(
                selection,
                {"source": 64, "plugin_ref": 512},
            ),
            "plugin_repo_url": _scheduled_plugin_repo_url(
                selection.get("plugin_repo_url")
            ),
        },
        "installed": installed_report,
        "target": target_report,
        "update_available": (
            status.get("update_available")
            if isinstance(status.get("update_available"), bool)
            else None
        ),
        "target_error": (
            "Plugin target could not be resolved"
            if status.get("target_error")
            else None
        ),
    }


def _manual_plugin_error(
    exc: Exception,
    *,
    command_spec: dict[str, Any],
) -> str:
    repo_value = (
        command_spec.get("plugin_repo_url")
        or command_spec.get("repo_url")
        or os.getenv("TINYHAT_PLUGIN_REPO_URL")
    )
    repo_url = repo_value if isinstance(repo_value, str) else ""
    detail = public_plugin_target_error(exc, repo_url=repo_url).strip()
    if not detail:
        detail = "plugin update check failed"
    return f"{type(exc).__name__}: {detail}"[:500]


def _plugin_check_spec(command_spec: dict[str, Any]) -> dict[str, Any]:
    """Project only plugin target fields out of a combined update spec.

    ``target_sha`` names the runtime commit in a combined update command, while
    ``target_commit`` names the plugin commit. Standalone plugin commands do not
    use this projection helper, so a runtime SHA must never be copied into the
    plugin target.
    """

    plugin_keys = (
        "plugin_name",
        "plugin_repo_url",
        "repo_url",
        "plugin_ref",
        "ref",
        "target_commit",
    )
    plugin_spec = {key: command_spec[key] for key in plugin_keys if key in command_spec}
    return plugin_spec


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
    except (error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        return {
            "ok": False,
            "status": "unavailable",
            "message": str(getattr(exc, "reason", exc)),
        }
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {
            "ok": False,
            "status": "malformed",
            "message": "GitHub returned a malformed commit response",
        }
    sha = str(payload.get("sha") or "").strip() if isinstance(payload, dict) else ""
    if FULL_GIT_SHA_RE.fullmatch(sha) is None:
        sha = ""
    return {
        "ok": bool(sha),
        "status": "ok" if sha else "malformed",
        "sha": sha or None,
        "html_url": payload.get("html_url") if isinstance(payload, dict) else None,
    }


def _resolve_channel_final_target(channel_ref: str) -> dict[str, Any]:
    """Resolve a moving channel's root VERSION to an immutable final tag."""

    encoded_ref = parse.quote(channel_ref, safe="/")
    req = request.Request(
        f"https://raw.githubusercontent.com/{REPO}/{encoded_ref}/VERSION",
        headers={"User-Agent": "tinyhat-hermes-runtime-update-check/0.0.1"},
        method="GET",
    )
    try:
        with request.urlopen(req, timeout=20) as response:
            raw_version = response.read(128).decode("utf-8", errors="replace").strip()
    except error.HTTPError as exc:
        return {
            "ok": False,
            "status": "channel_version_unavailable",
            "http_status": exc.code,
            "message": "Channel VERSION could not be resolved",
        }
    except (error.URLError, TimeoutError, OSError, http.client.HTTPException):
        return {
            "ok": False,
            "status": "channel_version_unavailable",
            "message": "Channel VERSION could not be resolved",
        }

    version = _final_version_tuple(raw_version)
    if version is None:
        return {
            "ok": False,
            "status": "channel_version_invalid",
            "message": "Channel VERSION is not a final release",
        }
    target_ref = f"v{version[0]}.{version[1]}.{version[2]}"
    resolved = _fetch_github_commit(target_ref)
    if not resolved.get("ok"):
        return {
            **resolved,
            "ok": False,
            "status": "channel_tag_unavailable",
            "message": "Channel release tag could not be resolved",
            "target_ref": target_ref,
        }
    return {**resolved, "target_ref": target_ref}


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
    current_code_version: str | None = None,
    target_ref: str,
) -> bool | None:
    current = _final_version_tuple(current_version)
    if current is None:
        current = _final_version_tuple(current_code_version)
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
    current_code_version: str | None,
    current_sha: str | None,
) -> dict[str, Any]:
    channel_eligible = _is_channel_eligible_target(
        channel=channel,
        target_ref=target_ref,
    )
    final_version_is_newer = _is_strictly_newer_final(
        current_version=current_version,
        current_code_version=current_code_version,
        target_ref=target_ref,
    )
    current_matches_target = _versions_match(current_version, target_ref)
    if not current_matches_target:
        current_matches_target = _versions_match(current_code_version, target_ref)
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
    current_code_version: str | None = None,
    current_sha: str | None = None,
    spec: dict[str, Any] | None = None,
    reason: str = "scheduled",
    scheduled_local_date: str | None = None,
    include_plugin_check: bool = True,
) -> dict[str, Any]:
    config = read_config(state_dir)
    command_spec = spec if isinstance(spec, dict) else {}
    bounded_reason = _bounded_run_reason(reason)
    channel = (
        str(command_spec.get("channel") or config.channel or "lts").strip() or "lts"
    )
    requested_target_ref = str(
        command_spec.get("target_ref") or config.target_ref or ""
    ).strip()
    if not requested_target_ref and channel in {"lts", "latest"}:
        requested_target_ref = f"channels/{channel}"
    if not requested_target_ref:
        raise ValueError("check_update requires target_ref for custom update checks")
    target_ref = requested_target_ref

    supplied_target_sha = str(command_spec.get("target_sha") or "").strip()
    if supplied_target_sha:
        if FULL_GIT_SHA_RE.fullmatch(supplied_target_sha) is None:
            raise ValueError("target_sha must be a full git commit sha")
        resolved = {
            "ok": True,
            "status": "provided_target_sha",
            "sha": supplied_target_sha.lower(),
            "html_url": None,
        }
    elif os.getenv("TINYHAT_LOCAL_DEV_TOKEN"):
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
    elif _is_channel_selector(channel=channel, target_ref=target_ref):
        resolved = await asyncio.to_thread(
            _resolve_channel_final_target,
            target_ref,
        )
        concrete_target_ref = str(resolved.get("target_ref") or "").strip()
        if concrete_target_ref:
            target_ref = concrete_target_ref
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
        current_code_version=current_code_version,
        current_sha=current_sha,
    )
    result = {
        "schema": "tinyhat_hermes_update_check_v1",
        "reason": bounded_reason,
        "status": resolved.get("status") or "unknown",
        "repo": REPO,
        "channel": channel,
        "requested_target_ref": requested_target_ref,
        "target_ref": target_ref,
        "target_sha": target_sha,
        "target_url": resolved.get("html_url"),
        "current_version": current_version,
        "current_code_version": current_code_version,
        "current_sha": current_sha,
        **decision,
        "checked_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
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
    if bounded_reason == "scheduled":
        result.update(
            _scheduled_run_metadata(
                config=config,
                reason=bounded_reason,
                scheduled_local_date=scheduled_local_date,
            )
        )
    if resolved.get("message"):
        result["message"] = resolved.get("message")
    if not resolved.get("ok"):
        result["http_status"] = resolved.get("http_status")
    if include_plugin_check:
        try:
            plugin_status = await tinyhat_plugin_status(
                {"spec": _plugin_check_spec(command_spec)}
            )
            if bounded_reason == "scheduled":
                plugin_status = _scheduled_plugin_report(plugin_status)
            result["plugin_update_check"] = {
                "schema": (
                    SCHEDULED_PLUGIN_UPDATE_CHECK_SCHEMA
                    if bounded_reason == "scheduled"
                    else PLUGIN_UPDATE_CHECK_SCHEMA
                ),
                **plugin_status,
            }
        except Exception as exc:
            plugin_error = (
                f"{type(exc).__name__}: plugin update check failed"
                if bounded_reason == "scheduled"
                else _manual_plugin_error(exc, command_spec=command_spec)
            )
            result["plugin_update_check"] = {
                "schema": (
                    SCHEDULED_PLUGIN_UPDATE_CHECK_SCHEMA
                    if bounded_reason == "scheduled"
                    else PLUGIN_UPDATE_CHECK_SCHEMA
                ),
                "update_available": None,
                "decision": "target_unavailable",
                "error": plugin_error,
                "checked_at": datetime.now(timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z"),
            }
    if bounded_reason == "scheduled":
        _write_json(
            state_dir / "updates" / PENDING_SCHEDULED_RESULT_FILE,
            result,
        )
    _write_json(state_dir / "updates" / "last_check.json", result)
    return result
