"""Helpers for applying Hermes env-file changes in the current process."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable


def parse_env_value(raw: str) -> str:
    value = raw.strip()
    if (
        len(value) >= 2
        and value[0] == value[-1]
        and value.startswith(("'", '"'))
    ):
        value = value[1:-1]
    return value.replace('\\"', '"').replace("\\\\", "\\")


def load_env_files_into_process(
    paths: Iterable[Path],
    *,
    keys: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Load selected env-file keys into ``os.environ``.

    This mirrors the operational ``set -a; . ~/.hermes/.env`` recovery step
    without shelling out or returning secret values to Tinyhat.
    """

    selected_keys = {str(key) for key in keys} if keys is not None else None
    loaded_keys: set[str] = set()
    read_files: list[str] = []
    missing_files: list[str] = []
    for raw_path in paths:
        path = raw_path.expanduser()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            missing_files.append(str(path))
            continue
        except OSError:
            missing_files.append(str(path))
            continue
        read_files.append(str(path))
        for line in lines:
            clean = line.strip()
            if not clean or clean.startswith("#") or "=" not in clean:
                continue
            if clean.startswith("export "):
                clean = clean[len("export ") :].lstrip()
            key, raw_value = clean.split("=", 1)
            key = key.strip()
            if not key:
                continue
            if selected_keys is not None and key not in selected_keys:
                continue
            os.environ[key] = parse_env_value(raw_value)
            loaded_keys.add(key)
    return {
        "loaded": True,
        "keys": sorted(loaded_keys),
        "count": len(loaded_keys),
        "files": read_files,
        "missing_files": missing_files,
    }
