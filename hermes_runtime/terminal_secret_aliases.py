"""Maintain Tinyhat-managed Hermes terminal secret aliases.

Hermes intentionally strips provider/tool credentials such as ``EXA_API_KEY``
from terminal child processes. Its local terminal backend has an audited
``_HERMES_FORCE_<NAME>`` caller opt-in: the alias is consumed by Hermes and the
child receives only ``<NAME>``.

Tinyhat writes aliases only for env names saved through the encrypted secret
handoff/runtime-secret flows, and stores them in the same Computer-local
Hermes env files that already contain the plaintext secret.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any, Iterable

from hermes_runtime.runtime_env import env_file_candidates, read_env_values

FORCE_PREFIX = "_HERMES_FORCE_"
ALIAS_START = "# tinyhat terminal secret aliases start"
ALIAS_END = "# tinyhat terminal secret aliases end"
ALIAS_SCHEMA = "tinyhat_hermes_terminal_secret_aliases_v1"
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def force_alias_name(name: str) -> str:
    return f"{FORCE_PREFIX}{name}"


def _quote_env(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _clean_names(names: Iterable[str]) -> list[str]:
    clean: list[str] = []
    for raw in names:
        name = str(raw or "").strip()
        if not name:
            continue
        if not ENV_NAME_RE.fullmatch(name):
            raise ValueError(
                "Secret names must look like EXA_API_KEY (letters, digits, underscores)."
            )
        if name not in clean:
            clean.append(name)
    return clean


def _remove_alias_block(lines: list[str]) -> tuple[list[str], bool]:
    next_lines: list[str] = []
    in_block = False
    removed = False
    for line in lines:
        if line.strip() == ALIAS_START:
            in_block = True
            removed = True
            continue
        if line.strip() == ALIAS_END:
            in_block = False
            continue
        if in_block:
            continue
        next_lines.append(line)
    return next_lines, removed


def _write_alias_block(path: Path, values: dict[str, str]) -> dict[str, Any]:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    next_lines, removed_existing = _remove_alias_block(lines)

    while next_lines and not next_lines[-1].strip():
        next_lines.pop()
    if values:
        if next_lines:
            next_lines.append("")
        next_lines.append(ALIAS_START)
        for name in sorted(values):
            next_lines.append(f"{force_alias_name(name)}={_quote_env(values[name])}")
        next_lines.append(ALIAS_END)

    next_text = "\n".join(next_lines).rstrip() + "\n"
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    changed = before != next_text
    if changed:
        path.write_text(next_text, encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass

    return {
        "path": str(path),
        "updated": changed,
        "removed_existing_block": removed_existing,
        "alias_names": [force_alias_name(name) for name in sorted(values)],
        "count": len(values),
    }


def sync_terminal_secret_aliases(
    names: Iterable[str],
    *,
    remove_names: Iterable[str] = (),
    env_paths: Iterable[Path] | None = None,
) -> dict[str, Any]:
    requested = _clean_names(names)
    removals = _clean_names(remove_names)
    paths = list(env_paths or env_file_candidates())
    values = read_env_values(paths, names=requested)

    for removed in removals:
        os.environ.pop(force_alias_name(removed), None)
    for name, value in values.items():
        os.environ[force_alias_name(name)] = value

    files = [_write_alias_block(path, values) for path in paths]
    return {
        "schema": ALIAS_SCHEMA,
        "requested_names": requested,
        "aliased_names": sorted(values),
        "alias_names": [force_alias_name(name) for name in sorted(values)],
        "missing_names": [name for name in requested if name not in values],
        "removed_names": removals,
        "env_files": files,
    }
