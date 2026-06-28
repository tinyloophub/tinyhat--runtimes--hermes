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


def test_extract_auth_material_ignores_non_device_auth_urls_and_url_tokens() -> None:
    material = codex_auth._extract_auth_material(
        "Read https://docs.openai.com/auth/ABCDEFGH before trying again."
    )

    assert material == {"url": None, "code": None}


def test_extract_auth_material_accepts_bare_code_line_after_url() -> None:
    material = codex_auth._extract_auth_material(
        "Open https://auth.openai.com/codex/device\n\nABCD-EFGH\n"
    )

    assert material == {
        "url": "https://auth.openai.com/codex/device",
        "code": "ABCD-EFGH",
    }


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


def test_telegram_credentials_reject_comma_separated_allowed_users_without_home_channel() -> None:
    old_env = os.environ.copy()
    os.environ.clear()
    os.environ.update(
        {
            "TELEGRAM_BOT_TOKEN": "123:abc",
            "TELEGRAM_ALLOWED_USERS": "111,222",
        }
    )
    try:
        try:
            codex_auth._telegram_credentials()
        except RuntimeError as exc:
            assert "Telegram is not configured" in str(exc)
        else:
            raise AssertionError("expected RuntimeError")
    finally:
        os.environ.clear()
        os.environ.update(old_env)


def test_auth_command_uses_no_browser_device_flow_flags() -> None:
    command = codex_auth._auth_command(Path("/usr/local/bin/hermes"), "openai-codex")

    assert command == [
        "/usr/local/bin/hermes",
        "auth",
        "add",
        "openai-codex",
        "--no-browser",
        "--timeout",
        "900",
    ]


def test_model_switch_uses_formal_hermes_model_picker() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        hermes_bin = Path(tmp) / "hermes"
        hermes_bin.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "print('Select provider:', flush=True)\n"
            "print('  (○) Nous Portal', flush=True)\n"
            "print('  → (●) OpenRouter (Pay-per-use API aggregator)  ← currently active', flush=True)\n"
            "print('  (○) Mixture of Agents', flush=True)\n"
            "print('  (○) NovitaAI', flush=True)\n"
            "print('  (○) LM Studio', flush=True)\n"
            "print('  (○) Anthropic', flush=True)\n"
            "print('  (○) 7. OpenAI ▸ (Codex CLI or direct OpenAI API)', flush=True)\n"
            "first = sys.stdin.buffer.readline()\n"
            "print('Select OpenAI provider:', flush=True)\n"
            "print('  → (●) OpenAI Codex', flush=True)\n"
            "print('  (○) OpenAI API', flush=True)\n"
            "second = sys.stdin.buffer.readline()\n"
            "print('OpenAI Codex credentials:', flush=True)\n"
            "print('  → (●) Use existing credentials', flush=True)\n"
            "third = sys.stdin.buffer.readline()\n"
            "print('Select default model:', flush=True)\n"
            "print('  → (●) gpt-5.5', flush=True)\n"
            "fourth = sys.stdin.buffer.readline()\n"
            "assert first.strip() == b'7'\n"
            "assert second and third and fourth\n"
            "print('Default model set to: gpt-5.5 (via OpenAI Codex)', flush=True)\n",
            encoding="utf-8",
        )
        hermes_bin.chmod(0o755)

        result = codex_auth._run_config_switch(hermes_bin)

    assert result["ok"] is True
    assert result["source"] == "hermes model"
    assert result["model_provider"] == "openai-codex"
    assert result["model_default"] == "gpt-5.5"
    assert result["selections"] == {
        "provider": True,
        "openai_provider": True,
        "credentials": True,
        "model": True,
    }


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
        result = codex_auth._send_auth_material(
            {
                "url": "https://auth.openai.com/device",
                "code": "ABCD-EFGH",
            },
            "codex-oauth",
        )

    assert result["ok"] is True
    assert sends == [
        (
            "OpenAI Codex auth is ready. Open the authorization page, then paste the code from the next message.",
            "Open OpenAI auth",
            "https://auth.openai.com/device",
        ),
        ("ABCD-EFGH", None, None),
    ]


def test_send_auth_material_reports_telegram_delivery_failure() -> None:
    def fake_send(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"ok": False, "description": "rate limited"}

    with tempfile.TemporaryDirectory() as tmp:
        old_env = os.environ.copy()
        os.environ.update({"HOME": tmp})
        try:
            with patch("hermes_runtime.telegram_codex_auth._telegram_send", fake_send):
                result = codex_auth._send_auth_material(
                    {
                        "url": "https://auth.openai.com/device",
                        "code": "ABCD-EFGH",
                    },
                    "openai-codex",
                )
                log = codex_auth._read_log()
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    assert result["ok"] is False
    assert "Telegram delivery failed" in log


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


def test_start_uses_start_lock_to_avoid_duplicate_workers() -> None:
    calls: list[list[str]] = []

    class FakePopen:
        def __init__(self, args: list[str], **_kwargs: object) -> None:
            calls.append(args)

    with tempfile.TemporaryDirectory() as tmp:
        old_env = os.environ.copy()
        os.environ.update({"HOME": tmp})
        try:
            with patch("hermes_runtime.telegram_codex_auth.subprocess.Popen", FakePopen):
                first = codex_auth.start()
                second = codex_auth.start()
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    assert "starting OpenAI Codex auth" in first
    assert "already starting" in second
    assert len(calls) == 1


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
                    return_value={"ok": True, "model_provider": "openai-codex"},
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
    assert status["provider"] == "openai-codex"
    assert status["model_provider"] == "openai-codex"
    assert status["gateway_restart"]["healthy"] is True
    assert any("restarted my Telegram gateway" in text for text in sent)


def test_worker_uses_openai_codex_model_provider_after_fallback_auth_alias() -> None:
    switch_calls: list[Path] = []

    def fake_auth_once(_hermes_bin: Path, provider: str) -> tuple[int, bool]:
        if provider == "openai-codex":
            return 1, False
        return 0, True

    def fake_switch(hermes_bin: Path) -> dict[str, object]:
        switch_calls.append(hermes_bin)
        return {"ok": True, "model_provider": "openai-codex"}

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
                    side_effect=fake_auth_once,
                ),
                patch(
                    "hermes_runtime.telegram_codex_auth._run_config_switch",
                    side_effect=fake_switch,
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
                    return_value={"ok": True},
                ),
            ):
                exit_code = codex_auth.worker()
                status = codex_auth._read_status()
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    assert exit_code == 0
    assert switch_calls == [Path("/usr/local/bin/hermes")]
    assert status is not None
    assert status["provider"] == "codex-oauth"
    assert status["model_provider"] == "openai-codex"


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


def test_log_redacts_token_like_values() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        old_env = os.environ.copy()
        os.environ.update({"HOME": tmp})
        try:
            codex_auth._append_log("access_token=sk-secretvalue1234567890abcdef\n")
            output = codex_auth.log()
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    assert "sk-secretvalue" not in output
    assert "[redacted]" in output
