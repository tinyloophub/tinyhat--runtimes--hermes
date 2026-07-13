"""Helpers for applying Hermes env-file changes in the current process."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable

# Markers for the env-file block that Tinyhat writes when the platform syncs
# runtime secrets (``apply_config``). The terminal env export module reads the
# same markers, so writer and reader cannot drift.
RUNTIME_SECRETS_START = "# tinyhat runtime secrets start"
RUNTIME_SECRETS_END = "# tinyhat runtime secrets end"


def hermes_home() -> Path:
    raw = (
        os.getenv("TINYHAT_HERMES_HOME")
        or os.getenv("HERMES_HOME")
        or str(Path.home() / ".hermes")
    )
    return Path(raw).expanduser()


def env_file_candidates() -> list[Path]:
    """Return the Hermes env files Tinyhat manages, in precedence order.

    ``hermes config set`` and the Hermes CLI write ``<hermes home>/.env``;
    the project checkout may carry its own ``.env``. Hermes' own loader
    (``run_agent``) loads the home file first and does not let the project
    file override it, so the first file that defines a name wins here too.
    """
    candidates: list[Path] = []
    explicit = (os.getenv("HERMES_ENV_FILE") or "").strip()
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(hermes_home() / ".env")

    project_dir = Path(
        (os.getenv("HERMES_PROJECT_DIR") or "/usr/local/lib/hermes-agent").strip()
    )
    if project_dir.exists():
        candidates.append(project_dir / ".env")

    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path.expanduser())
        if key not in seen:
            unique.append(path)
            seen.add(key)
    return unique


def parse_env_value(raw: str) -> str:
    value = raw.strip()
    if (
        len(value) >= 2
        and value[0] == value[-1]
        and value.startswith(("'", '"'))
    ):
        value = value[1:-1]
    return value.replace('\\"', '"').replace("\\\\", "\\")


def _parse_env_line(line: str) -> tuple[str, str] | None:
    clean = line.strip()
    if not clean or clean.startswith("#") or "=" not in clean:
        return None
    if clean.startswith("export "):
        clean = clean[len("export ") :].lstrip()
    key, raw_value = clean.split("=", 1)
    key = key.strip()
    if not key:
        return None
    return key, raw_value


def read_managed_secret_names(lines: Iterable[str]) -> set[str]:
    """Return env names inside the Tinyhat runtime-secrets managed block."""
    names: set[str] = set()
    in_managed_block = False
    for line in lines:
        clean = line.strip()
        if clean == RUNTIME_SECRETS_START:
            in_managed_block = True
            continue
        if clean == RUNTIME_SECRETS_END:
            in_managed_block = False
            continue
        if not in_managed_block:
            continue
        parsed = _parse_env_line(line)
        if parsed is not None:
            names.add(parsed[0])
    return names


def read_env_values(
    paths: Iterable[Path],
    *,
    names: Iterable[str] | None = None,
) -> dict[str, str]:
    """Read selected env-file values without touching ``os.environ``.

    The first file that defines a name wins (matching Hermes' own loader);
    within one file the last assignment wins (matching shell sourcing).
    """
    selected = {str(name) for name in names} if names is not None else None
    values: dict[str, str] = {}
    for raw_path in paths:
        path = raw_path.expanduser()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        file_values: dict[str, str] = {}
        for line in lines:
            parsed = _parse_env_line(line)
            if parsed is None:
                continue
            key, raw_value = parsed
            if selected is not None and key not in selected:
                continue
            file_values[key] = parse_env_value(raw_value)
        for key, value in file_values.items():
            values.setdefault(key, value)
    return values


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
        file_values: dict[str, str] = {}
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
            # Match read_env_values and Hermes itself: the last assignment in
            # one file wins, while the first file defining a name wins across
            # files. The old line-by-line loop accidentally let a lower-
            # precedence project .env overwrite the Hermes home .env.
            file_values[key] = parse_env_value(raw_value)
        for key, value in file_values.items():
            if key in loaded_keys:
                continue
            os.environ[key] = value
            loaded_keys.add(key)
    return {
        "loaded": True,
        "keys": sorted(loaded_keys),
        "count": len(loaded_keys),
        "files": read_files,
        "missing_files": missing_files,
    }
