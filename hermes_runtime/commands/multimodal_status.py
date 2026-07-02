"""Show Hermes voice and image model configuration without exposing secrets."""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any

from hermes_runtime.commands.configure_telegram import (
    CODEX_STT_PROVIDER,
    CODEX_VISION_PROVIDER,
    DEFAULT_CODEX_VISION_MODEL,
    DEFAULT_LOCAL_STT_MODEL,
    DEFAULT_OPENROUTER_STT_MODEL,
    DEFAULT_VISION_MODEL,
    DEFAULT_VISION_PROVIDER,
    OPENROUTER_STT_PROVIDER,
    _hermes_config_file,
)
from hermes_runtime.hermes_cli import find_hermes_binary, run_process
from hermes_runtime.runtime_env import env_file_candidates

SCHEMA = "tinyhat_hermes_multimodal_status_v1"
MAX_CONFIG_SHOW_CHARS = 12_000
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?im)^([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*\s*=\s*).+$"
)


def _strip_scalar(value: str) -> str:
    clean = value.strip()
    if " #" in clean:
        clean = clean.split(" #", 1)[0].strip()
    if (
        len(clean) >= 2
        and clean[0] == clean[-1]
        and clean.startswith(("'", '"'))
    ):
        clean = clean[1:-1]
    return clean.replace('\\"', '"').replace("\\\\", "\\")


def _parse_scalar_yaml(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    stack: list[tuple[int, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("- "):
            continue
        if ":" not in stripped:
            continue
        indent = len(line) - len(line.lstrip(" "))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        key, _sep, raw_value = stripped.partition(":")
        key = key.strip().strip("'\"")
        if not key:
            continue
        path = ".".join([item for _level, item in stack] + [key])
        value = _strip_scalar(raw_value)
        if value:
            values[path] = value
        else:
            stack.append((indent, key))
    return values


def _read_config_values(path: Path) -> tuple[dict[str, str], dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, {"path": str(path), "exists": False, "readable": False}
    except OSError as exc:
        return {}, {
            "path": str(path),
            "exists": path.exists(),
            "readable": False,
            "error": str(exc)[:300],
        }
    return _parse_scalar_yaml(text), {
        "path": str(path),
        "exists": True,
        "readable": True,
        "scalar_count": len(_parse_scalar_yaml(text)),
    }


def _parse_env_line(line: str) -> tuple[str, str] | None:
    clean = line.strip()
    if not clean or clean.startswith("#") or "=" not in clean:
        return None
    key, value = clean.split("=", 1)
    return key.strip(), _strip_scalar(value)


def _env_key_presence(key: str) -> dict[str, Any]:
    present = bool((os.environ.get(key) or "").strip())
    sources: list[dict[str, Any]] = []
    for path in env_file_candidates():
        source = {"path": str(path), "exists": path.exists(), "readable": False, "present": False}
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            sources.append(source)
            continue
        source["readable"] = True
        for line in lines:
            parsed = _parse_env_line(line)
            if parsed and parsed[0] == key and parsed[1]:
                present = True
                source["present"] = True
                break
        sources.append(source)
    return {"present": present, "sources": sources}


def _as_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def _active_stt_model(provider: str, values: dict[str, str]) -> str | None:
    if provider == OPENROUTER_STT_PROVIDER:
        return (
            values.get("stt.openrouter.model")
            or values.get("stt.providers.openrouter.model")
            or DEFAULT_OPENROUTER_STT_MODEL
        )
    if provider == "local":
        return values.get("stt.local.model") or DEFAULT_LOCAL_STT_MODEL
    if provider == CODEX_STT_PROVIDER:
        return values.get(f"stt.{CODEX_STT_PROVIDER}.model")
    return values.get(f"stt.{provider}.model") if provider else None


def _redact_text(text: str) -> str:
    return SECRET_ASSIGNMENT_RE.sub(r"\1[redacted]", text)


async def run(_ctx: Any, _command: dict[str, Any]) -> dict[str, Any]:
    config_file = _hermes_config_file()
    values, config = _read_config_values(config_file)
    hermes_bin = find_hermes_binary()
    config_show: dict[str, Any] | None = None
    if hermes_bin is not None:
        show = await run_process(
            [str(hermes_bin), "config", "show"],
            timeout_seconds=30,
        )
        config_show = {
            "ok": bool(show.get("ok")),
            "returncode": show.get("returncode"),
            "stdout": _redact_text(str(show.get("stdout") or ""))[
                :MAX_CONFIG_SHOW_CHARS
            ],
            "stderr": _redact_text(str(show.get("stderr") or ""))[:2000],
            "duration_ms": show.get("duration_ms"),
        }

    stt_provider = values.get("stt.provider") or "auto"
    vision_provider = values.get("auxiliary.vision.provider") or DEFAULT_VISION_PROVIDER
    vision_model = values.get("auxiliary.vision.model") or (
        DEFAULT_CODEX_VISION_MODEL
        if vision_provider == CODEX_VISION_PROVIDER
        else DEFAULT_VISION_MODEL
    )
    openrouter_key = _env_key_presence("OPENROUTER_API_KEY")
    openrouter_base_url = _env_key_presence("OPENROUTER_BASE_URL")
    openrouter_command = values.get("stt.providers.openrouter.command") or ""
    openrouter_model = (
        values.get("stt.openrouter.model")
        or values.get("stt.providers.openrouter.model")
        or DEFAULT_OPENROUTER_STT_MODEL
    )

    return {
        "schema": SCHEMA,
        "hermes": {
            "installed": hermes_bin is not None,
            "hermes_bin": str(hermes_bin) if hermes_bin is not None else None,
        },
        "config": config,
        "stt": {
            "enabled": _as_bool(values.get("stt.enabled")),
            "provider": stt_provider,
            "active_model": _active_stt_model(stt_provider, values),
            "openrouter": {
                "provider": OPENROUTER_STT_PROVIDER,
                "model": openrouter_model,
                "command_provider_configured": bool(openrouter_command.strip()),
                "language": values.get("stt.providers.openrouter.language") or "auto",
                "timeout_seconds": values.get("stt.providers.openrouter.timeout"),
                "output_format": values.get("stt.providers.openrouter.output_format") or "txt",
                "api_key_present": bool(openrouter_key["present"]),
                "base_url_present": bool(openrouter_base_url["present"]),
            },
            "local_fallback": {
                "provider": "local",
                "model": values.get("stt.local.model") or DEFAULT_LOCAL_STT_MODEL,
            },
            "codex": {
                "provider": CODEX_STT_PROVIDER,
                "model": values.get(f"stt.{CODEX_STT_PROVIDER}.model"),
                "active": stt_provider == CODEX_STT_PROVIDER,
            },
        },
        "vision": {
            "provider": vision_provider,
            "model": vision_model,
            "uses_codex_auth": vision_provider == CODEX_VISION_PROVIDER,
        },
        "secrets": {
            "values_masked": True,
            "openrouter_api_key_present": bool(openrouter_key["present"]),
            "openrouter_base_url_present": bool(openrouter_base_url["present"]),
            "env_files": openrouter_key["sources"],
        },
        "config_show": config_show,
    }
