"""Apply Tinyhat runtime config changes to Hermes Agent.

The Tinyhat platform queues this command when a user saves a runtime secret in
the settings Mini App. Hermes stores those values in its normal env files, then
reloads the updated env entries into this Python process. Current Hermes gateway
builds reload ``~/.hermes/.env`` before each turn, so a saved secret becomes
available to the next chat shell command without restarting the Telegram
gateway.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import re
from typing import Any

from hermes_runtime.commands.configure_telegram import (
    _env_file_candidates,
    _quote_env,
    _run_gateway,
)
from hermes_runtime.hermes_cli import find_hermes_binary
from hermes_runtime.platform_paths import context_computer_api_path
from hermes_runtime.runtime_env import load_env_files_into_process
from hermes_runtime.telegram_codex_auth import _telegram_send


SCHEMA = "tinyhat_hermes_apply_config_v1"
RUNTIME_SECRETS_START = "# tinyhat runtime secrets start"
RUNTIME_SECRETS_END = "# tinyhat runtime secrets end"
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _clean_secret_map(payload: dict[str, Any]) -> dict[str, str]:
    secrets = payload.get("secrets")
    if not isinstance(secrets, dict):
        return {}
    cleaned: dict[str, str] = {}
    for raw_key, raw_value in secrets.items():
        key = str(raw_key or "").strip()
        if not key or raw_value is None:
            continue
        if not ENV_NAME_RE.fullmatch(key):
            raise RuntimeError("Platform returned an invalid runtime secret name.")
        cleaned[key] = str(raw_value)
    return cleaned


def _read_managed_secret_keys(lines: list[str]) -> set[str]:
    keys: set[str] = set()
    in_managed_block = False
    for line in lines:
        clean = line.strip()
        if clean == RUNTIME_SECRETS_START:
            in_managed_block = True
            continue
        if clean == RUNTIME_SECRETS_END:
            in_managed_block = False
            continue
        if not in_managed_block or not clean or clean.startswith("#") or "=" not in clean:
            continue
        key, _raw_value = clean.split("=", 1)
        key = key.strip()
        if key and ENV_NAME_RE.fullmatch(key):
            keys.add(key)
    return keys


def _write_runtime_secret_env_file(path: Path, values: dict[str, str]) -> dict[str, Any]:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    previous_keys = _read_managed_secret_keys(existing)
    next_keys = set(values)
    next_lines: list[str] = []
    in_managed_block = False
    for line in existing:
        if line.strip() == RUNTIME_SECRETS_START:
            in_managed_block = True
            continue
        if line.strip() == RUNTIME_SECRETS_END:
            in_managed_block = False
            continue
        if in_managed_block:
            continue
        next_lines.append(line)

    while next_lines and not next_lines[-1].strip():
        next_lines.pop()
    if values:
        if next_lines:
            next_lines.append("")
        next_lines.append(RUNTIME_SECRETS_START)
        for key in sorted(values):
            next_lines.append(f"{key}={_quote_env(values[key])}")
        next_lines.append(RUNTIME_SECRETS_END)

    path.write_text("\n".join(next_lines).rstrip() + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return {
        "path": str(path),
        "updated": True,
        "keys": sorted(values),
        "previous_keys": sorted(previous_keys),
        "removed_keys": sorted(previous_keys - next_keys),
        "managed_block": "tinyhat runtime secrets",
    }


def _secret_available_notice(secret_names: list[str]) -> str:
    if len(secret_names) == 1:
        subject = f"`{secret_names[0]}` is saved"
    elif secret_names:
        subject = f"{len(secret_names)} secrets are saved"
    else:
        subject = "Your secret settings are saved"
    return (
        f"{subject}. I'm making the new secret available to Hermes now, so the "
        "next shell command can use it."
    )


async def _send_secret_available_notice(secret_names: list[str]) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(
            _telegram_send,
            _secret_available_notice(secret_names),
        )
    except Exception as exc:  # noqa: BLE001 - env apply must still complete.
        return {
            "ok": False,
            "message": str(exc),
            "failure_code": exc.__class__.__name__,
        }


def _secret_restart_notice(removed_keys: list[str]) -> str:
    if len(removed_keys) == 1:
        subject = f"`{removed_keys[0]}` was removed"
    elif removed_keys:
        subject = f"{len(removed_keys)} secrets were removed"
    else:
        subject = "Your secret settings changed"
    return (
        f"{subject}. I'm restarting my Telegram gateway now so removed secrets "
        "are no longer available in shell commands. I'll confirm once it is back."
    )


async def _send_secret_restart_notice(removed_keys: list[str]) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(
            _telegram_send,
            _secret_restart_notice(removed_keys),
        )
    except Exception as exc:  # noqa: BLE001 - env apply/restart must still run.
        return {
            "ok": False,
            "message": str(exc),
            "failure_code": exc.__class__.__name__,
        }


def _notice_result(*, sent: bool, notice: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": bool(notice.get("ok")) if sent else None,
        "sent": sent,
        "http_status": notice.get("http_status") if sent else None,
        "description": notice.get("description") if sent else None,
    }


async def run(ctx: Any, command: dict[str, Any]) -> dict[str, Any]:
    spec = command.get("spec") if isinstance(command.get("spec"), dict) else {}
    desired_revision = int(spec.get("desired_config_revision") or 0)
    api_path = context_computer_api_path(ctx, "runtime-secrets")
    payload = await ctx.platform.get_json(api_path)
    revision = int(payload.get("revision") or desired_revision)
    secrets = _clean_secret_map(payload)
    secret_names = sorted(secrets)

    env_files = [
        _write_runtime_secret_env_file(env_path, secrets)
        for env_path in _env_file_candidates()
    ]
    removed_keys = sorted(
        {
            str(key)
            for item in env_files
            for key in item.get("removed_keys", [])
        }
    )
    for key in removed_keys:
        os.environ.pop(key, None)
    env_paths = [Path(str(item["path"])) for item in env_files]
    env_reload = load_env_files_into_process(env_paths, keys=secret_names)

    restart_required = bool(removed_keys)
    if restart_required:
        hermes_bin = find_hermes_binary()
        if hermes_bin is None:
            raise RuntimeError("Hermes CLI was not found; cannot restart Hermes gateway.")
        notice = await _send_secret_restart_notice(removed_keys)
        gateway = await _run_gateway(hermes_bin)
        if not gateway.get("healthy"):
            raise RuntimeError("Hermes gateway did not report a healthy status.")
    else:
        notice = await _send_secret_available_notice(secret_names)
        gateway = {
            "restarted": False,
            "restart_required": False,
            "reason": "runtime_secret_add_or_update_does_not_require_gateway_restart",
        }

    return {
        "schema": SCHEMA,
        "revision": revision,
        "desired_config_revision": desired_revision,
        "reason": spec.get("reason") or "runtime_secrets_changed",
        "configured": True,
        "secret_count": len(secret_names),
        "secret_names": secret_names,
        "removed_secret_names": removed_keys,
        "env_files": env_files,
        "env_reload": env_reload,
        "secret_available_notice": _notice_result(
            sent=not restart_required,
            notice=notice,
        ),
        "gateway_restart_notice": _notice_result(
            sent=restart_required,
            notice=notice,
        ),
        "gateway": gateway,
        "restart_requested": restart_required,
        "systemd_restart_requested": False,
        "diagnostic": f"applied {len(secret_names)} runtime secret(s)",
    }
