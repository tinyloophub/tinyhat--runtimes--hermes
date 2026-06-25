"""Small local command ledger for the Hermes runtime."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_MAX_AGE_DAYS = 60
DEFAULT_MAX_BYTES = 1024 * 1024
DEFAULT_LIMIT = 50


def ledger_path(state_dir: Path) -> Path:
    return state_dir / "commands" / "ledger.jsonl"


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def append_entry(
    *,
    state_dir: Path,
    command: dict[str, Any],
    status: str,
    phase: str,
    result: dict[str, Any],
    started_at: str,
    completed_at: str,
    failure_code: str | None = None,
) -> dict[str, Any]:
    entry = {
        "schema": "tinyhat_hermes_local_command_v1",
        "command_id": command.get("command_id"),
        "kind": command.get("kind"),
        "status": status,
        "phase": phase,
        "failure_code": failure_code,
        "created_at": command.get("created_at"),
        "started_at": started_at,
        "completed_at": completed_at,
        "spec": command.get("spec") if isinstance(command.get("spec"), dict) else {},
        "result": result,
    }
    path = ledger_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n")
    compact(state_dir=state_dir)
    return entry


def read_entries(*, state_dir: Path, limit: int = DEFAULT_LIMIT) -> list[dict[str, Any]]:
    path = ledger_path(state_dir)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    entries: list[dict[str, Any]] = []
    for line in reversed(lines):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            entries.append(payload)
        if len(entries) >= limit:
            break
    return entries


def compact(
    *,
    state_dir: Path,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> None:
    path = ledger_path(state_dir)
    try:
        stat = path.stat()
    except FileNotFoundError:
        return
    if stat.st_size <= max_bytes:
        return

    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    kept: list[dict[str, Any]] = []
    for entry in read_entries(state_dir=state_dir, limit=2000):
        completed_at = str(entry.get("completed_at") or "")
        try:
            observed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
        except ValueError:
            observed = datetime.now(UTC)
        if observed >= cutoff:
            kept.append(entry)

    kept = kept[:500]
    kept.reverse()
    path.write_text(
        "".join(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n" for entry in kept),
        encoding="utf-8",
    )


def report(*, state_dir: Path, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    entries = read_entries(state_dir=state_dir, limit=limit)
    path = ledger_path(state_dir)
    size_bytes = path.stat().st_size if path.exists() else 0
    return {
        "schema": "tinyhat_hermes_local_command_report_v1",
        "state_dir": str(state_dir),
        "ledger_path": str(path),
        "limit": limit,
        "count": len(entries),
        "size_bytes": size_bytes,
        "commands": entries,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Print recent Hermes runtime commands.")
    parser.add_argument("--state-dir", default="/var/lib/tinyhat-hermes-runtime")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args()
    print(
        json.dumps(
            report(state_dir=Path(args.state_dir), limit=max(1, min(args.limit, 200))),
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
