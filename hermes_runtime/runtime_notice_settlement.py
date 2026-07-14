"""Bounded local state for settling runtime-only Telegram notices."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


RUNTIME_NOTICE_SCHEMA = "tinyhat_hermes_runtime_notice_v1"
RUNTIME_NOTICE_FILE = "pending_runtime_notice.json"
RUNTIME_NOTICE_UNACKNOWLEDGED = "staged_unacknowledged"
MAX_RUNTIME_NOTICE_MARKER_BYTES = 16_384
MAX_RUNTIME_NOTICE_ATTEMPTS = 3
FULL_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def _read_bounded_json(path: Path, *, max_bytes: int) -> dict[str, Any] | None:
    """Read one small record without inflating a scheduled public report."""

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


def _valid_runtime_notice_marker(payload: Any) -> bool:
    """Recognize only an exact-target, bounded runtime notice marker."""

    if not isinstance(payload, dict):
        return False
    if (
        payload.get("schema") != RUNTIME_NOTICE_SCHEMA
        or payload.get("state") != RUNTIME_NOTICE_UNACKNOWLEDGED
    ):
        return False

    target_ref = payload.get("target_ref")
    if (
        not isinstance(target_ref, str)
        or not target_ref
        or len(target_ref) > 512
        or any(char in target_ref for char in ("\x00", "\r", "\n"))
    ):
        return False
    target_sha = payload.get("target_sha")
    if not isinstance(target_sha, str) or FULL_GIT_SHA_RE.fullmatch(target_sha) is None:
        return False

    attempts = payload.get("notice_attempts", 0)
    if (
        isinstance(attempts, bool)
        or not isinstance(attempts, int)
        or not 0 <= attempts <= MAX_RUNTIME_NOTICE_ATTEMPTS
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


def read_runtime_notice(state_dir: Path) -> dict[str, Any] | None:
    """Return one validated local marker without exposing its contents."""

    marker = _read_bounded_json(
        state_dir / "updates" / RUNTIME_NOTICE_FILE,
        max_bytes=MAX_RUNTIME_NOTICE_MARKER_BYTES,
    )
    return marker if _valid_runtime_notice_marker(marker) else None


def runtime_notice_recovery_pending(state_dir: Path) -> bool:
    """Return a non-sensitive boolean for pending runtime notice settlement."""

    return read_runtime_notice(state_dir) is not None
