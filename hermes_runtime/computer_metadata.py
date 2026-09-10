"""Save the platform's credential-free Computer metadata in the user's home."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from hermes_runtime.agent_systems import SYSTEM_COMMANDS

SCHEMA = "tinyhat.computer-metadata.v1"
README = """# This Tinyhat Computer

`computer.json` contains the latest timing snapshot received from Tinyhat.
Read it with `cat ~/tinyhat/computer.json` or open this folder in Files.

- `creation`: machine provisioning started, machine launched, first heartbeat,
  and ready for assignment. This time excludes waiting in the warm pool.
- `assignment`: the coding-agent API request started, the warm Computer was
  reserved, and the runtime acknowledged it was ready for its owner.
- `allocation_duration_ms`: request start to reservation.
- `duration_ms`: start to ready, measured in milliseconds.
- All timestamps include their UTC offset. `null` means not yet measured or
  unavailable for an older Computer; it does not mean zero.

Tinyhat replaces computer.json when the platform snapshot changes. Editing this
file does not change the platform's records. It contains no access credentials.
This folder is separate from your projects and from Tinyhat's private config.
Your edits to this README are preserved.
"""


def _time(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 40 or datetime.fromisoformat(value).utcoffset() is None:
        raise ValueError("Invalid metadata timestamp")
    return value


def _duration(value: Any) -> int | None:
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError("Invalid metadata duration")
    return value


def _identifier(value: Any, pattern: str) -> str | None:
    if value is not None and (not isinstance(value, str) or re.fullmatch(pattern, value) is None):
        raise ValueError("Invalid metadata identity")
    return value


def validate(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ValueError("Unknown Computer metadata schema")
    handle = _identifier(value.get("handle"), r"tinyhat/computers/[A-Za-z0-9][A-Za-z0-9_-]{0,109}")
    if handle is None or not isinstance(value.get("creation"), dict):
        raise ValueError("Missing Computer metadata identity or creation")
    creation = value["creation"]
    result = {
        "schema": SCHEMA, "handle": handle, "created_at": _time(value.get("created_at")),
        "creation": {
            **{key: _time(creation.get(key)) for key in ("started_at", "container_launched_at", "first_heartbeat_at", "ready_at")},
            "duration_ms": _duration(creation.get("duration_ms")),
        },
        "assignment": None,
    }
    assignment = value.get("assignment")
    if assignment is not None:
        if not isinstance(assignment, dict) or assignment.get("system") not in {None, *SYSTEM_COMMANDS} or assignment.get("source") not in {None, "warm"}:
            raise ValueError("Invalid Computer assignment metadata")
        result["assignment"] = {
            "computer_id": _identifier(assignment.get("computer_id"), r"cmp_[A-Za-z0-9_-]{32}"),
            "agent_id": _identifier(assignment.get("agent_id"), r"agt_[A-Za-z0-9_-]{22}"),
            "system": assignment.get("system"), "source": assignment.get("source"),
            **{key: _time(assignment.get(key)) for key in ("started_at", "assigned_at", "ready_at")},
            **{key: _duration(assignment.get(key)) for key in ("allocation_duration_ms", "duration_ms")},
        }
    return result


def _write_changed(path: Path, text: str) -> None:
    if path.is_symlink():
        raise OSError("Computer metadata path must not be a symlink")
    try:
        if path.exists() and path.read_text(encoding="utf-8") == text:
            return
    except (OSError, UnicodeError):
        # A damaged/unreadable generated snapshot can still be replaced when
        # its parent is writable. Retry must be able to repair this state.
        pass
    descriptor, temporary = tempfile.mkstemp(prefix=".metadata-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def apply(value: Any, *, home: Path | None = None) -> None:
    """An absent response from an older platform preserves the last snapshot."""
    if value is None:
        return
    content = json.dumps(validate(value), indent=2, sort_keys=True) + "\n"
    directory = (home or Path.home()) / "tinyhat"
    if directory.is_symlink():
        raise OSError("Computer metadata directory must not be a symlink")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_changed(directory / "computer.json", content)
    guide = directory / "README.md"
    if not guide.exists():
        _write_changed(guide, README)
