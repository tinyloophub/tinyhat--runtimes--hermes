"""Send the Tinyhat settings Mini App button to Telegram.

Hermes invokes this module through the ``/tinyhat_settings`` quick command.
It does one small thing: read the Telegram bot/home-channel config and the
platform-provided settings Mini App URL, then send a Telegram Web App button.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib import error, parse, request


def _env_files() -> list[Path]:
    candidates: list[Path] = []
    explicit = (os.getenv("HERMES_ENV_FILE") or "").strip()
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(Path.home() / ".hermes" / ".env")
    project_dir = Path(
        (os.getenv("HERMES_PROJECT_DIR") or "/usr/local/lib/hermes-agent").strip()
    )
    candidates.append(project_dir / ".env")

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            unique.append(candidate)
            seen.add(key)
    return unique


def _parse_env_value(raw: str) -> str:
    value = raw.strip()
    if (
        len(value) >= 2
        and value[0] == value[-1]
        and value.startswith(("'", '"'))
    ):
        value = value[1:-1]
    return value.replace('\\"', '"').replace("\\\\", "\\")


def _env_values() -> dict[str, str]:
    values = {
        "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN") or "",
        "TELEGRAM_HOME_CHANNEL": os.getenv("TELEGRAM_HOME_CHANNEL") or "",
        "TELEGRAM_ALLOWED_USERS": os.getenv("TELEGRAM_ALLOWED_USERS") or "",
        "TINYHAT_SETTINGS_MINIAPP_URL": os.getenv("TINYHAT_SETTINGS_MINIAPP_URL")
        or "",
    }
    for path in _env_files():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            clean = line.strip()
            if not clean or clean.startswith("#") or "=" not in clean:
                continue
            key, raw_value = clean.split("=", 1)
            key = key.strip()
            if key in values and not values[key]:
                values[key] = _parse_env_value(raw_value)
    return values


def _telegram_credentials() -> tuple[str, str]:
    values = _env_values()
    token = values["TELEGRAM_BOT_TOKEN"].strip()
    chat_id = values["TELEGRAM_HOME_CHANNEL"].strip()
    if not chat_id:
        allowed_users = values["TELEGRAM_ALLOWED_USERS"].strip()
        if allowed_users and "," not in allowed_users:
            chat_id = allowed_users
    if not token or not chat_id:
        raise RuntimeError("Telegram is not configured for this Hermes instance yet.")
    return token, chat_id


def _settings_url() -> str:
    url = _env_values()["TINYHAT_SETTINGS_MINIAPP_URL"].strip()
    if not url:
        raise RuntimeError("Tinyhat settings Mini App URL is not configured yet.")
    if not url.lower().startswith("https://"):
        raise RuntimeError("Tinyhat settings Mini App URL must be HTTPS.")
    return url


def _telegram_send_web_app_button(
    *,
    token: str,
    chat_id: str,
    text: str,
    button_text: str,
    web_app_url: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text[:3900],
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [
                [{"text": button_text, "web_app": {"url": web_app_url}}]
            ]
        },
    }
    body = parse.urlencode(
        {
            key: json.dumps(value) if isinstance(value, dict) else str(value)
            for key, value in payload.items()
        }
    ).encode("utf-8")
    req = request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "tinyhat-hermes-runtime/telegram-settings",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "http_status": exc.code, "description": detail[:500]}
    except error.URLError as exc:
        return {"ok": False, "description": str(exc.reason)[:500]}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "description": "Telegram returned invalid JSON."}
    return payload if isinstance(payload, dict) else {"ok": False}


def open_settings() -> str:
    token, chat_id = _telegram_credentials()
    url = _settings_url()
    result = _telegram_send_web_app_button(
        token=token,
        chat_id=chat_id,
        text="Tinyhat settings",
        button_text="Open settings",
        web_app_url=url,
    )
    if not result.get("ok"):
        raise RuntimeError(
            "Telegram could not send the Tinyhat settings button: "
            f"{result.get('description') or result.get('http_status') or 'unknown'}"
        )
    return "I sent the Tinyhat settings button."


def main() -> int:
    try:
        print(open_settings())
        return 0
    except Exception as exc:
        print(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
