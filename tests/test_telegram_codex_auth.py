"""Focused tests for Telegram-side OpenAI Codex auth helper."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hermes_runtime.telegram_codex_auth as codex_auth  # noqa: E402


def test_extract_auth_material_finds_url_and_device_code() -> None:
    material = codex_auth._extract_auth_material(
        "Open https://auth.openai.com/device and enter code ABCD-EFGH"
    )

    assert material == {
        "url": "https://auth.openai.com/device",
        "code": "ABCD-EFGH",
    }


def test_extract_auth_material_does_not_treat_provider_name_as_code() -> None:
    material = codex_auth._extract_auth_material("Unknown provider: codex-oauth")

    assert material == {"url": None, "code": None}


def test_telegram_credentials_fall_back_to_hermes_env_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        env_file = home / ".hermes" / ".env"
        env_file.parent.mkdir(parents=True)
        env_file.write_text(
            'TELEGRAM_BOT_TOKEN="123:abc"\n'
            'TELEGRAM_HOME_CHANNEL="555111"\n',
            encoding="utf-8",
        )
        old_env = os.environ.copy()
        os.environ.clear()
        os.environ.update({"HOME": tmp})
        try:
            credentials = codex_auth._telegram_credentials()
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    assert credentials == ("123:abc", "555111")


def test_send_auth_material_uses_button_and_bare_code_message() -> None:
    sends: list[tuple[str, str | None, str | None]] = []

    def fake_send(
        text: str,
        *,
        button_text: str | None = None,
        button_url: str | None = None,
    ) -> dict[str, object]:
        sends.append((text, button_text, button_url))
        return {"ok": True}

    with patch("hermes_runtime.telegram_codex_auth._telegram_send", fake_send):
        codex_auth._send_auth_material(
            {
                "url": "https://auth.openai.com/device",
                "code": "ABCD-EFGH",
            },
            "codex-oauth",
        )

    assert sends == [
        (
            "OpenAI Codex auth is ready. Open the authorization page, then paste the code from the next message.",
            "Open OpenAI auth",
            "https://auth.openai.com/device",
        ),
        ("ABCD-EFGH", None, None),
    ]


def test_start_spawns_worker_without_waiting_for_device_flow() -> None:
    calls: list[list[str]] = []

    class FakePopen:
        def __init__(self, args: list[str], **_kwargs: object) -> None:
            calls.append(args)

    with tempfile.TemporaryDirectory() as tmp:
        old_env = os.environ.copy()
        os.environ.update({"HOME": tmp})
        try:
            with patch("hermes_runtime.telegram_codex_auth.subprocess.Popen", FakePopen):
                message = codex_auth.start()
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    assert "starting OpenAI Codex auth" in message
    assert calls == [
        [
            "bash",
            "-lc",
            'PYTHONPATH="${TINYHAT_RUNTIME_PREFIX:-/opt/tinyhat-hermes-runtime}:${PYTHONPATH:-}" '
            "python3 -m hermes_runtime.telegram_codex_auth worker",
        ]
    ]


def test_worker_restarts_gateway_after_successful_device_auth() -> None:
    sent: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        old_env = os.environ.copy()
        os.environ.update({"HOME": tmp})
        try:
            with (
                patch(
                    "hermes_runtime.telegram_codex_auth.find_hermes_binary",
                    return_value=Path("/usr/local/bin/hermes"),
                ),
                patch(
                    "hermes_runtime.telegram_codex_auth._run_auth_once",
                    return_value=(0, True),
                ),
                patch(
                    "hermes_runtime.telegram_codex_auth._run_config_switch",
                    return_value={"ok": True},
                ),
                patch(
                    "hermes_runtime.telegram_codex_auth._restart_gateway_after_auth",
                    return_value={"healthy": True, "started": True},
                ),
                patch(
                    "hermes_runtime.telegram_codex_auth._auth_status",
                    return_value={"ok": True, "provider": "codex-oauth"},
                ),
                patch(
                    "hermes_runtime.telegram_codex_auth._telegram_send",
                    side_effect=lambda text, **_kwargs: sent.append(text)
                    or {"ok": True},
                ),
            ):
                exit_code = codex_auth.worker()
                status = codex_auth._read_status()
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    assert exit_code == 0
    assert status is not None
    assert status["state"] == "connected"
    assert status["gateway_restart"]["healthy"] is True
    assert any("restarted my Telegram gateway" in text for text in sent)


def test_status_reports_connected_state_without_exposing_tokens() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        old_env = os.environ.copy()
        os.environ.update({"HOME": tmp})
        try:
            codex_auth._write_status(
                {
                    "state": "connected",
                    "message": "OpenAI Codex auth connected.",
                }
            )
            with (
                patch(
                    "hermes_runtime.telegram_codex_auth.find_hermes_binary",
                    return_value=Path("/usr/local/bin/hermes"),
                ),
                patch(
                    "hermes_runtime.telegram_codex_auth._auth_status",
                    return_value={"ok": True, "provider": "codex-oauth"},
                ),
            ):
                output = codex_auth.status()
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    assert "State: connected" in output
    assert "Hermes auth: ok" in output
    assert "token" not in output.lower()
