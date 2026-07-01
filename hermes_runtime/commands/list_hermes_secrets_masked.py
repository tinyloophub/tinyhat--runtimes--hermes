"""List Tinyhat-managed Hermes secrets without revealing plaintext."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from hermes_runtime.commands.apply_config import ENV_NAME_RE
from hermes_runtime.commands.apply_config import RUNTIME_SECRETS_END
from hermes_runtime.commands.apply_config import RUNTIME_SECRETS_START
from hermes_runtime.commands.configure_telegram import _env_file_candidates
from hermes_runtime.hermes_cli import find_hermes_binary
from hermes_runtime.hermes_cli import run_process
from hermes_runtime.runtime_env import parse_env_value


SCHEMA = "tinyhat_hermes_secrets_masked_v1"
MASKED_VALUE = "********"
HERMES_ENV_PATH_TIMEOUT_SECONDS = 10


def _mask_secret_value(value: str) -> str:
    if not value:
        return ""
    return MASKED_VALUE


def _read_env_values(path: Path, *, managed_only: bool) -> dict[str, str]:
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
        if managed_only and not in_managed_block:
            continue
        if not clean or clean.startswith("#") or "=" not in clean:
            continue
        if clean.startswith("export "):
            clean = clean[len("export ") :].lstrip()
        key, raw_value = clean.split("=", 1)
        key = key.strip()
        if key and ENV_NAME_RE.fullmatch(key):
            values[key] = parse_env_value(raw_value)
    return values


def _read_managed_secret_values(path: Path) -> dict[str, str]:
    return _read_env_values(path, managed_only=True)


def _read_hermes_env_values(path: Path) -> dict[str, str]:
    return _read_env_values(path, managed_only=False)


def _path_key(path: Path) -> str:
    return str(path.expanduser())


async def _hermes_env_path_candidate() -> tuple[Path | None, dict[str, Any] | None]:
    hermes_bin = find_hermes_binary()
    if hermes_bin is None:
        return None, {"available": False, "path": None, "ok": False}
    result = await run_process(
        [str(hermes_bin), "config", "env-path"],
        timeout_seconds=HERMES_ENV_PATH_TIMEOUT_SECONDS,
    )
    stdout = str(result.get("stdout") or "")
    candidate = next((line.strip() for line in stdout.splitlines() if line.strip()), "")
    probe = {
        "available": True,
        "ok": bool(result.get("ok")) and bool(candidate),
        "path": candidate or None,
        "returncode": result.get("returncode"),
        "duration_ms": result.get("duration_ms"),
    }
    if not probe["ok"]:
        probe["stderr"] = str(result.get("stderr") or "")[:500] or None
        return None, probe
    return Path(candidate), probe


async def _env_file_candidates_with_hermes() -> tuple[list[Path], dict[str, Any] | None]:
    paths: list[Path] = []
    seen: set[str] = set()
    hermes_env_path, hermes_probe = await _hermes_env_path_candidate()
    for path in [hermes_env_path, *_env_file_candidates()]:
        if path is None:
            continue
        key = _path_key(path)
        if key in seen:
            continue
        seen.add(key)
        paths.append(path)
    return paths, hermes_probe


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
        values = _read_hermes_env_values(expanded)
        managed_values = _read_managed_secret_values(expanded)
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
            "managed_keys": sorted(managed_values),
            "managed_count": len(managed_values),
        },
        values,
    )


async def run(_ctx: Any, _command: dict[str, Any]) -> dict[str, Any]:
    env_files: list[dict[str, Any]] = []
    latest_values: dict[str, str] = {}
    source_files: dict[str, list[str]] = {}
    managed_source_files: dict[str, list[str]] = {}
    source_conflicts: set[str] = set()

    env_paths, hermes_env_path = await _env_file_candidates_with_hermes()
    for env_path in env_paths:
        snapshot, values = _env_file_snapshot(env_path)
        env_files.append(snapshot)
        path = str(Path(snapshot["path"]))
        managed_keys = {
            str(name)
            for name in snapshot.get("managed_keys", [])
            if isinstance(name, str)
        }
        for name, value in values.items():
            if name in latest_values and latest_values[name] != value:
                source_conflicts.add(name)
            latest_values[name] = value
            source_files.setdefault(name, []).append(path)
            if name in managed_keys:
                managed_source_files.setdefault(name, []).append(path)

    env_names = sorted(latest_values)
    secrets: list[dict[str, Any]] = []
    for name in env_names:
        value = latest_values[name]
        process_present = name in os.environ
        managed_files = managed_source_files.get(name, [])
        secrets.append(
            {
                "name": name,
                "masked_value": _mask_secret_value(value),
                "value_present": bool(value),
                "source_files": source_files.get(name, []),
                "source_count": len(source_files.get(name, [])),
                "source_conflict": name in source_conflicts,
                "managed_by_tinyhat": bool(managed_files),
                "managed_source_files": managed_files,
                "available_in_process": process_present,
                "process_value_matches_managed": (
                    os.environ.get(name) == value if process_present else None
                ),
            }
        )

    return {
        "schema": SCHEMA,
        "env_count": len(env_names),
        "env_names": env_names,
        "secret_count": len(secrets),
        "secrets": secrets,
        "env_files": env_files,
        "hermes_env_path": hermes_env_path,
        "values_masked": True,
        "diagnostic": f"found {len(secrets)} Hermes env secret(s)",
    }
