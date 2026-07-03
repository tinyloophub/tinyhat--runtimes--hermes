"""Activate imported OpenAI Codex auth model settings when available.

This command is intentionally non-interactive. It only switches Hermes to
OpenAI Codex when existing Hermes auth-store or Codex CLI credentials are
already present on the Computer. It never starts a fresh device-code flow.
"""

from __future__ import annotations

from typing import Any

from hermes_runtime.codex_limits import find_codex_binary
from hermes_runtime.hermes_cli import find_hermes_binary
from hermes_runtime.telegram_codex_auth import (
    MODEL_PROVIDER,
    _auth_status,
    _codex_cli_status,
    _configure_multimedia_after_auth,
    _restart_gateway_after_auth,
    _run_config_switch,
)


SCHEMA = "tinyhat_hermes_activate_codex_auth_models_v1"


def _bool_spec(spec: dict[str, Any], key: str, default: bool = False) -> bool:
    value = spec.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _codex_cli_status_if_available() -> dict[str, Any]:
    codex_bin = find_codex_binary()
    if codex_bin is None:
        return {
            "ok": False,
            "available": False,
            "message": "Codex CLI was not found.",
        }
    status = _codex_cli_status(codex_bin)
    status["available"] = True
    status["codex_bin"] = str(codex_bin)
    return status


def _existing_auth_present(
    *,
    hermes_auth_status: dict[str, Any],
    codex_cli_status: dict[str, Any],
) -> bool:
    return bool(hermes_auth_status.get("ok") or codex_cli_status.get("ok"))


async def run(_ctx: Any, command: dict[str, Any]) -> dict[str, Any]:
    spec = command.get("spec") if isinstance(command.get("spec"), dict) else {}
    hermes_bin = find_hermes_binary()
    if hermes_bin is None:
        raise RuntimeError("Hermes CLI was not found; cannot activate Codex auth models.")

    hermes_auth_status = _auth_status(hermes_bin)
    codex_cli_status = _codex_cli_status_if_available()
    if not _existing_auth_present(
        hermes_auth_status=hermes_auth_status,
        codex_cli_status=codex_cli_status,
    ):
        return {
            "schema": SCHEMA,
            "activated": False,
            "status": "skipped",
            "reason": "codex_auth_not_found",
            "message": (
                "No existing OpenAI Codex auth was found on this Computer; "
                "leaving the current Hermes model configuration unchanged."
            ),
            "model_provider": MODEL_PROVIDER,
            "hermes": {
                "hermes_bin": str(hermes_bin),
                "auth_status": hermes_auth_status,
            },
            "codex_cli_status": codex_cli_status,
        }

    switch = _run_config_switch(hermes_bin)
    if not switch.get("ok"):
        raise RuntimeError(
            "Hermes model picker did not switch to OpenAI Codex: "
            + str(switch.get("output") or switch.get("returncode") or "unknown error")[:1000]
        )

    codex_chat_model = str(switch.get("model_default") or "").strip()
    multimedia = _configure_multimedia_after_auth(
        hermes_bin,
        codex_chat_model=codex_chat_model,
    )
    if not multimedia.get("ok"):
        raise RuntimeError(
            "Hermes Codex multimedia configuration failed: "
            + str(multimedia.get("message") or multimedia.get("failed_key") or "unknown error")[:1000]
        )

    gateway: dict[str, Any] | None = None
    if _bool_spec(spec, "restart_gateway", False):
        gateway = _restart_gateway_after_auth(hermes_bin)

    result: dict[str, Any] = {
        "schema": SCHEMA,
        "activated": True,
        "status": "applied",
        "model_provider": MODEL_PROVIDER,
        "model_default": codex_chat_model,
        "config_switch": switch,
        "multimedia_config": multimedia,
        "hermes": {
            "hermes_bin": str(hermes_bin),
            "auth_status": _auth_status(hermes_bin),
        },
        "codex_cli_status": codex_cli_status,
        "gateway_restart_requested": _bool_spec(spec, "restart_gateway", False),
    }
    if gateway is not None:
        result["gateway_restart"] = gateway
    return result
