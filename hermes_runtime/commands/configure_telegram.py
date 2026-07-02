"""Configure Hermes Agent to receive this Tinyhat Telegram bot.

What it does:
    1. Calls the Tinyhat platform setup endpoint for this Computer:
       ``/hapi/v1/computers/local-dev/hermes/telegram-setup/v1`` in local
       development, or ``/hapi/v1/computers/me/hermes/telegram-setup/v1`` on
       GCloud. That endpoint only returns the Telegram setup payload when the
       Computer is already assigned to the agent and the agent has a short
       setup grant.
    2. Writes Hermes Telegram environment variables and the platform-provided
       OpenRouter key into the normal Hermes env files:
       - ``~/.hermes/.env``
       - ``/usr/local/lib/hermes-agent/.env`` when that project directory
         exists.
    3. Applies the platform-selected model/base URL, OpenRouter-backed
       multilingual STT, a warmed local STT model for local/operator-selected
       mode, and auxiliary vision model through Hermes' public
       ``hermes config set`` command.
    4. Installs Tinyhat-managed Hermes quick commands and a tiny Hermes plugin
       for the agent settings Mini App and OpenAI Codex device-code auth:
       ``/tinyhat_settings`` opens the Tinyhat settings Mini App.
       ``/codex_auth``, ``/codex_auth_status``, ``/codex_auth_log``, and
       ``/codex_limits``.
       It also installs ``codex-auth`` as a best-effort Hermes quick-command
       alias for typed chat input, while Telegram's command menu uses
       underscores because Telegram clients and the Bot API do not reliably
       handle hyphenated slash commands. The plugin registers the same
       underscore command names through Hermes' documented
       ``ctx.register_command`` interface so Hermes' own Telegram BotCommand
       menu can discover them. These commands run only after Telegram is
       configured because they need a Telegram channel for the device code.
    5. Configures Hermes' own Telegram command-menu priority so Tinyhat
       plugin commands stay visible in Telegram's slash-command picker while
       Hermes still owns the full menu registration.
    6. Sets Telegram's default **configure** Mini App button to the same
       Tinyhat settings page.
    7. Clears Telegram's webhook for the bot so Hermes long-polling can own
       the bot connection.
    8. Starts the Hermes gateway using the public ``hermes gateway`` command.
       Hermes auto-registers the Telegram command menu from its central slash
       command registry, plugin/skill commands, and the priority list above.
    9. Returns a command result to the Tinyhat runtime loop. The loop posts
       that result to ``/hapi/v1/computers/me/runtime-command/result``; on
       success the platform marks the Computer/agent active and revokes the
       short-lived Telegram setup grant so this Computer cannot fetch the bot
       token again.

When to use it:
    Tinyhat queues this automatically after a Mini App user claims an
    invitation and asks to create a Hermes agent. It is also available in Hat
    admin for retrying the transparent setup step.

Example input:
    {"kind": "configure_telegram", "spec": {"reason": "miniapp_assignment"}}

Example output:
    {
      "schema": "tinyhat_hermes_configure_telegram_v1",
      "configured": true,
      "bot_username": "tinyhatdevtest_4_bot",
      "owner_user_id": "123456",
      "env_files": [{"path": "~/.hermes/.env", "updated": true}],
      "webhook": {"ok": true},
      "gateway": {"started": true},
      "hermes": {"installed": true, "ok": true, "version": "Hermes Agent 0.1.0"}
    }

Side effects:
    Writes a private env file containing the Telegram bot token on the
    machine. The token is never returned in the command result or command
    ledger. Starts or restarts the Hermes messaging gateway.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
import shlex
import time
from typing import Any
from urllib import error, parse, request

from hermes_runtime.hermes_cli import (
    find_hermes_binary,
    probe_hermes_status,
    run_process,
)
from hermes_runtime.openrouter_stt import (
    DEFAULT_FALLBACK_MODELS as DEFAULT_OPENROUTER_STT_FALLBACK_MODELS,
    DEFAULT_LOCAL_FALLBACK_TIMEOUT_SECONDS,
    DEFAULT_TIMEOUT_SECONDS as DEFAULT_OPENROUTER_STT_BRIDGE_TIMEOUT_SECONDS,
)
from hermes_runtime.platform_paths import context_computer_api_path
from hermes_runtime.plugin_manager import hermes_home
from hermes_runtime.runtime_env import env_file_candidates


TELEGRAM_TINYHAT_MENU_COMMANDS = {
    "tinyhat_settings": "Open Tinyhat settings",
}
TELEGRAM_CODEX_MENU_COMMANDS = {
    "codex_auth": "Connect OpenAI Codex auth",
    "codex_auth_status": "Check Codex auth status",
    "codex_auth_log": "Show recent Codex auth output",
    "codex_limits": "Show OpenAI Codex usage limits",
}
TELEGRAM_MANAGED_MENU_COMMANDS = {
    **TELEGRAM_TINYHAT_MENU_COMMANDS,
    **TELEGRAM_CODEX_MENU_COMMANDS,
}
TELEGRAM_MENU_PRIORITY_MODE = "prepend"
TELEGRAM_MENU_MAX_COMMANDS = 60
TELEGRAM_MENU_START_MARKER = "# tinyhat managed telegram command menu start"
TELEGRAM_MENU_END_MARKER = "# tinyhat managed telegram command menu end"
CODEX_PLUGIN_NAME = "tinyhat-codex"
CODEX_PLUGIN_DIR_NAME = "tinyhat-codex"
OPENROUTER_STT_PROVIDER = "openrouter"
DEFAULT_OPENROUTER_STT_MODEL = "openai/gpt-4o-transcribe"
OPENROUTER_STT_COMMAND_TIMEOUT_MARGIN_SECONDS = 15
CODEX_STT_PROVIDER = "openai-codex-stt"
CODEX_STT_MODEL = "gpt-4o-transcribe"
CODEX_VISION_PROVIDER = "openai-codex"
DEFAULT_CODEX_VISION_MODEL = "gpt-5.4-mini"
DEFAULT_LOCAL_STT_MODEL = "medium"
DEFAULT_VISION_PROVIDER = "openrouter"
DEFAULT_VISION_MODEL = "google/gemini-2.5-flash-lite"


def local_stt_model() -> str:
    return (
        os.getenv("TINYHAT_HERMES_LOCAL_STT_MODEL")
        or DEFAULT_LOCAL_STT_MODEL
    ).strip() or DEFAULT_LOCAL_STT_MODEL


def openrouter_stt_model() -> str:
    return (
        os.getenv("TINYHAT_HERMES_OPENROUTER_STT_MODEL")
        or DEFAULT_OPENROUTER_STT_MODEL
    ).strip() or DEFAULT_OPENROUTER_STT_MODEL


def openrouter_stt_fallback_models() -> str:
    return (
        os.getenv("TINYHAT_HERMES_OPENROUTER_STT_FALLBACK_MODELS")
        or ",".join(DEFAULT_OPENROUTER_STT_FALLBACK_MODELS)
    ).strip()


def openrouter_stt_command_timeout_seconds() -> str:
    raw = (
        os.getenv("TINYHAT_HERMES_OPENROUTER_STT_TIMEOUT_SECONDS")
        or str(int(DEFAULT_OPENROUTER_STT_BRIDGE_TIMEOUT_SECONDS))
    ).strip()
    try:
        timeout = int(raw)
    except ValueError:
        timeout = int(DEFAULT_OPENROUTER_STT_BRIDGE_TIMEOUT_SECONDS)
    return str(
        max(
            45,
            timeout
            + int(DEFAULT_LOCAL_FALLBACK_TIMEOUT_SECONDS)
            + OPENROUTER_STT_COMMAND_TIMEOUT_MARGIN_SECONDS,
        )
    )


def openrouter_stt_command() -> str:
    fallback_models_arg = shlex.quote(openrouter_stt_fallback_models())
    local_fallback_model_arg = shlex.quote(local_stt_model())
    return (
        'PYTHONPATH="${TINYHAT_RUNTIME_PREFIX:-/opt/tinyhat-hermes-runtime}:${PYTHONPATH:-}" '
        "python3 -m hermes_runtime.openrouter_stt "
        "--input {input_path} --output {output_path} --format {format} "
        "--language {language} --model {model} "
        f"--fallback-models {fallback_models_arg} "
        f"--local-fallback-model {local_fallback_model_arg}"
    )


def vision_provider() -> str:
    return (
        os.getenv("TINYHAT_HERMES_VISION_PROVIDER")
        or DEFAULT_VISION_PROVIDER
    ).strip() or DEFAULT_VISION_PROVIDER


def vision_model() -> str:
    return (
        os.getenv("TINYHAT_HERMES_VISION_MODEL")
        or DEFAULT_VISION_MODEL
    ).strip() or DEFAULT_VISION_MODEL


def codex_vision_model() -> str:
    return (
        os.getenv("TINYHAT_HERMES_CODEX_VISION_MODEL")
        or DEFAULT_CODEX_VISION_MODEL
    ).strip() or DEFAULT_CODEX_VISION_MODEL


def _quote_env(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _upsert_env_file(path: Path, values: dict[str, str]) -> dict[str, Any]:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    next_lines: list[str] = []
    for line in existing:
        key, sep, _value = line.partition("=")
        clean_key = key.strip()
        if sep and clean_key in values:
            next_lines.append(f"{clean_key}={_quote_env(values[clean_key])}")
            seen.add(clean_key)
        else:
            next_lines.append(line)
    for key in sorted(values):
        if key not in seen:
            next_lines.append(f"{key}={_quote_env(values[key])}")
    path.write_text("\n".join(next_lines).rstrip() + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return {
        "path": str(path),
        "updated": True,
        "keys": sorted(values),
    }


def _env_file_candidates() -> list[Path]:
    return env_file_candidates()


def _hermes_config_file() -> Path:
    explicit = (os.getenv("HERMES_CONFIG_FILE") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return hermes_home() / "config.yaml"


def _yaml_single_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _codex_auth_command(action: str) -> str:
    return (
        'PYTHONPATH="${TINYHAT_RUNTIME_PREFIX:-/opt/tinyhat-hermes-runtime}:${PYTHONPATH:-}" '
        f"python3 -m hermes_runtime.telegram_codex_auth {shlex.quote(action)}"
    )


def _codex_limits_command() -> str:
    return (
        'PYTHONPATH="${TINYHAT_RUNTIME_PREFIX:-/opt/tinyhat-hermes-runtime}:${PYTHONPATH:-}" '
        "python3 -m hermes_runtime.codex_limits telegram"
    )


def _tinyhat_settings_command() -> str:
    return (
        'PYTHONPATH="${TINYHAT_RUNTIME_PREFIX:-/opt/tinyhat-hermes-runtime}:${PYTHONPATH:-}" '
        "python3 -m hermes_runtime.telegram_tinyhat_settings"
    )


def _codex_auth_quick_commands_block() -> str:
    commands = {
        "tinyhat_settings": {
            "description": "Open Tinyhat settings",
            "command": _tinyhat_settings_command(),
        },
        "codex_auth": {
            "description": "Connect OpenAI Codex auth",
            "command": _codex_auth_command("start"),
        },
        "codex-auth": {
            "description": "Connect OpenAI Codex auth",
            "command": _codex_auth_command("start"),
        },
        "codex_auth_status": {
            "description": "Check OpenAI Codex auth status",
            "command": _codex_auth_command("status"),
        },
        "codex_auth_log": {
            "description": "Show recent OpenAI Codex auth output",
            "command": _codex_auth_command("log"),
        },
        "codex_limits": {
            "description": "Show OpenAI Codex usage limits",
            "command": _codex_limits_command(),
        },
    }
    lines = [
        "  # tinyhat managed codex auth commands start",
    ]
    for name, spec in commands.items():
        lines.extend(
            [
                f"  {name}:",
                "    type: exec",
                f"    description: {_yaml_single_quote(spec['description'])}",
                f"    command: {_yaml_single_quote(spec['command'])}",
            ]
        )
    lines.append("  # tinyhat managed codex auth commands end")
    return "\n".join(lines)


def _install_codex_auth_quick_commands(config_file: Path | None = None) -> dict[str, Any]:
    config_file = config_file or _hermes_config_file()
    config_file.parent.mkdir(parents=True, exist_ok=True)
    text = config_file.read_text(encoding="utf-8") if config_file.exists() else ""
    block = _codex_auth_quick_commands_block()
    start_marker = "  # tinyhat managed codex auth commands start"
    end_marker = "  # tinyhat managed codex auth commands end"

    if start_marker in text and end_marker in text:
        prefix, rest = text.split(start_marker, 1)
        _old_block, suffix = rest.split(end_marker, 1)
        next_text = f"{prefix}{block}{suffix}"
    else:
        lines = text.splitlines()
        quick_index = next(
            (
                index
                for index, line in enumerate(lines)
                if line.strip() == "quick_commands:" and not line.startswith((" ", "\t"))
            ),
            None,
        )
        if quick_index is None:
            next_text = text.rstrip()
            if next_text:
                next_text += "\n\n"
            next_text += "quick_commands:\n" + block + "\n"
        else:
            insert_at = quick_index + 1
            lines[insert_at:insert_at] = block.splitlines()
            next_text = "\n".join(lines).rstrip() + "\n"

    config_file.write_text(next_text.rstrip() + "\n", encoding="utf-8")
    try:
        config_file.chmod(0o600)
    except OSError:
        pass
    return {
        "config_file": str(config_file),
        "installed": True,
        "commands": [
            "tinyhat_settings",
            "codex_auth",
            "codex-auth",
            "codex_auth_status",
            "codex_auth_log",
            "codex_limits",
        ],
        "telegram_menu_commands": [
            "tinyhat_settings",
            "codex_auth",
            "codex_auth_status",
            "codex_auth_log",
            "codex_limits",
        ],
    }


def _codex_auth_plugin_source() -> str:
    return '''"""Tinyhat Codex slash-command bridge for Hermes.

The runtime keeps the actual command behavior in Hermes quick_commands because
those are zero-token and run before plugin slash commands. This plugin exists so
Hermes' documented plugin command registry can include the Tinyhat Codex
commands in gateway slash-command surfaces such as Telegram BotCommands.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

try:
    from agent.transcription_provider import TranscriptionProvider
except Exception:  # noqa: BLE001 - older Hermes builds may not expose STT plugins.
    TranscriptionProvider = None


_COMMANDS = {
    "tinyhat_settings": {
        "description": "Open Tinyhat settings",
        "module": "hermes_runtime.telegram_tinyhat_settings",
        "args": [],
        "timeout": 30,
    },
    "codex_auth": {
        "description": "Connect OpenAI Codex auth",
        "module": "hermes_runtime.telegram_codex_auth",
        "args": ["start"],
        "timeout": 45,
    },
    "codex_auth_status": {
        "description": "Check Codex auth status",
        "module": "hermes_runtime.telegram_codex_auth",
        "args": ["status"],
        "timeout": 30,
    },
    "codex_auth_log": {
        "description": "Show recent Codex auth output",
        "module": "hermes_runtime.telegram_codex_auth",
        "args": ["log"],
        "timeout": 30,
    },
    "codex_limits": {
        "description": "Show OpenAI Codex usage limits",
        "module": "hermes_runtime.codex_limits",
        "args": ["telegram"],
        "timeout": 60,
    },
}

_STT_PROVIDER_NAME = "openai-codex-stt"
_STT_DEFAULT_MODEL = "gpt-4o-transcribe"
_STT_DEFAULT_BASE_URL = "https://api.openai.com/v1"


def _load_stt_provider_config() -> dict:
    try:
        from hermes_cli.config import load_config
    except Exception:
        return {}
    try:
        config = load_config() or {}
    except Exception:
        return {}
    stt = config.get("stt") if isinstance(config, dict) else None
    provider_cfg = stt.get(_STT_PROVIDER_NAME) if isinstance(stt, dict) else None
    return provider_cfg if isinstance(provider_cfg, dict) else {}


def _resolve_codex_audio_credentials() -> tuple[str, str, str | None]:
    try:
        from hermes_cli.auth import resolve_codex_runtime_credentials
    except Exception as exc:
        return "", "", f"Hermes Codex auth resolver is unavailable: {exc}"
    try:
        credentials = resolve_codex_runtime_credentials(refresh_if_expiring=True)
    except TypeError:
        try:
            credentials = resolve_codex_runtime_credentials()
        except Exception as exc:  # noqa: BLE001
            return "", "", f"OpenAI Codex auth is not connected: {exc}"
    except Exception as exc:  # noqa: BLE001
        return "", "", f"OpenAI Codex auth is not connected: {exc}"
    token = str((credentials or {}).get("api_key") or "").strip()
    if not token:
        return "", "", "OpenAI Codex auth is not connected. Run /codex_auth first."

    provider_cfg = _load_stt_provider_config()
    # Codex chat itself uses chatgpt.com/backend-api/codex, but OpenAI audio
    # transcription is exposed on the regular OpenAI API host.
    base_url = str(
        provider_cfg.get("base_url")
        or os.environ.get("TINYHAT_OPENAI_CODEX_STT_BASE_URL")
        or _STT_DEFAULT_BASE_URL
    ).strip().rstrip("/")
    return token, base_url, None


def _error_from_response(response) -> str:
    detail = ""
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "").strip()
            code = str(error.get("code") or error.get("type") or "").strip()
            if message and code:
                detail = f"{code}: {message}"
            elif message:
                detail = message
    if not detail:
        detail = str(getattr(response, "text", "") or "").strip()[:500]
    return f"OpenAI Codex STT failed ({response.status_code}): {detail or 'no response body'}"


if TranscriptionProvider is not None:
    class OpenAICodexTranscriptionProvider(TranscriptionProvider):
        @property
        def name(self) -> str:
            return _STT_PROVIDER_NAME

        @property
        def display_name(self) -> str:
            return "OpenAI Codex Speech-to-Text"

        def list_models(self):
            return [
                {
                    "id": "gpt-4o-transcribe",
                    "display": "GPT-4o Transcribe",
                },
                {
                    "id": "gpt-4o-mini-transcribe",
                    "display": "GPT-4o Mini Transcribe",
                },
                {
                    "id": "whisper-1",
                    "display": "Whisper",
                },
            ]

        def default_model(self):
            return _STT_DEFAULT_MODEL

        def is_available(self) -> bool:
            token, _base_url, error = _resolve_codex_audio_credentials()
            return bool(token and not error)

        def get_setup_schema(self):
            return {
                "name": self.display_name,
                "badge": "OpenAI auth",
                "tag": "Uses Hermes OpenAI Codex auth from /codex_auth",
                "env_vars": [],
            }

        def transcribe(self, file_path: str, *, model=None, language=None, **_extra):
            token, base_url, error = _resolve_codex_audio_credentials()
            if error:
                return {
                    "success": False,
                    "transcript": "",
                    "provider": self.name,
                    "error": error,
                }
            path = Path(file_path).expanduser()
            model_name = str(model or _STT_DEFAULT_MODEL).strip() or _STT_DEFAULT_MODEL
            data = {"model": model_name, "response_format": "json"}
            if language:
                data["language"] = str(language)
            try:
                import httpx

                with path.open("rb") as audio:
                    files = {
                        "file": (
                            path.name,
                            audio,
                            "application/octet-stream",
                        )
                    }
                    response = httpx.post(
                        f"{base_url}/audio/transcriptions",
                        headers={"Authorization": f"Bearer {token}"},
                        data=data,
                        files=files,
                        timeout=60.0,
                    )
                if response.status_code >= 400:
                    return {
                        "success": False,
                        "transcript": "",
                        "provider": self.name,
                        "error": _error_from_response(response),
                    }
                try:
                    payload = response.json()
                except Exception:
                    payload = None
                if isinstance(payload, dict):
                    text = str(payload.get("text") or "").strip()
                else:
                    text = str(getattr(response, "text", "") or "").strip()
                return {
                    "success": bool(text),
                    "transcript": text,
                    "provider": self.name,
                    **({} if text else {"error": "OpenAI Codex STT returned an empty transcript."}),
                }
            except Exception as exc:  # noqa: BLE001
                return {
                    "success": False,
                    "transcript": "",
                    "provider": self.name,
                    "error": f"OpenAI Codex STT failed: {exc}",
                }


def _runtime_pythonpath(env: dict[str, str]) -> str:
    prefix = env.get("TINYHAT_RUNTIME_PREFIX") or "/opt/tinyhat-hermes-runtime"
    existing = env.get("PYTHONPATH") or ""
    return prefix if not existing else prefix + os.pathsep + existing


def _run_runtime_module(module: str, args: list[str], timeout: int) -> str:
    env = os.environ.copy()
    env["PYTHONPATH"] = _runtime_pythonpath(env)
    result = subprocess.run(
        ["python3", "-m", module, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )
    output = "\\n".join(
        part.strip() for part in (result.stdout, result.stderr) if part.strip()
    )
    if result.returncode != 0:
        return output or f"{module} failed with exit code {result.returncode}."
    return output or "Done."


def _handler(module: str, args: list[str], timeout: int):
    def run(_raw_args: str = "") -> str:
        return _run_runtime_module(module, args, timeout)

    return run


def register(ctx):
    for name, spec in _COMMANDS.items():
        ctx.register_command(
            name,
            _handler(spec["module"], list(spec["args"]), int(spec["timeout"])),
            description=str(spec["description"]),
        )
    if TranscriptionProvider is not None and hasattr(ctx, "register_transcription_provider"):
        ctx.register_transcription_provider(OpenAICodexTranscriptionProvider())
'''


def _codex_auth_plugin_manifest() -> str:
    return "\n".join(
        [
            "name: tinyhat-codex",
            "version: 1.0.0",
            "description: Tinyhat Codex Telegram slash-command menu entries and OpenAI Codex STT provider.",
            "author: Tinyhat",
            "kind: standalone",
            "",
        ]
    )


def _simple_flow_list_items(value: str) -> list[str] | None:
    clean = value.split("#", 1)[0].strip()
    if not clean.startswith("[") or not clean.endswith("]"):
        return None
    inner = clean[1:-1].strip()
    if not inner:
        return []
    items: list[str] = []
    for raw_item in inner.split(","):
        item = raw_item.strip().strip("'\"")
        if item:
            items.append(item)
    return items


def _normalize_plugin_list_key(
    lines: list[str],
    key_index: int,
) -> tuple[list[str], int]:
    line = lines[key_index]
    _prefix, _separator, value = line.partition(":")
    items = _simple_flow_list_items(value)
    if items is None:
        return lines, key_index

    next_lines = lines[:]
    indent = _line_indent(line)
    replacement = [f"{' ' * indent}{line.strip().split(':', 1)[0]}:"]
    replacement.extend(f"{' ' * (indent + 2)}- {item}" for item in items)
    next_lines[key_index : key_index + 1] = replacement
    return next_lines, key_index


def _remove_plugin_from_disabled(lines: list[str], *, plugins_index: int) -> list[str]:
    plugins_end = _block_end(lines, plugins_index, indent=0)
    disabled_index = _find_key(
        lines,
        "disabled",
        indent=2,
        start=plugins_index + 1,
        end=plugins_end,
    )
    if disabled_index is None:
        return lines
    lines, disabled_index = _normalize_plugin_list_key(lines, disabled_index)
    disabled_end = _block_end(lines, disabled_index, indent=2)
    next_lines = lines[: disabled_index + 1]
    for line in lines[disabled_index + 1: disabled_end]:
        if line.strip() == f"- {CODEX_PLUGIN_NAME}":
            continue
        next_lines.append(line)
    next_lines.extend(lines[disabled_end:])
    return next_lines


def _ensure_plugin_enabled_config(lines: list[str]) -> list[str]:
    plugins_index = _find_key(lines, "plugins", indent=0)
    if plugins_index is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(
            [
                "plugins:",
                "  enabled:",
                f"    - {CODEX_PLUGIN_NAME}",
            ]
        )
        return lines

    plugins_end = _block_end(lines, plugins_index, indent=0)
    enabled_index = _find_key(
        lines,
        "enabled",
        indent=2,
        start=plugins_index + 1,
        end=plugins_end,
    )
    if enabled_index is None:
        lines[plugins_index + 1: plugins_index + 1] = [
            "  enabled:",
            f"    - {CODEX_PLUGIN_NAME}",
        ]
        return lines

    lines, enabled_index = _normalize_plugin_list_key(lines, enabled_index)
    enabled_end = _block_end(lines, enabled_index, indent=2)
    for line in lines[enabled_index + 1: enabled_end]:
        if line.strip() == f"- {CODEX_PLUGIN_NAME}":
            return lines
    lines[enabled_index + 1: enabled_index + 1] = [f"    - {CODEX_PLUGIN_NAME}"]
    return lines


def _install_codex_auth_plugin_commands(
    config_file: Path | None = None,
) -> dict[str, Any]:
    config_file = config_file or _hermes_config_file()
    config_file.parent.mkdir(parents=True, exist_ok=True)

    plugin_dir = config_file.parent / "plugins" / CODEX_PLUGIN_DIR_NAME
    plugin_dir.mkdir(parents=True, exist_ok=True)
    manifest_file = plugin_dir / "plugin.yaml"
    init_file = plugin_dir / "__init__.py"
    manifest_file.write_text(_codex_auth_plugin_manifest(), encoding="utf-8")
    init_file.write_text(_codex_auth_plugin_source(), encoding="utf-8")

    text = config_file.read_text(encoding="utf-8") if config_file.exists() else ""
    lines = text.splitlines()
    lines = _ensure_plugin_enabled_config(lines)
    plugins_index = _find_key(lines, "plugins", indent=0)
    if plugins_index is not None:
        lines = _remove_plugin_from_disabled(lines, plugins_index=plugins_index)
    config_file.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    try:
        config_file.chmod(0o600)
    except OSError:
        pass
    return {
        "config_file": str(config_file),
        "plugin_dir": str(plugin_dir),
        "installed": True,
        "enabled": True,
        "plugin": CODEX_PLUGIN_NAME,
        "mechanism": "hermes_plugin_register_command_and_transcription_provider",
        "commands": list(TELEGRAM_MANAGED_MENU_COMMANDS),
        "transcription_providers": [CODEX_STT_PROVIDER],
    }


def _line_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_yaml_key(line: str, *, indent: int, key: str) -> bool:
    stripped = line.strip()
    return _line_indent(line) == indent and (
        stripped == f"{key}:" or stripped.startswith(f"{key}: ")
    )


def _find_key(
    lines: list[str],
    key: str,
    *,
    indent: int,
    start: int = 0,
    end: int | None = None,
) -> int | None:
    end = len(lines) if end is None else end
    for index in range(start, end):
        if _is_yaml_key(lines[index], indent=indent, key=key):
            return index
    return None


def _block_end(lines: list[str], start: int, *, indent: int) -> int:
    index = start + 1
    while index < len(lines):
        line = lines[index]
        if line.strip() and _line_indent(line) <= indent:
            break
        index += 1
    return index


def _parse_telegram_menu_values(
    lines: list[str],
    *,
    start: int,
    end: int,
    key_indent: int,
) -> tuple[int | None, list[str]]:
    max_commands: int | None = None
    priority: list[str] = []
    index = start
    while index < end:
        line = lines[index]
        stripped = line.strip()
        if _line_indent(line) == key_indent and stripped.startswith("max_commands:"):
            raw_value = stripped.partition(":")[2].strip()
            try:
                parsed = int(raw_value)
                max_commands = max(1, min(100, parsed))
            except ValueError:
                max_commands = None
        if _line_indent(line) == key_indent and stripped == "priority:":
            item_index = index + 1
            while item_index < end:
                item_line = lines[item_index]
                if item_line.strip() and _line_indent(item_line) <= key_indent:
                    break
                item = item_line.strip()
                if item.startswith("- "):
                    command = item[2:].strip().strip("'\"")
                    if command:
                        priority.append(command)
                item_index += 1
            index = item_index
            continue
        index += 1
    return max_commands, priority


def _remove_tinyhat_telegram_menu_block(
    lines: list[str],
) -> tuple[list[str], int | None, list[str]]:
    next_lines: list[str] = []
    managed_lines: list[str] = []
    skipping = False
    for line in lines:
        if line.strip() == TELEGRAM_MENU_START_MARKER:
            skipping = True
            continue
        if skipping and line.strip() == TELEGRAM_MENU_END_MARKER:
            skipping = False
            continue
        if skipping:
            managed_lines.append(line)
        else:
            next_lines.append(line)
    max_commands, priority = _parse_telegram_menu_values(
        managed_lines,
        start=0,
        end=len(managed_lines),
        key_indent=8,
    )
    return next_lines, max_commands, priority


def _parse_existing_priority(
    lines: list[str],
    command_menu_index: int,
) -> tuple[int | None, list[str]]:
    end = _block_end(lines, command_menu_index, indent=6)
    return _parse_telegram_menu_values(
        lines,
        start=command_menu_index + 1,
        end=end,
        key_indent=8,
    )


def _remove_command_menu_keys(lines: list[str], command_menu_index: int) -> list[str]:
    end = _block_end(lines, command_menu_index, indent=6)
    next_lines = lines[: command_menu_index + 1]
    index = command_menu_index + 1
    while index < end:
        line = lines[index]
        stripped = line.strip()
        if _line_indent(line) == 8 and (
            stripped.startswith("max_commands:")
            or stripped.startswith("priority_mode:")
            or stripped == "priority:"
        ):
            child_end = index + 1
            while child_end < end:
                child = lines[child_end]
                if child.strip() and _line_indent(child) <= 8:
                    break
                child_end += 1
            index = child_end
            continue
        next_lines.append(line)
        index += 1
    next_lines.extend(lines[end:])
    return next_lines


def _telegram_menu_block(*, max_commands: int, existing_priority: list[str]) -> list[str]:
    priority = list(TELEGRAM_MANAGED_MENU_COMMANDS)
    for command in existing_priority:
        if command not in priority:
            priority.append(command)
    lines = [
        f"        {TELEGRAM_MENU_START_MARKER}",
        f"        max_commands: {max_commands}",
        f"        priority_mode: {TELEGRAM_MENU_PRIORITY_MODE}",
        "        priority:",
    ]
    lines.extend(f"          - {command}" for command in priority)
    lines.append(f"        {TELEGRAM_MENU_END_MARKER}")
    return lines


def _ensure_telegram_command_menu_config(lines: list[str]) -> tuple[list[str], int]:
    lines, managed_max_commands, managed_priority = _remove_tinyhat_telegram_menu_block(lines)
    fallback_max_commands = managed_max_commands or TELEGRAM_MENU_MAX_COMMANDS

    platforms_index = _find_key(lines, "platforms", indent=0)
    if platforms_index is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(
            [
                "platforms:",
                "  telegram:",
                "    extra:",
                "      command_menu:",
                *_telegram_menu_block(
                    max_commands=fallback_max_commands,
                    existing_priority=managed_priority,
                ),
            ]
        )
        return lines, fallback_max_commands

    platforms_end = _block_end(lines, platforms_index, indent=0)
    telegram_index = _find_key(
        lines,
        "telegram",
        indent=2,
        start=platforms_index + 1,
        end=platforms_end,
    )
    if telegram_index is None:
        lines[platforms_end:platforms_end] = [
            "  telegram:",
            "    extra:",
            "      command_menu:",
            *_telegram_menu_block(
                max_commands=fallback_max_commands,
                existing_priority=managed_priority,
            ),
        ]
        return lines, fallback_max_commands

    telegram_end = _block_end(lines, telegram_index, indent=2)
    extra_index = _find_key(
        lines,
        "extra",
        indent=4,
        start=telegram_index + 1,
        end=telegram_end,
    )
    if extra_index is None:
        lines[telegram_end:telegram_end] = [
            "    extra:",
            "      command_menu:",
            *_telegram_menu_block(
                max_commands=fallback_max_commands,
                existing_priority=managed_priority,
            ),
        ]
        return lines, fallback_max_commands

    extra_end = _block_end(lines, extra_index, indent=4)
    command_menu_index = _find_key(
        lines,
        "command_menu",
        indent=6,
        start=extra_index + 1,
        end=extra_end,
    )
    if command_menu_index is None:
        lines[extra_end:extra_end] = [
            "      command_menu:",
            *_telegram_menu_block(
                max_commands=fallback_max_commands,
                existing_priority=managed_priority,
            ),
        ]
        return lines, fallback_max_commands

    existing_max_commands, existing_priority = _parse_existing_priority(
        lines,
        command_menu_index,
    )
    max_commands = existing_max_commands or fallback_max_commands
    combined_priority = [*managed_priority, *existing_priority]
    lines = _remove_command_menu_keys(lines, command_menu_index)
    command_menu_index = _find_key(lines, "command_menu", indent=6) or command_menu_index
    lines[command_menu_index + 1:command_menu_index + 1] = _telegram_menu_block(
        max_commands=max_commands,
        existing_priority=combined_priority,
    )
    return lines, max_commands


def _install_telegram_command_menu_priority(
    config_file: Path | None = None,
) -> dict[str, Any]:
    config_file = config_file or _hermes_config_file()
    config_file.parent.mkdir(parents=True, exist_ok=True)
    text = config_file.read_text(encoding="utf-8") if config_file.exists() else ""
    next_lines, max_commands = _ensure_telegram_command_menu_config(text.splitlines())
    config_file.write_text("\n".join(next_lines).rstrip() + "\n", encoding="utf-8")
    try:
        config_file.chmod(0o600)
    except OSError:
        pass
    return {
        "config_file": str(config_file),
        "installed": True,
        "mechanism": "hermes_config",
        "path": "platforms.telegram.extra.command_menu",
        "priority_mode": TELEGRAM_MENU_PRIORITY_MODE,
        "max_commands": max_commands,
        "commands": list(TELEGRAM_MANAGED_MENU_COMMANDS),
    }


def _openrouter_env_values(setup: dict[str, Any]) -> dict[str, str]:
    api_key = str(setup.get("openrouter_api_key") or "").strip()
    if not api_key:
        raise RuntimeError("Platform did not return OpenRouter runtime config.")
    values = {"OPENROUTER_API_KEY": api_key}
    base_url = str(setup.get("openrouter_base_url") or "").strip()
    if base_url:
        values["OPENROUTER_BASE_URL"] = base_url
    return values


async def _run_config_set_commands(
    hermes_bin: Path,
    commands: list[tuple[str, str]],
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for key, value in commands:
        result = await run_process(
            [str(hermes_bin), "config", "set", key, value],
            timeout_seconds=45,
        )
        results.append(
            {
                "key": key,
                "value": value,
                "ok": bool(result.get("ok")),
                "returncode": result.get("returncode"),
                "duration_ms": result.get("duration_ms"),
                "stdout": str(result.get("stdout") or "")[:500],
                "stderr": str(result.get("stderr") or "")[:500],
            }
        )
        if not result.get("ok"):
            raise RuntimeError(f"Hermes config set failed for {key}.")
    return {"ok": True, "commands": results}


async def _configure_model(hermes_bin: Path, setup: dict[str, Any]) -> dict[str, Any]:
    # OpenRouter setup is deterministic provisioning, so use Hermes' public
    # config CLI instead of private Hermes modules or on-disk internals.
    commands: list[tuple[str, str]] = [("model.provider", "auto")]
    default_model = str(setup.get("openrouter_default_model") or "").strip()
    if default_model:
        commands.append(("model.default", default_model))
    base_url = str(setup.get("openrouter_base_url") or "").strip()
    if base_url:
        commands.append(("model.base_url", base_url))
    return await _run_config_set_commands(hermes_bin, commands)


async def _configure_day_one_multimedia(hermes_bin: Path) -> dict[str, Any]:
    commands = [
        ("stt.enabled", "true"),
        ("stt.provider", OPENROUTER_STT_PROVIDER),
        ("stt.local.model", local_stt_model()),
        ("stt.providers.openrouter.type", "command"),
        ("stt.providers.openrouter.command", openrouter_stt_command()),
        ("stt.providers.openrouter.model", openrouter_stt_model()),
        ("stt.providers.openrouter.fallback_models", openrouter_stt_fallback_models()),
        ("stt.providers.openrouter.local_fallback_model", local_stt_model()),
        ("stt.providers.openrouter.language", "auto"),
        ("stt.providers.openrouter.timeout", openrouter_stt_command_timeout_seconds()),
        ("stt.providers.openrouter.output_format", "txt"),
        ("auxiliary.vision.provider", vision_provider()),
        ("auxiliary.vision.model", vision_model()),
    ]
    return await _run_config_set_commands(hermes_bin, commands)


def _run_config_set_commands_sync(
    hermes_bin: Path,
    commands: list[tuple[str, str]],
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for key, value in commands:
        started = time.monotonic()
        try:
            process = subprocess.run(
                [str(hermes_bin), "config", "set", key, value],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=45,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            result = {
                "key": key,
                "value": value,
                "ok": False,
                "returncode": None,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "stdout": "",
                "stderr": str(exc)[:500],
            }
        else:
            result = {
                "key": key,
                "value": value,
                "ok": process.returncode == 0,
                "returncode": process.returncode,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "stdout": process.stdout[:500],
                "stderr": process.stderr[:500],
            }
        results.append(result)
        if not result["ok"]:
            return {"ok": False, "commands": results, "failed_key": key}
    return {"ok": True, "commands": results}


def configure_codex_multimedia(hermes_bin: Path) -> dict[str, Any]:
    # Codex subscription auth improves Hermes' chat and vision route, but it
    # should not become the active STT provider: Codex subscription auth may not
    # include API-billed audio transcription. Keep the OpenRouter STT command
    # provider active and leave the Codex STT plugin as an opt-in provider.
    commands = [
        ("stt.enabled", "true"),
        ("stt.provider", OPENROUTER_STT_PROVIDER),
        ("stt.providers.openrouter.type", "command"),
        ("stt.providers.openrouter.command", openrouter_stt_command()),
        ("stt.providers.openrouter.model", openrouter_stt_model()),
        ("stt.providers.openrouter.fallback_models", openrouter_stt_fallback_models()),
        ("stt.providers.openrouter.local_fallback_model", local_stt_model()),
        ("stt.providers.openrouter.language", "auto"),
        ("stt.providers.openrouter.timeout", openrouter_stt_command_timeout_seconds()),
        ("stt.providers.openrouter.output_format", "txt"),
        (f"stt.{CODEX_STT_PROVIDER}.model", CODEX_STT_MODEL),
        ("auxiliary.vision.provider", CODEX_VISION_PROVIDER),
        ("auxiliary.vision.model", codex_vision_model()),
    ]
    result = _run_config_set_commands_sync(hermes_bin, commands)
    result.update(
        {
            "active_provider": OPENROUTER_STT_PROVIDER,
            "openrouter_stt_provider": OPENROUTER_STT_PROVIDER,
            "openrouter_stt_model": openrouter_stt_model(),
            "openrouter_stt_fallback_models": openrouter_stt_fallback_models(),
            "local_stt_fallback_model": local_stt_model(),
            "codex_stt_provider": CODEX_STT_PROVIDER,
            "codex_stt_model": CODEX_STT_MODEL,
            "auto_selected_codex_stt": False,
            "vision_provider": CODEX_VISION_PROVIDER,
            "vision_model": codex_vision_model(),
        }
    )
    if result.get("ok"):
        result["message"] = (
            "Switched Hermes vision to the Codex provider and kept OpenRouter "
            "Whisper STT active."
        )
    return result


def _telegram_delete_webhook(token: str) -> dict[str, Any]:
    body = parse.urlencode({"drop_pending_updates": "false"}).encode("utf-8")
    req = request.Request(
        f"https://api.telegram.org/bot{token}/deleteWebhook",
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "tinyhat-hermes-runtime/0.0.1",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return {
            "ok": False,
            "http_status": exc.code,
            "description": detail[:500],
        }
    except error.URLError as exc:
        return {"ok": False, "description": str(exc.reason)[:500]}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "description": "Telegram returned invalid JSON."}
    if not isinstance(payload, dict):
        return {"ok": False, "description": "Telegram returned non-object JSON."}
    return {
        "ok": bool(payload.get("ok")),
        "description": str(payload.get("description") or "")[:500] or None,
    }


def _telegram_set_chat_menu_button(
    token: str,
    *,
    text: str,
    web_app_url: str,
    chat_id: str | int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "menu_button": {
            "type": "web_app",
            "text": text,
            "web_app": {"url": web_app_url},
        }
    }
    if chat_id is not None and str(chat_id).strip():
        try:
            payload["chat_id"] = int(str(chat_id).strip())
        except ValueError:
            payload["chat_id"] = str(chat_id).strip()
    body = parse.urlencode(
        {
            key: json.dumps(value) if isinstance(value, dict) else str(value)
            for key, value in payload.items()
        }
    ).encode("utf-8")
    req = request.Request(
        f"https://api.telegram.org/bot{token}/setChatMenuButton",
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "tinyhat-hermes-runtime/0.0.1",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return {
            "ok": False,
            "http_status": exc.code,
            "description": detail[:500],
        }
    except error.URLError as exc:
        return {"ok": False, "description": str(exc.reason)[:500]}
    try:
        response_payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "description": "Telegram returned invalid JSON."}
    if not isinstance(response_payload, dict):
        return {"ok": False, "description": "Telegram returned non-object JSON."}
    return {
        "ok": bool(response_payload.get("ok")),
        "description": str(response_payload.get("description") or "")[:500] or None,
    }


async def _configure_tinyhat_menu_button(
    *,
    token: str,
    settings_url: str,
    owner_chat_id: str,
) -> dict[str, Any]:
    if not settings_url:
        return {"configured": False, "reason": "missing_settings_url"}
    if not settings_url.lower().startswith("https://"):
        return {
            "configured": False,
            "reason": "settings_url_not_https",
            "settings_url": settings_url,
        }
    default_result = await asyncio.to_thread(
        _telegram_set_chat_menu_button,
        token,
        text="configure",
        web_app_url=settings_url,
    )
    owner_result = await asyncio.to_thread(
        _telegram_set_chat_menu_button,
        token,
        text="configure",
        web_app_url=settings_url,
        chat_id=owner_chat_id,
    )
    return {
        "configured": bool(default_result.get("ok")) and bool(owner_result.get("ok")),
        "text": "configure",
        "settings_url": settings_url,
        "default": default_result,
        "owner_chat": owner_result,
    }


def _compact_process(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    return {
        "ok": bool(result.get("ok")),
        "returncode": result.get("returncode"),
        "timed_out": bool(result.get("timed_out")),
        "duration_ms": result.get("duration_ms"),
        "stdout": str(result.get("stdout") or "")[:1000],
        "stderr": str(result.get("stderr") or "")[:1000],
        "stdout_truncated": bool(result.get("stdout_truncated")),
        "stderr_truncated": bool(result.get("stderr_truncated")),
    }


def _process_text(result: dict[str, Any] | None) -> str:
    if not isinstance(result, dict):
        return ""
    return f"{result.get('stdout') or ''}\n{result.get('stderr') or ''}".lower()


def _gateway_status_is_healthy(status: dict[str, Any] | None) -> bool:
    if not isinstance(status, dict) or not status.get("ok"):
        return False
    text = _process_text(status)
    if "not running" in text or "gateway is not running" in text:
        return False
    return True


def _gateway_needs_foreground_run(
    *,
    start: dict[str, Any],
    status: dict[str, Any],
) -> bool:
    text = f"{_process_text(start)}\n{_process_text(status)}"
    if "not applicable inside a docker container" in text:
        return True
    if "run the gateway directly" in text:
        return True
    return not _gateway_status_is_healthy(status)


def _gateway_log_path() -> Path:
    state_dir = Path(
        (os.getenv("TINYHAT_RUNTIME_STATE_DIR") or "/var/lib/tinyhat-hermes-runtime")
    )
    return state_dir / "hermes-gateway.log"


def _gateway_log_has_adapter_failure(path: Path | None) -> bool:
    if path is None:
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[-8000:].lower()
    except OSError:
        return False
    needles = (
        "platform 'telegram' requirements not met",
        "adapter creation failed",
        "no adapter available for telegram",
    )
    return any(needle in text for needle in needles)


async def _start_gateway_foreground(hermes_bin: Path) -> dict[str, Any]:
    log_path = _gateway_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log_file:
        process = subprocess.Popen(
            [
                str(hermes_bin),
                "gateway",
                "run",
                "--replace",
                "--force",
                "--accept-hooks",
            ],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )
    await asyncio.sleep(2)
    returncode = process.poll()
    return {
        "mode": "foreground_detached",
        "pid": process.pid,
        "started": returncode is None,
        "returncode": returncode,
        "log_path": str(log_path),
    }


async def _run_gateway(hermes_bin: Path) -> dict[str, Any]:
    stop = await run_process(
        [str(hermes_bin), "gateway", "stop"],
        timeout_seconds=60,
    )
    start = await run_process(
        [str(hermes_bin), "gateway", "start"],
        timeout_seconds=180,
    )
    status = await run_process(
        [str(hermes_bin), "gateway", "status"],
        timeout_seconds=45,
    )
    foreground: dict[str, Any] | None = None
    if _gateway_needs_foreground_run(start=start, status=status):
        foreground = await _start_gateway_foreground(hermes_bin)
        status = await run_process(
            [str(hermes_bin), "gateway", "status"],
            timeout_seconds=45,
        )
    foreground_log = (
        Path(str(foreground.get("log_path")))
        if isinstance(foreground, dict) and foreground.get("log_path")
        else None
    )
    adapter_failure = _gateway_log_has_adapter_failure(foreground_log)
    healthy = _gateway_status_is_healthy(status) and not adapter_failure
    return {
        "stopped": bool(stop.get("ok")),
        "started": bool(start.get("ok"))
        or bool(foreground and foreground.get("started")),
        "healthy": healthy,
        "mode": (
            str(foreground.get("mode"))
            if isinstance(foreground, dict) and foreground.get("mode")
            else "service"
        ),
        "adapter_ready": not adapter_failure,
        "stop": _compact_process(stop),
        "start": _compact_process(start),
        "foreground": foreground,
        "status": _compact_process(status),
    }


def _compact_hermes_status(status: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": status.get("schema"),
        "installed": bool(status.get("installed")),
        "ok": bool(status.get("ok")),
        "version": status.get("version"),
        "message": status.get("message"),
    }


async def run(ctx: Any, _command: dict[str, Any]) -> dict[str, Any]:
    api_path = context_computer_api_path(ctx, "hermes/telegram-setup/v1")
    setup = await ctx.platform.post_json(api_path, {})

    token = str(setup.get("telegram_bot_token") or "").strip()
    if not token:
        raise RuntimeError("Platform did not return a Telegram bot token.")
    owner_user_id = str(setup.get("telegram_owner_user_id") or "").strip()
    if not owner_user_id:
        raise RuntimeError("Platform did not return the Telegram owner user id.")

    env_values = {
        "TELEGRAM_BOT_TOKEN": token,
        "TELEGRAM_ALLOWED_USERS": str(
            setup.get("telegram_allowed_users") or owner_user_id
        ),
        "TELEGRAM_HOME_CHANNEL": str(
            setup.get("telegram_home_channel") or owner_user_id
        ),
        "TELEGRAM_HOME_CHANNEL_NAME": str(
            setup.get("telegram_home_channel_name") or "Owner DM"
        ),
        "TINYHAT_SETTINGS_MINIAPP_URL": str(
            setup.get("settings_miniapp_url") or ""
        ).strip(),
        **_openrouter_env_values(setup),
    }
    env_files = [
        _upsert_env_file(env_path, env_values)
        for env_path in _env_file_candidates()
    ]
    codex_auth = {
        "quick_commands": _install_codex_auth_quick_commands(),
        "plugin_commands": _install_codex_auth_plugin_commands(),
        "telegram_command_menu": _install_telegram_command_menu_priority(),
    }

    hermes_bin = find_hermes_binary()
    if hermes_bin is None:
        raise RuntimeError("Hermes CLI was not found; install Hermes first.")
    model_config = await _configure_model(hermes_bin, setup)
    multimedia_config = await _configure_day_one_multimedia(hermes_bin)
    menu_button = await _configure_tinyhat_menu_button(
        token=token,
        settings_url=env_values["TINYHAT_SETTINGS_MINIAPP_URL"],
        owner_chat_id=owner_user_id,
    )

    webhook = await asyncio.to_thread(_telegram_delete_webhook, token)
    if not webhook.get("ok"):
        raise RuntimeError(
            "Telegram deleteWebhook failed: "
            f"{webhook.get('description') or webhook.get('http_status')}"
        )

    gateway = await _run_gateway(hermes_bin)
    if not gateway.get("healthy"):
        raise RuntimeError("Hermes gateway did not report a healthy status.")

    hermes_status = await probe_hermes_status()
    return {
        "schema": "tinyhat_hermes_configure_telegram_v1",
        "configured": True,
        "agent_id": setup.get("agent_id"),
        "computer_id": setup.get("computer_id"),
        "bot_user_id": setup.get("telegram_bot_user_id"),
        "bot_username": setup.get("telegram_bot_username"),
        "owner_user_id": owner_user_id,
        "allowed_users": env_values["TELEGRAM_ALLOWED_USERS"],
        "home_channel": env_values["TELEGRAM_HOME_CHANNEL"],
        "home_channel_name": env_values["TELEGRAM_HOME_CHANNEL_NAME"],
        "env_files": env_files,
        "codex_auth": codex_auth,
        "model_config": model_config,
        "multimedia_config": multimedia_config,
        "menu_button": menu_button,
        "webhook": webhook,
        "gateway": gateway,
        "hermes": _compact_hermes_status(hermes_status),
    }
