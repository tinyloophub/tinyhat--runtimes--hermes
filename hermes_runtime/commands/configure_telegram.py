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
    3. Applies the platform-selected model/base URL through Hermes' public
       ``hermes config set`` command.
    4. Clears Telegram's webhook for the bot so Hermes long-polling can own
       the bot connection.
    5. Starts the Hermes gateway using the public ``hermes gateway`` command.

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
from typing import Any
from urllib import error, parse, request

from hermes_runtime.hermes_cli import (
    find_hermes_binary,
    probe_hermes_status,
    run_process,
)
from hermes_runtime.platform_paths import context_computer_api_path


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
    candidates: list[Path] = []
    explicit = (os.getenv("HERMES_ENV_FILE") or "").strip()
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(Path.home() / ".hermes" / ".env")

    project_dir = Path(
        (os.getenv("HERMES_PROJECT_DIR") or "/usr/local/lib/hermes-agent").strip()
    )
    if project_dir.exists():
        candidates.append(project_dir / ".env")

    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path.expanduser())
        if key not in seen:
            unique.append(path)
            seen.add(key)
    return unique


def _openrouter_env_values(setup: dict[str, Any]) -> dict[str, str]:
    api_key = str(setup.get("openrouter_api_key") or "").strip()
    if not api_key:
        raise RuntimeError("Platform did not return OpenRouter runtime config.")
    values = {"OPENROUTER_API_KEY": api_key}
    base_url = str(setup.get("openrouter_base_url") or "").strip()
    if base_url:
        values["OPENROUTER_BASE_URL"] = base_url
    return values


async def _configure_model(hermes_bin: Path, setup: dict[str, Any]) -> dict[str, Any]:
    commands: list[tuple[str, str]] = [("model.provider", "auto")]
    default_model = str(setup.get("openrouter_default_model") or "").strip()
    if default_model:
        commands.append(("model.default", default_model))
    base_url = str(setup.get("openrouter_base_url") or "").strip()
    if base_url:
        commands.append(("model.base_url", base_url))

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
    with log_path.open("wb") as log_file:
        process = await asyncio.create_subprocess_exec(
            str(hermes_bin),
            "gateway",
            "run",
            "--replace",
            "--force",
            "--accept-hooks",
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )
    await asyncio.sleep(2)
    if process.returncode is not None:
        await process.wait()
    return {
        "mode": "foreground_detached",
        "pid": process.pid,
        "started": process.returncode is None,
        "returncode": process.returncode,
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
    path = context_computer_api_path(ctx, "hermes/telegram-setup/v1")
    setup = await ctx.platform.post_json(path, {})

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
        **_openrouter_env_values(setup),
    }
    env_files = [_upsert_env_file(path, env_values) for path in _env_file_candidates()]

    hermes_bin = find_hermes_binary()
    if hermes_bin is None:
        raise RuntimeError("Hermes CLI was not found; install Hermes first.")
    model_config = await _configure_model(hermes_bin, setup)

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
        "model_config": model_config,
        "webhook": webhook,
        "gateway": gateway,
        "hermes": _compact_hermes_status(hermes_status),
    }
