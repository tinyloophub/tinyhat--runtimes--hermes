"""Install Tinyhat's Hermes terminal env reload hook.

Hermes reloads ``~/.hermes/.env`` into the gateway process between turns, but
fresh terminal sessions capture their own login-shell snapshot. This hook uses
Hermes' public ``terminal.shell_init_files`` config surface so new terminal
snapshots also export values from the Hermes env files.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from hermes_runtime.plugin_manager import hermes_home

HOOK_COMMENT = "# Tinyhat-managed: export Hermes env files into terminal snapshots."
HOOK_RELATIVE_PATH = ("tinyhat", "terminal-env.sh")


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
    if value in {"", "[]", "null", "None"}:
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


def _ensure_shell_init_file(text: str, hook_path: str) -> tuple[str, bool]:
    lines = text.splitlines()
    if hook_path in lines or any(line.strip() == f"- {hook_path}" for line in lines):
        return text if text.endswith("\n") or not text else text + "\n", False

    bounds = _terminal_block_bounds(lines)
    if bounds is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(["terminal:", "  shell_init_files:", f"    - {hook_path}"])
        return "\n".join(lines).rstrip() + "\n", True

    start, end = bounds
    shell_index = None
    for index in range(start + 1, end):
        stripped = lines[index].strip()
        if stripped.startswith("shell_init_files:"):
            shell_index = index
            break

    if shell_index is None:
        lines[start + 1:start + 1] = ["  shell_init_files:", f"    - {hook_path}"]
        return "\n".join(lines).rstrip() + "\n", True

    prefix, _sep, raw_value = lines[shell_index].partition(":")
    inline_items = _inline_list_items(raw_value)
    if inline_items is not None:
        block = [f"{prefix}:"]
        block.extend(f"    - {item}" for item in inline_items if item != hook_path)
        block.append(f"    - {hook_path}")
        lines[shell_index:shell_index + 1] = block
        return "\n".join(lines).rstrip() + "\n", True

    insert_at = shell_index + 1
    while insert_at < end:
        line = lines[insert_at]
        if line.startswith("    ") or not line.strip():
            insert_at += 1
            continue
        break
    lines.insert(insert_at, f"    - {hook_path}")
    return "\n".join(lines).rstrip() + "\n", True


def _ensure_config_hook(config_file: Path, hook_path: Path) -> dict[str, Any]:
    config_file.parent.mkdir(parents=True, exist_ok=True)
    before = config_file.read_text(encoding="utf-8") if config_file.exists() else ""
    after, changed = _ensure_shell_init_file(before, str(hook_path))
    if changed or not config_file.exists():
        config_file.write_text(after, encoding="utf-8")
    return {
        "config_file": str(config_file),
        "updated": bool(changed),
        "path": "terminal.shell_init_files",
    }


def install_terminal_env_reload_hook() -> dict[str, Any]:
    hook_path = terminal_env_hook_path()
    config_file = _hermes_config_file()
    hook = _write_hook_file(hook_path)
    config = _ensure_config_hook(config_file, hook_path)
    return {
        "schema": "tinyhat_hermes_terminal_env_hook_v1",
        "installed": True,
        "hook": hook,
        "config": config,
    }
