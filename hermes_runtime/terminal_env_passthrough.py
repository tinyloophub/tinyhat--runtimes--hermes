"""Register Tinyhat secret names with Hermes' terminal env passthrough.

Hermes owns the security boundary for values that enter terminal/code
subprocesses. Tinyhat therefore records only env names in Hermes'
``terminal.env_passthrough`` config when Hermes permits that name. Hermes-managed
provider/tool credentials such as ``EXA_API_KEY`` stay in Hermes' main process
after the gateway reloads its ``.env``; they are not forced into child shells.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterable

from hermes_runtime.runtime_env import hermes_home

ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PASSTHROUGH_SCHEMA = "tinyhat_hermes_terminal_env_passthrough_v1"

# Fallback mirror of Hermes' provider/tool credential blocklist for the names
# Tinyhat is most likely to receive. When Hermes is installed, its own helper is
# the authority; this set only keeps local tests and partial installs safe.
_FALLBACK_PROTECTED_NAMES = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_TOKEN",
    "BRAVE_SEARCH_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "COHERE_API_KEY",
    "DAYTONA_API_KEY",
    "DEEPSEEK_API_KEY",
    "EMAIL_PASSWORD",
    "EXA_API_KEY",
    "FAL_KEY",
    "FIRECRAWL_API_KEY",
    "FIRECRAWL_API_URL",
    "FIREWORKS_API_KEY",
    "GH_TOKEN",
    "GITHUB_APP_ID",
    "GITHUB_APP_INSTALLATION_ID",
    "GITHUB_APP_PRIVATE_KEY_PATH",
    "GOOGLE_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GROQ_API_KEY",
    "HASS_TOKEN",
    "HELICONE_API_KEY",
    "MISTRAL_API_KEY",
    "MODAL_TOKEN_ID",
    "MODAL_TOKEN_SECRET",
    "OPENAI_API_BASE",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_ORG_ID",
    "OPENAI_ORGANIZATION",
    "OPENROUTER_API_KEY",
    "PARALLEL_API_KEY",
    "PERPLEXITY_API_KEY",
    "TAVILY_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TOGETHER_API_KEY",
    "VERTEX_CREDENTIALS_PATH",
    "VOICE_TOOLS_OPENAI_KEY",
    "XAI_API_KEY",
}


def _hermes_config_file() -> Path:
    explicit = (os.getenv("HERMES_CONFIG_FILE") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return hermes_home() / "config.yaml"


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


def _hermes_blocks_passthrough(name: str) -> bool:
    try:
        from tools.env_passthrough import _is_hermes_provider_credential

        return bool(_is_hermes_provider_credential(name))
    except Exception:  # noqa: BLE001 - partial Hermes installs use fallback set.
        if name in _FALLBACK_PROTECTED_NAMES:
            return True
        return (
            name.startswith("AUXILIARY_")
            and (name.endswith("_API_KEY") or name.endswith("_BASE_URL"))
        ) or (
            name.startswith("GATEWAY_RELAY_")
            and (name.endswith("_SECRET") or name.endswith("_KEY") or name.endswith("_TOKEN"))
        )


def _find_terminal_key(
    lines: list[str],
    start: int,
    end: int,
    key: str,
) -> tuple[int, int, list[str]] | None:
    key_prefix = f"{key}:"
    for index in range(start + 1, end):
        stripped = lines[index].strip()
        if not stripped.startswith(key_prefix):
            continue
        prefix, _sep, raw_value = lines[index].partition(":")
        inline = _inline_list_items(raw_value)
        if inline is not None:
            return index, index + 1, inline

        items: list[str] = []
        stop = index + 1
        while stop < end:
            line = lines[stop]
            stripped_child = line.strip()
            if not stripped_child:
                stop += 1
                continue
            if not line.startswith("    "):
                break
            if stripped_child.startswith("- "):
                item = stripped_child[2:].strip().strip("'\"")
                if item:
                    items.append(item)
            stop += 1
        return index, stop, items
    return None


def _render_terminal_list(key: str, items: Iterable[str]) -> list[str]:
    unique: list[str] = []
    for item in items:
        clean = str(item).strip()
        if clean and clean not in unique:
            unique.append(clean)
    if not unique:
        return [f"  {key}: []"]
    return [f"  {key}:"] + [f"    - {item}" for item in unique]


def _edit_terminal_list(
    text: str,
    *,
    key: str,
    add_items: Iterable[str] = (),
    remove_items: Iterable[str] = (),
) -> tuple[str, bool, list[str]]:
    lines = text.splitlines()
    add = list(add_items)
    remove = set(remove_items)
    changed = False

    bounds = _terminal_block_bounds(lines)
    if bounds is None:
        if not add:
            return text if text.endswith("\n") or not text else text + "\n", False, []
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("terminal:")
        lines.extend(_render_terminal_list(key, add))
        return "\n".join(lines).rstrip() + "\n", True, add

    start, end = bounds
    found = _find_terminal_key(lines, start, end, key)
    if found is None:
        if not add:
            return text if text.endswith("\n") or not text else text + "\n", False, []
        insertion = _render_terminal_list(key, add)
        lines[start + 1:start + 1] = insertion
        return "\n".join(lines).rstrip() + "\n", True, add

    key_start, key_end, existing = found
    next_items = [item for item in existing if item not in remove]
    for item in add:
        if item not in next_items:
            next_items.append(item)
    changed = next_items != existing
    if not changed:
        return text if text.endswith("\n") or not text else text + "\n", False, next_items
    lines[key_start:key_end] = _render_terminal_list(key, next_items)
    return "\n".join(lines).rstrip() + "\n", True, next_items


def _update_config_list(
    *,
    key: str,
    add_items: Iterable[str] = (),
    remove_items: Iterable[str] = (),
) -> dict[str, Any]:
    config_file = _hermes_config_file()
    config_file.parent.mkdir(parents=True, exist_ok=True)
    before = config_file.read_text(encoding="utf-8") if config_file.exists() else ""
    after, changed, items = _edit_terminal_list(
        before,
        key=key,
        add_items=list(add_items),
        remove_items=list(remove_items),
    )
    if changed:
        config_file.write_text(after, encoding="utf-8")
    return {
        "config_file": str(config_file),
        "updated": bool(changed),
        "path": f"terminal.{key}",
        "names": items,
    }


def sync_terminal_env_passthrough(
    names: Iterable[str],
    *,
    remove_names: Iterable[str] = (),
) -> dict[str, Any]:
    requested = _clean_names(names)
    removals = _clean_names(remove_names)
    registered: list[str] = []
    skipped: list[dict[str, str]] = []
    for name in requested:
        if _hermes_blocks_passthrough(name):
            skipped.append(
                {
                    "name": name,
                    "reason": "hermes_protected_credential",
                    "message": (
                        "Hermes keeps this provider/tool credential in the main "
                        "agent process and refuses terminal env passthrough."
                    ),
                }
            )
        else:
            registered.append(name)

    config = _update_config_list(
        key="env_passthrough",
        add_items=registered,
        remove_items=removals,
    )
    return {
        "schema": PASSTHROUGH_SCHEMA,
        "requested_names": requested,
        "registered_names": registered,
        "skipped_names": skipped,
        "removed_names": removals,
        "config": config,
    }


def register_name(name: str) -> dict[str, Any]:
    return sync_terminal_env_passthrough([name])


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0] if args else "register"
    if command == "register":
        if len(args) != 2:
            print("usage: terminal_env_passthrough register <ENV_NAME>", file=sys.stderr)
            return 2
        try:
            result = register_name(args[1])
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps(result, sort_keys=True))
        return 0
    print(f"unknown terminal_env_passthrough command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover - exercised via CLI tests
    raise SystemExit(main())
