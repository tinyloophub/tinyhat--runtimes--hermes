"""Bounded local state for recovering Tinyhat plugin result settlement."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib import parse


PLUGIN_REPAIR_SCHEMA = "tinyhat_hermes_plugin_repair_v1"
PLUGIN_REPAIR_FILE = "pending_plugin_repair.json"
PLUGIN_REPAIR_PENDING = "repair_pending"
PLUGIN_INSTALLED_UNACKNOWLEDGED = "installed_unacknowledged"
MAX_PLUGIN_SETTLEMENT_MARKER_BYTES = 16_384
MAX_PLUGIN_NOTICE_ATTEMPTS = 3
FULL_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def _read_bounded_json(path: Path, *, max_bytes: int) -> dict[str, Any] | None:
    """Read a small state record without letting it inflate a public report."""

    try:
        with path.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
    except OSError:
        return None
    if len(raw) > max_bytes:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return payload if isinstance(payload, dict) else None


def _valid_plugin_recovery_marker(payload: Any) -> bool:
    """Recognize only a durable, exact-target plugin recovery marker."""

    if not isinstance(payload, dict):
        return False
    if payload.get("schema") != PLUGIN_REPAIR_SCHEMA or payload.get("state") not in {
        PLUGIN_REPAIR_PENDING,
        PLUGIN_INSTALLED_UNACKNOWLEDGED,
    }:
        return False

    repo_url = payload.get("plugin_repo_url")
    if not isinstance(repo_url, str) or not repo_url or len(repo_url) > 2_048:
        return False
    try:
        parsed_repo = parse.urlsplit(repo_url)
    except ValueError:
        return False
    if (
        parsed_repo.scheme.lower() != "https"
        or not parsed_repo.hostname
        or parsed_repo.username
        or parsed_repo.password
        or parsed_repo.query
        or parsed_repo.fragment
    ):
        return False

    plugin_ref = payload.get("plugin_ref")
    if (
        not isinstance(plugin_ref, str)
        or not plugin_ref
        or len(plugin_ref) > 512
        or any(char in plugin_ref for char in ("\x00", "\r", "\n"))
    ):
        return False
    target_commit = payload.get("target_commit")
    if (
        not isinstance(target_commit, str)
        or FULL_GIT_SHA_RE.fullmatch(target_commit) is None
    ):
        return False

    if payload.get("state") == PLUGIN_REPAIR_PENDING:
        return True

    attempts = payload.get("notice_attempts", 0)
    if (
        isinstance(attempts, bool)
        or not isinstance(attempts, int)
        or not 0 <= attempts <= MAX_PLUGIN_NOTICE_ATTEMPTS
    ):
        return False
    outcome = payload.get("notice_outcome")
    if outcome is not None:
        if not isinstance(outcome, dict) or not isinstance(outcome.get("sent"), bool):
            return False
        http_status = outcome.get("http_status")
        if http_status is not None and (
            isinstance(http_status, bool) or not isinstance(http_status, int)
        ):
            return False
    return True


def read_plugin_recovery(state_dir: Path) -> dict[str, Any] | None:
    """Return one validated local recovery marker without projecting it."""

    marker = _read_bounded_json(
        state_dir / "updates" / PLUGIN_REPAIR_FILE,
        max_bytes=MAX_PLUGIN_SETTLEMENT_MARKER_BYTES,
    )
    return marker if _valid_plugin_recovery_marker(marker) else None


def plugin_update_recovery_pending(state_dir: Path) -> bool:
    """Return a non-sensitive boolean for valid pending plugin recovery."""

    return read_plugin_recovery(state_dir) is not None
