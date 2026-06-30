"""List Tinyhat-managed Hermes secrets without revealing plaintext."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from hermes_runtime.commands.apply_config import ENV_NAME_RE
from hermes_runtime.commands.apply_config import RUNTIME_SECRETS_END
from hermes_runtime.commands.apply_config import RUNTIME_SECRETS_START
from hermes_runtime.commands.configure_telegram import _env_file_candidates
from hermes_runtime.runtime_env import parse_env_value


SCHEMA = "tinyhat_hermes_secrets_masked_v1"
MASKED_VALUE = "********"


def _mask_secret_value(value: str) -> str:
    if not value:
        return ""
    return MASKED_VALUE


def _read_managed_secret_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    in_managed_block = False
    lines = path.read_text(encoding="utf-8").splitlines()
    for line in lines:
        clean = line.strip()
        if clean == RUNTIME_SECRETS_START:
            in_managed_block = True
            continue
        if clean == RUNTIME_SECRETS_END:
            in_managed_block = False
            continue
        if not in_managed_block or not clean or clean.startswith("#") or "=" not in clean:
            continue
        if clean.startswith("export "):
            clean = clean[len("export ") :].lstrip()
        key, raw_value = clean.split("=", 1)
        key = key.strip()
        if key and ENV_NAME_RE.fullmatch(key):
            values[key] = parse_env_value(raw_value)
    return values


def _env_file_snapshot(path: Path) -> tuple[dict[str, Any], dict[str, str]]:
    expanded = path.expanduser()
    if not expanded.exists():
        return (
            {
                "path": str(expanded),
                "exists": False,
                "readable": False,
                "keys": [],
                "count": 0,
            },
            {},
        )
    try:
        values = _read_managed_secret_values(expanded)
    except OSError as exc:
        return (
            {
                "path": str(expanded),
                "exists": True,
                "readable": False,
                "error": exc.__class__.__name__,
                "keys": [],
                "count": 0,
            },
            {},
        )
    return (
        {
            "path": str(expanded),
            "exists": True,
            "readable": True,
            "keys": sorted(values),
            "count": len(values),
        },
        values,
    )


async def run(_ctx: Any, _command: dict[str, Any]) -> dict[str, Any]:
    env_files: list[dict[str, Any]] = []
    latest_values: dict[str, str] = {}
    source_files: dict[str, list[str]] = {}
    source_conflicts: set[str] = set()

    for env_path in _env_file_candidates():
        snapshot, values = _env_file_snapshot(env_path)
        env_files.append(snapshot)
        path = str(Path(snapshot["path"]))
        for name, value in values.items():
            if name in latest_values and latest_values[name] != value:
                source_conflicts.add(name)
            latest_values[name] = value
            source_files.setdefault(name, []).append(path)

    secrets: list[dict[str, Any]] = []
    for name in sorted(latest_values):
        value = latest_values[name]
        process_present = name in os.environ
        secrets.append(
            {
                "name": name,
                "masked_value": _mask_secret_value(value),
                "value_present": bool(value),
                "source_files": source_files.get(name, []),
                "source_count": len(source_files.get(name, [])),
                "source_conflict": name in source_conflicts,
                "available_in_process": process_present,
                "process_value_matches_managed": (
                    os.environ.get(name) == value if process_present else None
                ),
            }
        )

    return {
        "schema": SCHEMA,
        "secret_count": len(secrets),
        "secrets": secrets,
        "env_files": env_files,
        "values_masked": True,
        "diagnostic": f"found {len(secrets)} Tinyhat-managed Hermes secret(s)",
    }
