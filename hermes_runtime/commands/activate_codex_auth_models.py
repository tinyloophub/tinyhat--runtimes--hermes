"""Activate imported OpenAI Codex auth model settings when available.

This command switches Hermes to OpenAI Codex when existing Hermes auth-store
or Codex CLI credentials are already present on the Computer. During OpenClaw
to Hermes migration, it can also start the normal Hermes Codex device-code
flow when it detects an older OpenClaw OpenAI login that cannot be reused
directly.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
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
    start_openclaw_migration_reconnect,
)


SCHEMA = "tinyhat_hermes_activate_codex_auth_models_v1"
OPENCLAW_AUTH_SOURCE_ROOTS = (
    "/var/lib/tinyhat-openclaw",
    "~/.openclaw",
    "/root/.openclaw",
)


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


def _json_has_openai_oauth(value: Any) -> bool:
    if isinstance(value, dict):
        provider = str(value.get("provider") or "").strip().lower()
        auth_type = str(value.get("type") or "").strip().lower()
        if provider == "openai" and (
            auth_type == "oauth"
            or any(
                key in value
                for key in ("access", "access_token", "refresh", "refresh_token")
            )
        ):
            return True
        return any(
            ("openai:" in str(key).lower()) or _json_has_openai_oauth(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_json_has_openai_oauth(item) for item in value)
    if isinstance(value, str):
        lowered = value.lower()
        return "openai" in lowered and ("oauth" in lowered or "access" in lowered)
    return False


def _openclaw_auth_db_candidates(
    source_roots: tuple[str, ...] | None = None,
) -> list[Path]:
    roots = source_roots or OPENCLAW_AUTH_SOURCE_ROOTS
    candidates: list[Path] = []
    seen: set[str] = set()
    for raw_root in roots:
        root = Path(raw_root).expanduser()
        if not root.exists():
            continue
        for pattern in ("**/openclaw-agent.sqlite", "**/*.sqlite", "**/*.db"):
            for path in root.glob(pattern):
                key = str(path)
                if key not in seen:
                    seen.add(key)
                    candidates.append(path)
    return candidates


def _sqlite_has_openai_oauth(path: Path) -> bool:
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return False
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "auth_profile_store" not in tables:
            return False
        cursor = connection.execute("SELECT * FROM auth_profile_store LIMIT 100")
        for row in cursor.fetchall():
            for cell in row:
                if isinstance(cell, bytes):
                    try:
                        cell = cell.decode("utf-8", errors="replace")
                    except Exception:
                        continue
                if not isinstance(cell, str):
                    continue
                try:
                    parsed = json.loads(cell)
                except json.JSONDecodeError:
                    parsed = cell
                if _json_has_openai_oauth(parsed):
                    return True
        return False
    except sqlite3.Error:
        return False
    finally:
        connection.close()


def _openclaw_codex_auth_summary(
    source_roots: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    # Migration-only, read-only legacy inspection. The public OpenClaw migrator
    # does not expose "had OpenAI OAuth" as a separate signal, so this keeps the
    # platform on the supported Hermes reconnect path without returning values.
    candidates = _openclaw_auth_db_candidates(source_roots)
    matches = sum(1 for path in candidates if _sqlite_has_openai_oauth(path))
    return {
        "present": matches > 0,
        "source": "openclaw_auth_profile_store",
        "database_count_checked": len(candidates),
        "profile_store_match_count": matches,
        "values_returned": False,
    }


def _maybe_start_openclaw_reconnect(openclaw_auth: dict[str, Any]) -> dict[str, Any]:
    if not openclaw_auth.get("present"):
        return {"started": False, "reason": "openclaw_codex_auth_not_found"}
    return start_openclaw_migration_reconnect()


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
        openclaw_auth = _openclaw_codex_auth_summary()
        codex_reconnect = _maybe_start_openclaw_reconnect(openclaw_auth)
        if openclaw_auth.get("present"):
            if codex_reconnect.get("started"):
                message = (
                    "No reusable Hermes OpenAI Codex auth was found, but an older "
                    "OpenClaw Codex/OpenAI login exists on this Computer. Hermes "
                    "needs a fresh Codex sign-in, so the reconnect flow was "
                    "started."
                )
            else:
                message = (
                    "No reusable Hermes OpenAI Codex auth was found, but an older "
                    "OpenClaw Codex/OpenAI login exists on this Computer. Hermes "
                    "needs a fresh Codex sign-in, but the reconnect flow could "
                    "not be started yet."
                )
        else:
            message = (
                "No existing OpenAI Codex auth was found on this Computer; "
                "leaving the current Hermes model configuration unchanged."
            )
        return {
            "schema": SCHEMA,
            "activated": False,
            "status": "skipped",
            "reason": "codex_auth_not_found",
            "message": message,
            "model_provider": MODEL_PROVIDER,
            "hermes": {
                "hermes_bin": str(hermes_bin),
                "auth_status": hermes_auth_status,
            },
            "codex_cli_status": codex_cli_status,
            "openclaw_auth": openclaw_auth,
            "codex_reconnect": codex_reconnect,
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
