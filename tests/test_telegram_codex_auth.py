"""Focused tests for Telegram-side OpenAI Codex auth helper."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hermes_runtime.telegram_codex_auth as codex_auth  # noqa: E402


def load_tests(
    loader: unittest.TestLoader,
    tests: unittest.TestSuite,
    pattern: str | None,
) -> unittest.TestSuite:
    del loader, tests, pattern
    suite = unittest.TestSuite()
    module = sys.modules[__name__]
    for name, value in sorted(vars(module).items()):
        if name.startswith("test_") and callable(value):
            suite.addTest(unittest.FunctionTestCase(value))
    return suite


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


def test_extract_auth_material_rejects_lookalike_device_hosts() -> None:
    material = codex_auth._extract_auth_material(
        "Open https://evilopenai.com/device and enter code ABCD-EFGH"
    )

    assert material == {"url": None, "code": "ABCD-EFGH"}


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


def test_auth_status_treats_zero_exit_logged_out_output_as_not_connected() -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        provider = args[-1]
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=f"{provider}: logged out (No Codex credentials stored.)",
            stderr="",
        )

    with patch("hermes_runtime.telegram_codex_auth.subprocess.run", fake_run):
        result = codex_auth._auth_status(Path("/usr/local/bin/hermes"))

    assert result["ok"] is False
    assert result["provider"] == "codex-oauth"
    assert result["logged_out"] is True
    assert calls == [
        ["/usr/local/bin/hermes", "auth", "status", "openai-codex"],
        ["/usr/local/bin/hermes", "auth", "status", "codex-oauth"],
    ]


def test_openclaw_migration_reconnect_sends_notice_then_starts_codex_auth() -> None:
    sent: list[str] = []
    started = Mock(return_value="I am starting OpenAI Codex auth now.")

    with (
        patch(
            "hermes_runtime.telegram_codex_auth._telegram_send",
            side_effect=lambda text, **_kwargs: sent.append(text) or {"ok": True},
        ),
        patch("hermes_runtime.telegram_codex_auth.start", started),
    ):
        result = codex_auth.start_openclaw_migration_reconnect()

    assert result["started"] is True
    assert "starting OpenAI Codex auth" in result["message"]
    assert len(sent) == 1
    assert "needs one quick reconnect for Hermes" in sent[0]
    assert "cannot be safely reused by Hermes directly" in sent[0]
    started.assert_called_once_with()


def test_openclaw_migration_reconnect_without_telegram_does_not_start_auth() -> None:
    started = Mock()

    with (
        patch(
            "hermes_runtime.telegram_codex_auth._telegram_send",
            side_effect=RuntimeError("Telegram is not configured."),
        ),
        patch("hermes_runtime.telegram_codex_auth.start", started),
    ):
        result = codex_auth.start_openclaw_migration_reconnect()

    assert result == {
        "started": False,
        "reason": "telegram_not_configured",
        "message": "Telegram is not configured.",
    }
    started.assert_not_called()


def test_openclaw_migration_reconnect_does_not_duplicate_notice_when_worker_runs() -> None:
    telegram_send = Mock(return_value={"ok": True})
    started = Mock(
        return_value=(
            "OpenAI Codex auth is already running. I will send the auth link "
            "and completion message here."
        )
    )

    with (
        patch("hermes_runtime.telegram_codex_auth._running_worker_pid", return_value=1234),
        patch("hermes_runtime.telegram_codex_auth._telegram_send", telegram_send),
        patch("hermes_runtime.telegram_codex_auth.start", started),
    ):
        result = codex_auth.start_openclaw_migration_reconnect()

    assert result["started"] is True
    assert result["reason"] == "already_running"
    telegram_send.assert_not_called()
    started.assert_called_once_with()


def test_codex_cli_login_command_uses_device_auth() -> None:
    command = codex_auth._codex_cli_login_command(Path("/usr/local/bin/codex"))

    assert command == ["/usr/local/bin/codex", "login", "--device-auth"]


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
        "cli_import": False,
        "model": True,
    }


def test_model_switch_imports_existing_codex_cli_credentials() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        hermes_bin = Path(tmp) / "hermes"
        hermes_bin.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "print('Select provider:', flush=True)\n"
            "print('  1. OpenAI ▸ (Codex CLI or direct OpenAI API)', flush=True)\n"
            "first = sys.stdin.buffer.readline()\n"
            "print('Select OpenAI provider:', flush=True)\n"
            "print('  → (●) OpenAI Codex', flush=True)\n"
            "second = sys.stdin.buffer.readline()\n"
            "print('Found existing Codex CLI credentials at ~/.codex/auth.json', flush=True)\n"
            "print('Import these credentials? (a separate login is recommended) [y/N]: ', flush=True)\n"
            "third = sys.stdin.buffer.readline()\n"
            "print('Select default model:', flush=True)\n"
            "fourth = sys.stdin.buffer.readline()\n"
            "assert first.strip() == b'1'\n"
            "assert second and third.strip() == b'y' and fourth\n"
            "print('Default model set to: gpt-5.5', flush=True)\n",
            encoding="utf-8",
        )
        hermes_bin.chmod(0o755)

        result = codex_auth._run_config_switch(hermes_bin)

    assert result["ok"] is True
    assert result["selections"]["cli_import"] is True
    assert result["selections"]["credentials"] is True


def test_model_switch_uses_arrow_fallback_for_unnumbered_provider_menu() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        hermes_bin = Path(tmp) / "hermes"
        hermes_bin.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "print('Select provider:', flush=True)\n"
            "print('  (○) Nous Portal', flush=True)\n"
            "print('  → (●) OpenRouter (Pay-per-use API aggregator)  ← currently active', flush=True)\n"
            "print('  (○) Anthropic', flush=True)\n"
            "print('  (○) OpenAI ▸ (Codex CLI or direct OpenAI API)', flush=True)\n"
            "first = sys.stdin.buffer.readline()\n"
            "print('Select OpenAI provider:', flush=True)\n"
            "print('  → (●) OpenAI Codex', flush=True)\n"
            "second = sys.stdin.buffer.readline()\n"
            "print('OpenAI Codex credentials:', flush=True)\n"
            "third = sys.stdin.buffer.readline()\n"
            "print('Select default model:', flush=True)\n"
            "fourth = sys.stdin.buffer.readline()\n"
            "assert first == b'\\x1b[B\\x1b[B\\n'\n"
            "assert second and third and fourth\n"
            "print('Default model set to: gpt-5.5', flush=True)\n",
            encoding="utf-8",
        )
        hermes_bin.chmod(0o755)

        result = codex_auth._run_config_switch(hermes_bin)

    assert result["ok"] is True
    assert result["model_default"] == "gpt-5.5"
    assert result["selections"]["provider"] is True


def test_model_switch_redacts_picker_output_before_status_storage() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        hermes_bin = Path(tmp) / "hermes"
        hermes_bin.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "print('Select provider:', flush=True)\n"
            "print('  1. OpenAI ▸ (Codex CLI or direct OpenAI API)', flush=True)\n"
            "sys.stdin.buffer.readline()\n"
            "print('Select OpenAI provider:', flush=True)\n"
            "sys.stdin.buffer.readline()\n"
            "print('OpenAI Codex credentials:', flush=True)\n"
            "sys.stdin.buffer.readline()\n"
            "print('Select default model:', flush=True)\n"
            "sys.stdin.buffer.readline()\n"
            "print('access_token=sk-secretvalue1234567890abcdef', flush=True)\n"
            "print('Default model set to: gpt-5.5', flush=True)\n",
            encoding="utf-8",
        )
        hermes_bin.chmod(0o755)

        result = codex_auth._run_config_switch(hermes_bin)

    assert result["ok"] is True
    assert "sk-secretvalue" not in result["output"]
    assert "[redacted]" in result["output"]


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
    hermes_auth_fallback = Mock(return_value=(0, True))
    multimedia_config = Mock(return_value={"ok": True, "commands": []})

    def fake_gateway_restart(_hermes_bin: Path) -> dict[str, bool]:
        sent.append("__gateway_restart__")
        return {"healthy": True, "started": True}

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
                    "hermes_runtime.telegram_codex_auth.find_codex_binary",
                    return_value=Path("/usr/local/bin/codex"),
                ),
                patch(
                    "hermes_runtime.telegram_codex_auth._ensure_codex_cli_auth",
                    return_value=(0, False, {"ok": True}),
                ),
                patch(
                    "hermes_runtime.telegram_codex_auth._run_auth_once",
                    hermes_auth_fallback,
                ),
                patch(
                    "hermes_runtime.telegram_codex_auth._run_config_switch",
                    return_value={
                        "ok": True,
                        "model_provider": "openai-codex",
                        "model_default": "gpt-5.5",
                    },
                ),
                patch(
                    "hermes_runtime.telegram_codex_auth._restart_gateway_after_auth",
                    side_effect=fake_gateway_restart,
                ),
                patch(
                    "hermes_runtime.telegram_codex_auth._configure_multimedia_after_auth",
                    multimedia_config,
                ),
                patch(
                    "hermes_runtime.telegram_codex_auth._auth_status",
                    return_value={"ok": True, "provider": "codex-oauth"},
                ),
                patch(
                    "hermes_runtime.telegram_codex_auth._codex_cli_status",
                    return_value={"ok": True},
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
    assert status["codex_cli_status"]["ok"] is True
    assert status["multimedia_config"]["ok"] is True
    assert status["gateway_restart_notice"]["ok"] is True
    assert status["gateway_restart"]["healthy"] is True
    hermes_auth_fallback.assert_not_called()
    multimedia_config.assert_called_once_with(
        Path("/usr/local/bin/hermes"),
        codex_chat_model="gpt-5.5",
    )
    restart_notice = "OpenAI Codex is connected. I'm restarting now."
    completion = "Ready ✅"
    assert restart_notice in sent
    notice_index = sent.index(restart_notice)
    gateway_index = sent.index("__gateway_restart__")
    assert completion in sent
    completion_index = sent.index(completion)
    assert notice_index < gateway_index < completion_index
    assert not any(
        "Voice transcription is now set to OpenAI Codex STT" in text
        for text in sent
    )


def test_reconnect_runs_fresh_cli_flows_even_when_saved_status_is_logged_in() -> None:
    """Exercise the real PTY adapters, not just mocked worker success values."""
    for failure in ("", "codex", "hermes", "model", "cached"):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = root / "calls"
            script = "\n".join([
                f"#!{sys.executable}",
                "import pathlib, sys",
                f"calls = pathlib.Path({str(calls)!r})",
                "name = pathlib.Path(sys.argv[0]).name",
                "args = sys.argv[1:]",
                "with calls.open('a') as f: f.write(name + ' ' + ' '.join(args) + '\\n')",
                f"failure = {failure!r}",
                "if name == 'codex':",
                "    if args == ['login', 'status']: print('Logged in using ChatGPT')",
                "    elif args == ['login', '--device-auth']:",
                "        sys.exit(1 if failure == 'codex' else 0)",
                "    else: sys.exit(9)",
                "elif args[:2] == ['auth', 'status']:",
                "    print('openai-codex: logged in')",
                "elif args == ['model', '--no-browser']:",
                "    print('Select provider:\\n  1. OpenAI (Codex CLI)', flush=True)",
                "    input()",
                "    print('Existing Codex credentials found in Hermes auth store.\\nUse existing credentials? [Y/n]:', flush=True)",
                "    assert input().strip() == 'n'",
                "    print('Found existing Codex CLI credentials\\nImport these credentials?', flush=True)",
                "    assert input().strip() == 'n'",
                "    if failure == 'hermes': sys.exit(1)",
                "    if failure != 'cached': print('Open https://auth.openai.com/codex/device and enter code ABCD-EFGH', flush=True)",
                "    if failure == 'model': sys.exit(1)",
                "    print('Select default model:', flush=True)",
                "    input()",
                "    print('Default model set to: gpt-5.5', flush=True)",
                "else: sys.exit(9)",
            ]) + "\n"
            for name in ("codex", "hermes"):
                binary = root / name
                binary.write_text(script)
                binary.chmod(0o755)
            sent: list[str] = []
            restart = Mock(return_value={"healthy": True})
            with (
                patch.dict(os.environ, {"HOME": tmp}),
                patch.object(codex_auth, "find_hermes_binary", return_value=root / "hermes"),
                patch.object(codex_auth, "find_codex_binary", return_value=root / "codex"),
                patch.object(codex_auth, "AUTH_TIMEOUT_SECONDS", 5),
                patch.object(codex_auth, "_configure_multimedia_after_auth", return_value={"ok": True}),
                patch.object(codex_auth, "_restart_gateway_after_auth", restart),
                patch.object(codex_auth, "_telegram_send", side_effect=lambda text, **kwargs: sent.append(text) or {"ok": True}),
            ):
                result = codex_auth.worker()
                status = codex_auth._read_status()
            invoked = calls.read_text().splitlines()
            assert invoked[0] == "codex login --device-auth", (failure, invoked)
            assert result == (1 if failure else 0), (failure, invoked)
            assert status["state"] == ("failed" if failure else "connected")
            if failure == "codex":
                assert "hermes model --no-browser" not in invoked
            else:
                assert invoked.count("hermes model --no-browser") == 1
            assert not any(line.startswith("hermes auth add") for line in invoked)
            if failure:
                restart.assert_not_called()
                assert "Ready ✅" not in sent
            else:
                restart.assert_called_once()
                assert "Ready ✅" in sent


def test_reconnect_selects_new_login_in_both_official_credential_menus() -> None:
    for numbered in (False, True):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "hermes"
            prompt = ("1. Use existing credentials\n2. Reauthenticate (new OAuth login)"
                      if numbered else "→ (●) Use existing credentials\n(○) Reauthenticate (new OAuth login)")
            expected = "2" if numbered else "\x1b[B"
            binary.write_text(
                f"#!{sys.executable}\n"
                "import sys\n"
                "print('Select provider: 1. OpenAI (Codex CLI)', flush=True)\n"
                "input()\n"
                f"print('OpenAI Codex credentials:\\n' + {prompt!r}, flush=True)\n"
                f"assert input() == {expected!r}\n"
                "print('Open https://auth.openai.com/codex/device and enter code ABCD-EFGH', flush=True)\n"
                "print('Select default model:', flush=True)\n"
                "input()\n"
                "print('Default model set to: gpt-5.5', flush=True)\n"
            )
            binary.chmod(0o755)
            with (
                patch.dict(os.environ, {"HOME": tmp}),
                patch.object(codex_auth, "AUTH_TIMEOUT_SECONDS", 5),
                patch.object(codex_auth, "_telegram_send", return_value={"ok": True}) as send,
            ):
                result = codex_auth._run_config_switch(binary, reconnect=True)
            assert result["ok"], result
            assert result["device_auth_requested"]
            assert send.call_count == 2
            assert "ABCD-EFGH" not in result["output"]


def test_reconnect_log_recovers_hermes_prompt_and_login_errors() -> None:
    for has_prompt in (False, True):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "hermes"
            output = ("Open https://auth.openai.com/codex/device and enter code ABCD-EFGH\n"
                      if has_prompt else "") + "Login failed: device authorization HTTP 429"
            binary.write_text(
                f"#!{sys.executable}\n"
                "import sys\n"
                "print('Select provider: 1. OpenAI (Codex CLI)', flush=True)\n"
                "input()\n"
                f"print({output!r}, flush=True)\n"
                "sys.exit(1)\n"
            )
            binary.chmod(0o755)
            with (
                patch.dict(os.environ, {"TINYHAT_CODEX_AUTH_STATE_DIR": str(Path(tmp) / "state")}),
                patch.object(codex_auth, "AUTH_TIMEOUT_SECONDS", 5),
                patch.object(codex_auth, "_telegram_send", return_value={"ok": False, "description": "temporary delivery failure"}),
            ):
                result = codex_auth._run_config_switch(binary, reconnect=True)
                log = codex_auth._read_log()
                status = codex_auth._read_status()
                assert codex_auth._log_path().stat().st_mode & 0o777 == 0o600
            assert not result["ok"]
            assert "Login failed: device authorization HTTP 429" in log
            if has_prompt:
                assert "ABCD-EFGH" in log  # The owner can request a resend.
                assert "ABCD-EFGH" not in result["output"]
                assert status["state"] == "delivery_failed"
                assert "Hermes Codex auth code" in status["message"]
                assert not status["telegram_delivery"]["ok"]
            else:
                assert not result["device_auth_requested"]


def test_completion_message_keeps_multimedia_failure_actionable() -> None:
    assert codex_auth._completion_message(
        switch={"ok": True},
        gateway={"healthy": True},
        multimedia={"ok": False},
    ) == (
        "OpenAI Codex auth is connected ✅\n\n"
        "I could not confirm the image/voice model config; "
        "send /codex_auth_status if media still fails."
    )


def test_worker_stops_when_codex_cli_auth_fails_before_touching_hermes_auth() -> None:
    sent: list[str] = []
    hermes_auth_fallback = Mock(return_value=(0, True))
    config_switch = Mock(return_value={"ok": True, "model_provider": "openai-codex"})
    gateway_restart = Mock(return_value={"healthy": True, "started": True})

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
                    "hermes_runtime.telegram_codex_auth.find_codex_binary",
                    return_value=Path("/usr/local/bin/codex"),
                ),
                patch(
                    "hermes_runtime.telegram_codex_auth._ensure_codex_cli_auth",
                    return_value=(1, True, {"ok": False, "message": "not connected"}),
                ),
                patch(
                    "hermes_runtime.telegram_codex_auth._run_auth_once",
                    hermes_auth_fallback,
                ),
                patch(
                    "hermes_runtime.telegram_codex_auth._run_config_switch",
                    config_switch,
                ),
                patch(
                    "hermes_runtime.telegram_codex_auth._restart_gateway_after_auth",
                    gateway_restart,
                ),
                patch(
                    "hermes_runtime.telegram_codex_auth._configure_multimedia_after_auth",
                    return_value={"ok": True, "commands": []},
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

    assert exit_code == 1
    assert status is not None
    assert status["state"] == "failed"
    assert status["provider"] == "codex-cli"
    assert "did not complete" in status["message"]
    hermes_auth_fallback.assert_not_called()
    config_switch.assert_not_called()
    gateway_restart.assert_not_called()
    assert any("Codex CLI auth did not complete" in text for text in sent)


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
                    "hermes_runtime.telegram_codex_auth.find_codex_binary",
                    return_value=Path("/usr/local/bin/codex"),
                ),
                patch(
                    "hermes_runtime.telegram_codex_auth._auth_status",
                    return_value={"ok": True, "provider": "codex-oauth"},
                ),
                patch(
                    "hermes_runtime.telegram_codex_auth._codex_cli_status",
                    return_value={"ok": True},
                ),
            ):
                output = codex_auth.status()
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    assert "State: connected" in output
    assert "Hermes auth: ok" in output
    assert "Codex CLI auth: ok" in output
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
