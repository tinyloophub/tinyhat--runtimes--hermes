"""Maintain Tinyhat-managed Hermes terminal secret aliases.

Hermes intentionally strips provider/tool credentials such as ``EXA_API_KEY``
from terminal child processes. Its local terminal backend has a tested
``_HERMES_FORCE_<NAME>`` caller opt-in: the alias is consumed by Hermes and the
child receives only ``<NAME>``. This contract is pinned to upstream Hermes
source commit ``88d1d6206f399c134d1f4c0b7db27733aaa3c50c``:
``tools/environments/local.py`` consumes the prefix, and
``tests/tools/test_local_env_blocklist.py`` documents that callers can opt in
to passing a blocked var with this prefix.

Source:
https://github.com/NousResearch/hermes-agent/blob/88d1d6206f399c134d1f4c0b7db27733aaa3c50c/tools/environments/local.py#L94-L96
Test:
https://github.com/NousResearch/hermes-agent/blob/88d1d6206f399c134d1f4c0b7db27733aaa3c50c/tests/tools/test_local_env_blocklist.py#L241-L253

Tinyhat writes aliases only for env names saved through the encrypted secret
handoff/runtime-secret flows, and stores them in the same Computer-local
Hermes env file that first defines the plaintext secret in Hermes load order.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Iterable

from hermes_runtime.runtime_env import env_file_candidates, parse_env_value

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
                "Secret names must be valid environment variable names "
                "(letters, digits, underscores; start with a letter or underscore)."
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
    if changed or values:
        path.chmod(0o600)

    return {
        "path": str(path),
        "updated": changed,
        "removed_existing_block": removed_existing,
        "alias_names": [force_alias_name(name) for name in sorted(values)],
        "count": len(values),
    }


def _read_path_values(path: Path, names: set[str]) -> dict[str, str]:
    try:
        lines = path.expanduser().read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in lines:
        clean = line.strip()
        if not clean or clean.startswith("#") or "=" not in clean:
            continue
        if clean.startswith("export "):
            clean = clean[len("export ") :].lstrip()
        key, raw_value = clean.split("=", 1)
        key = key.strip()
        if key in names:
            values[key] = parse_env_value(raw_value)
    return values


def _alias_values_by_path(
    paths: list[Path],
    requested: list[str],
) -> tuple[dict[Path, dict[str, str]], dict[str, str]]:
    remaining = set(requested)
    values_by_path: dict[Path, dict[str, str]] = {}
    aliased_values: dict[str, str] = {}
    for path in paths:
        if not remaining:
            values_by_path[path] = {}
            continue
        file_values = _read_path_values(path, remaining)
        if file_values:
            values_by_path[path] = file_values
            for name, value in file_values.items():
                remaining.discard(name)
                aliased_values[name] = value
        else:
            values_by_path[path] = {}
    return values_by_path, aliased_values


def sync_terminal_secret_aliases(
    names: Iterable[str],
    *,
    remove_names: Iterable[str] = (),
    env_paths: Iterable[Path] | None = None,
) -> dict[str, Any]:
    requested = _clean_names(names)
    removals = _clean_names(remove_names)
    paths = list(env_paths or env_file_candidates())
    values_by_path, aliased_values = _alias_values_by_path(paths, requested)

    files = [_write_alias_block(path, values_by_path[path]) for path in paths]
    return {
        "schema": ALIAS_SCHEMA,
        "requested_names": requested,
        "aliased_names": sorted(aliased_values),
        "alias_names": [force_alias_name(name) for name in sorted(aliased_values)],
        "missing_names": [name for name in requested if name not in aliased_values],
        "removed_names": removals,
        "env_files": files,
    }
