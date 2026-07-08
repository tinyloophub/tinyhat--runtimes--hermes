"""Tinyhat-owned gateway desired-state markers.

The Hermes CLI owns the gateway process. Tinyhat only records operator intent
that is outside the Hermes CLI contract, such as "do not auto-heal a gateway
that the platform deliberately stopped before parking an agent."
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hermes_runtime.local_ledger import utc_now_iso

MARKER_FILE = "gateway_desired_stopped.json"


def marker_path(state_dir: Path) -> Path:
    return state_dir / "gateway" / MARKER_FILE


def read_desired_stopped(state_dir: Path) -> dict[str, Any] | None:
    path = marker_path(state_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def mark_desired_stopped(
    state_dir: Path,
    *,
    reason: str,
    command_kind: str | None = None,
) -> dict[str, Any]:
    payload = {
        "state": "stopped",
        "reason": reason,
        "command_kind": command_kind,
        "recorded_at": utc_now_iso(),
    }
    path = marker_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload


def clear_desired_stopped(state_dir: Path) -> bool:
    try:
        marker_path(state_dir).unlink()
    except FileNotFoundError:
        return False
    return True
