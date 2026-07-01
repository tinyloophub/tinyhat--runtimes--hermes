"""Install Tinyhat's Hermes terminal env reload hook.

Hermes reloads ``~/.hermes/.env`` into the gateway process between turns, but
fresh terminal sessions capture their own login-shell snapshot. This hook uses
Hermes' public ``terminal.shell_init_files`` config surface so new terminal
snapshots also export values from the Hermes env files.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any

from hermes_runtime.plugin_manager import hermes_home

HOOK_COMMENT = "# Tinyhat-managed: export Hermes env files into terminal snapshots."
HOOK_RELATIVE_PATH = ("tinyhat", "terminal-env.sh")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def terminal_env_hook_path() -> Path:
    return hermes_home().joinpath(*HOOK_RELATIVE_PATH)


def _hermes_config_file() -> Path:
    explicit = (os.getenv("HERMES_CONFIG_FILE") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return hermes_home() / "config.yaml"


def _hook_script() -> str:
    return "\n".join(
        [
            HOOK_COMMENT,
            "__tinyhat_source_env_file() {",
            '  [ -r "$1" ] || return 0',
            '  case "$-" in *a*) __tinyhat_had_allexport=1 ;; *) __tinyhat_had_allexport=0 ;; esac',
            "  set -a",
            '  . "$1"',
            '  [ "$__tinyhat_had_allexport" = "1" ] || set +a',
            "}",
            '__tinyhat_hermes_home="${TINYHAT_HERMES_HOME:-${HERMES_HOME:-$HOME/.hermes}}"',
            '__tinyhat_source_env_file "${HERMES_ENV_FILE:-$__tinyhat_hermes_home/.env}"',
            '__tinyhat_source_env_file "${HERMES_PROJECT_DIR:-/usr/local/lib/hermes-agent}/.env"',
            "unset -f __tinyhat_source_env_file 2>/dev/null || true",
            "unset __tinyhat_had_allexport __tinyhat_hermes_home 2>/dev/null || true",
            "",
        ]
    )


def _write_hook_file(path: Path) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    desired = _hook_script()
    before = path.read_text(encoding="utf-8") if path.exists() else None
    changed = before != desired
    if changed:
        path.write_text(desired, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return {"path": str(path), "updated": changed, "exists": True}


def _is_top_level(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not line.startswith((" ", "\t")) and not stripped.startswith("#")


def _terminal_block_bounds(lines: list[str]) -> tuple[int, int] | None:
    start = None
    for index, line in enumerate(lines):
        if _is_top_level(line) and line.strip() == "terminal:":
            start = index
            break
    if start is None:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if _is_top_level(lines[index]):
            end = index
            break
    return start, end


def _inline_list_items(raw: str) -> list[str] | None:
    value = raw.strip()
    if value == "":
        return None
    if value in {"[]", "null", "None"}:
        return []
    if not (value.startswith("[") and value.endswith("]")):
        return None
    inner = value[1:-1].strip()
    if not inner:
        return []
    items: list[str] = []
    for item in inner.split(","):
        clean = item.strip().strip("'\"")
        if clean:
            items.append(clean)
    return items


def _normalize_env_names(names: list[str] | tuple[str, ...] | None) -> list[str]:
    if not names:
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw_name in names:
        name = str(raw_name or "").strip()
        if not name:
            continue
        if not ENV_NAME_RE.fullmatch(name):
            raise ValueError("Tinyhat runtime secret names must be valid env names.")
        if name not in seen:
            seen.add(name)
            cleaned.append(name)
    return sorted(cleaned)


def _with_trailing_newline(text: str) -> str:
    return text if text.endswith("\n") or not text else text + "\n"


def _ensure_terminal_list_items(
    text: str,
    key: str,
    values: list[str],
) -> tuple[str, bool, list[str], int]:
    values = [value for value in values if value]
    lines = text.splitlines()
    if not values:
        return _with_trailing_newline(text), False, [], 0

    bounds = _terminal_block_bounds(lines)
    if bounds is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(["terminal:", f"  {key}:"])
        lines.extend(f"    - {value}" for value in values)
        return "\n".join(lines).rstrip() + "\n", True, values, len(values)

    start, end = bounds
    key_index = None
    for index in range(start + 1, end):
        stripped = lines[index].strip()
        if stripped.startswith(f"{key}:"):
            key_index = index
            break

    if key_index is None:
        block = [f"  {key}:"]
        block.extend(f"    - {value}" for value in values)
        lines[start + 1:start + 1] = block
        return "\n".join(lines).rstrip() + "\n", True, values, len(values)

    prefix, _sep, raw_value = lines[key_index].partition(":")
    inline_items = _inline_list_items(raw_value)
    if inline_items is not None:
        existing = list(dict.fromkeys(inline_items))
        added = [value for value in values if value not in existing]
        if not added:
            return _with_trailing_newline(text), False, [], len(existing)
        block = [f"{prefix}:"]
        block.extend(f"    - {item}" for item in existing)
        block.extend(f"    - {item}" for item in added)
        lines[key_index:key_index + 1] = block
        return "\n".join(lines).rstrip() + "\n", True, added, len(existing) + len(added)

    existing: list[str] = []
    insert_at = key_index + 1
    while insert_at < end:
        line = lines[insert_at]
        if line.startswith("    - "):
            item = line.strip()[2:].strip().strip("'\"")
            if item:
                existing.append(item)
        elif line.startswith("    ") or not line.strip():
            pass
        else:
            break
        insert_at += 1
    existing = list(dict.fromkeys(existing))
    added = [value for value in values if value not in existing]
    if not added:
        return _with_trailing_newline(text), False, [], len(existing)
    for value in reversed(added):
        lines.insert(insert_at, f"    - {value}")
    return "\n".join(lines).rstrip() + "\n", True, added, len(existing) + len(added)


def _ensure_config_hook(
    config_file: Path,
    hook_path: Path,
    secret_names: list[str],
) -> dict[str, Any]:
    config_file.parent.mkdir(parents=True, exist_ok=True)
    before = config_file.read_text(encoding="utf-8") if config_file.exists() else ""
    after = before
    after, shell_changed, shell_added, shell_count = _ensure_terminal_list_items(
        after,
        "shell_init_files",
        [str(hook_path)],
    )
    after, passthrough_changed, passthrough_added, passthrough_count = (
        _ensure_terminal_list_items(after, "env_passthrough", secret_names)
    )
    after, docker_changed, docker_added, docker_count = _ensure_terminal_list_items(
        after,
        "docker_forward_env",
        secret_names,
    )
    changed = shell_changed or passthrough_changed or docker_changed
    if changed or not config_file.exists():
        config_file.write_text(after, encoding="utf-8")
    paths = ["terminal.shell_init_files"]
    if secret_names:
        paths.extend(["terminal.env_passthrough", "terminal.docker_forward_env"])
    return {
        "config_file": str(config_file),
        "updated": bool(changed),
        "path": "terminal.shell_init_files",
        "paths": paths,
        "shell_init_files": {
            "updated": shell_changed,
            "added": shell_added,
            "count": shell_count,
        },
        "env_passthrough": {
            "updated": passthrough_changed,
            "added": passthrough_added,
            "count": passthrough_count,
        },
        "docker_forward_env": {
            "updated": docker_changed,
            "added": docker_added,
            "count": docker_count,
        },
    }


def install_terminal_env_reload_hook(
    secret_names: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    hook_path = terminal_env_hook_path()
    config_file = _hermes_config_file()
    normalized_secret_names = _normalize_env_names(secret_names)
    hook = _write_hook_file(hook_path)
    config = _ensure_config_hook(config_file, hook_path, normalized_secret_names)
    return {
        "schema": "tinyhat_hermes_terminal_env_hook_v1",
        "installed": True,
        "hook": hook,
        "config": config,
        "terminal_secret_env_names": normalized_secret_names,
    }
