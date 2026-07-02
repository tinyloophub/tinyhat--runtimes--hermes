"""Show Hermes voice and image model configuration without exposing secrets."""

from __future__ import annotations

import json
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
    DEFAULT_OPENROUTER_STT_FALLBACK_MODELS,
    DEFAULT_OPENROUTER_VISION_FALLBACK_MODELS,
    DEFAULT_VISION_MODEL,
    DEFAULT_VISION_PROVIDER,
    MAX_OPENROUTER_VISION_FALLBACK_MODELS,
    OPENROUTER_STT_PROVIDER,
    openrouter_vision_fallback_model_list,
    _hermes_config_file,
)
from hermes_runtime.hermes_cli import find_hermes_binary, run_process
from hermes_runtime.runtime_env import env_file_candidates
from hermes_runtime.openrouter_stt import (
    get_env_value as _openrouter_stt_env_value,
    hermes_python,
)

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


def _parse_model_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [
        item.strip()
        for item in value.replace("\n", ",").replace(";", ",").split(",")
        if item.strip()
    ]


def _active_stt_model(provider: str, values: dict[str, str]) -> str | None:
    if provider == OPENROUTER_STT_PROVIDER:
        return (
            values.get("stt.providers.openrouter.model")
            or DEFAULT_OPENROUTER_STT_MODEL
        )
    if provider == "local":
        return values.get("stt.local.model") or DEFAULT_LOCAL_STT_MODEL
    if provider == CODEX_STT_PROVIDER:
        return values.get(f"stt.{CODEX_STT_PROVIDER}.model")
    return values.get(f"stt.{provider}.model") if provider else None


def _redact_text(text: str) -> str:
    return SECRET_ASSIGNMENT_RE.sub(r"\1[redacted]", text)


_READ_STRUCTURED_CONFIG_SCRIPT = r"""
import json
from pathlib import Path
import sys

import yaml

path = Path(sys.argv[1]).expanduser()
config = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
if not isinstance(config, dict):
    config = {}
vision = config.get("auxiliary", {}).get("vision", {})
if not isinstance(vision, dict):
    vision = {}
print(json.dumps({
    "auxiliary": {
        "vision": {
            "extra_body": vision.get("extra_body") if isinstance(vision.get("extra_body"), dict) else {},
            "fallback_chain": vision.get("fallback_chain") if isinstance(vision.get("fallback_chain"), list) else [],
        }
    }
}))
"""


async def _read_structured_config(config_file: Path) -> dict[str, Any]:
    python_bin = hermes_python()
    if not python_bin.is_file():
        return {}
    result = await run_process(
        [
            str(python_bin),
            "-c",
            _READ_STRUCTURED_CONFIG_SCRIPT,
            str(config_file),
        ],
        timeout_seconds=30,
    )
    if not result.get("ok"):
        return {}
    try:
        parsed = json.loads(str(result.get("stdout") or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _sanitize_fallback_chain(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    entries: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        provider = str(entry.get("provider") or "").strip()
        model = str(entry.get("model") or "").strip()
        if provider and model:
            entries.append({"provider": provider, "model": model})
    return entries


async def run(_ctx: Any, _command: dict[str, Any]) -> dict[str, Any]:
    config_file = _hermes_config_file()
    values, config = _read_config_values(config_file)
    structured_config = await _read_structured_config(config_file)
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
    vision_structured = (
        structured_config.get("auxiliary", {})
        .get("vision", {})
        if isinstance(structured_config.get("auxiliary"), dict)
        else {}
    )
    if not isinstance(vision_structured, dict):
        vision_structured = {}
    vision_extra_body = vision_structured.get("extra_body")
    if not isinstance(vision_extra_body, dict):
        vision_extra_body = {}
    vision_openrouter_fallback_models = _parse_model_list(
        ",".join(str(item) for item in vision_extra_body.get("models", []))
        if isinstance(vision_extra_body.get("models"), list)
        else str(vision_extra_body.get("models") or "")
    )
    vision_openrouter_model = (
        vision_model
        if vision_provider == DEFAULT_VISION_PROVIDER
        else DEFAULT_VISION_MODEL
    )
    if not vision_openrouter_fallback_models:
        vision_openrouter_fallback_models = (
            openrouter_vision_fallback_model_list(
                exclude_model=vision_openrouter_model
            )
            or list(
                DEFAULT_OPENROUTER_VISION_FALLBACK_MODELS[
                    :MAX_OPENROUTER_VISION_FALLBACK_MODELS
                ]
            )
        )
    vision_provider_fallback_chain = _sanitize_fallback_chain(
        vision_structured.get("fallback_chain")
    )
    openrouter_key = _env_key_presence("OPENROUTER_API_KEY")
    openrouter_base_url = _env_key_presence("OPENROUTER_BASE_URL")
    openrouter_command_key = bool(_openrouter_stt_env_value("OPENROUTER_API_KEY"))
    openrouter_command_base_url = bool(
        _openrouter_stt_env_value("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    )
    openrouter_command = values.get("stt.providers.openrouter.command") or ""
    openrouter_model = (
        values.get("stt.providers.openrouter.model")
        or DEFAULT_OPENROUTER_STT_MODEL
    )
    openrouter_fallback_models = _parse_model_list(
        values.get("stt.providers.openrouter.fallback_models")
    ) or list(DEFAULT_OPENROUTER_STT_FALLBACK_MODELS)
    local_model = values.get("stt.local.model") or DEFAULT_LOCAL_STT_MODEL
    local_fallback_model = (
        values.get("stt.providers.openrouter.local_fallback_model")
        or local_model
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
                "fallback_models": openrouter_fallback_models,
                "command_provider_configured": bool(openrouter_command.strip()),
                "language": values.get("stt.providers.openrouter.language") or "auto",
                "timeout_seconds": values.get("stt.providers.openrouter.timeout"),
                "output_format": values.get("stt.providers.openrouter.output_format") or "txt",
                "api_key_present": bool(openrouter_key["present"]),
                "base_url_present": bool(openrouter_base_url["present"]),
                "command_api_key_resolvable": openrouter_command_key,
                "command_base_url_resolvable": openrouter_command_base_url,
            },
            "local_model": {
                "provider": "local",
                "model": local_model,
                "prepared_for_provider": "local",
                "automatic_fallback_from_openrouter": True,
            },
            "local_fallback": {
                "provider": "local",
                "model": local_fallback_model,
                "automatic": True,
                "note": (
                    "Used only after the OpenRouter command provider fails "
                    "after its configured model fallback chain."
                ),
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
            "openrouter": {
                "provider": DEFAULT_VISION_PROVIDER,
                "model": vision_openrouter_model,
                "fallback_models": vision_openrouter_fallback_models,
                "fallback_mechanism": "openrouter_chat_completions_models",
            },
            "provider_fallback_chain": vision_provider_fallback_chain,
        },
        "secrets": {
            "values_masked": True,
            "openrouter_api_key_present": bool(openrouter_key["present"]),
            "openrouter_base_url_present": bool(openrouter_base_url["present"]),
            "env_files": openrouter_key["sources"],
        },
        "config_show": config_show,
    }
