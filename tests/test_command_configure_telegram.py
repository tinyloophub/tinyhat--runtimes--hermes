"""Focused tests for the ``configure_telegram`` runtime command."""

from __future__ import annotations

import ast
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hermes_runtime.commands.configure_telegram as configure_telegram  # noqa: E402
from hermes_runtime.commands import run_command  # noqa: E402


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


class FakePlatform:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []

    async def post_json(self, path: str, payload: dict) -> dict:
        self.posts.append((path, payload))
        return {
            "schema": "tinyhat_hermes_telegram_setup_v1",
            "computer_id": 42,
            "agent_id": 7,
            "telegram_bot_token": "123456:secret-token",
            "telegram_bot_user_id": "999",
            "telegram_bot_username": "tinyhatdevtest_4_bot",
            "telegram_owner_user_id": "555111",
            "telegram_allowed_users": "555111",
            "telegram_home_channel": "555111",
            "telegram_home_channel_name": "Owner DM",
            "settings_miniapp_url": (
                "https://tinyloop-wt5.ngrok.app/tinyhat/miniapp/agents/"
                "agt_test/settings"
            ),
            "expires_at": "2026-06-26T10:00:00Z",
            "openrouter_api_key": "sk-or-v1-test-runtime-key",
            "openrouter_base_url": "https://openrouter.ai/api/v1",
            "openrouter_default_model": "deepseek/deepseek-v4-pro",
            "openrouter_model_package": {
                "default_model": "deepseek/deepseek-v4-pro"
            },
        }


def _yaml_block_list(text: str, key: str) -> list[str]:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != f"{key}:":
            continue
        items: list[str] = []
        for next_line in lines[index + 1:]:
            stripped = next_line.strip()
            if not stripped:
                continue
            if not next_line.startswith("    "):
                break
            if stripped.startswith("- "):
                items.append(stripped[2:])
        return items
    return []


def test_configure_telegram_writes_env_and_starts_gateway() -> None:
    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del timeout_seconds, env
        gateway_calls.append(args)
        events.append(f"gateway:{args[-1]}")
        return {
            "args": args,
            "returncode": 0,
            "ok": True,
            "timed_out": False,
            "duration_ms": 21,
            "stdout": "ok\n",
            "stderr": "",
        }

    async def fake_status() -> dict[str, object]:
        return {
            "schema": "tinyhat_hermes_status_v1",
            "installed": True,
            "ok": True,
            "version": "Hermes Agent 0.1.0",
            "message": "ok",
        }

    platform = FakePlatform()
    gateway_calls: list[list[str]] = []
    menu_calls: list[dict[str, object]] = []
    events: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        project = Path(tmp) / "project"
        home.mkdir()
        project.mkdir()
        old_env = os.environ.copy()
        os.environ.update(
            {
                "HOME": str(home),
                "HERMES_PROJECT_DIR": str(project),
            }
        )
        try:
            with (
                patch(
                    "hermes_runtime.commands.configure_telegram.find_hermes_binary",
                    return_value=Path("/usr/local/bin/hermes"),
                ),
                patch(
                    "hermes_runtime.commands.configure_telegram.run_process",
                    fake_run_process,
                ),
                patch(
                    "hermes_runtime.commands.configure_telegram.probe_hermes_status",
                    fake_status,
                ),
                patch(
                    "hermes_runtime.commands.configure_telegram._telegram_delete_webhook",
                    return_value={"ok": True, "description": "Webhook was deleted"},
                ),
                patch(
                    "hermes_runtime.commands.configure_telegram._telegram_set_chat_menu_button",
                    lambda token, **kwargs: menu_calls.append(
                        {"token": token, **kwargs}
                    )
                    or {"ok": True, "description": "menu set"},
                ),
            ):
                result = asyncio.run(
                    run_command(
                        SimpleNamespace(
                            platform=platform,
                            platform_auth="local_dev",
                            computer_id="local-dev",
                        ),
                        {"kind": "configure_telegram", "spec": {}},
                    )
                )
        finally:
            os.environ.clear()
            os.environ.update(old_env)

        home_env = home / ".hermes" / ".env"
        project_env = project / ".env"
        assert "TELEGRAM_BOT_TOKEN=\"123456:secret-token\"" in home_env.read_text(
            encoding="utf-8"
        )
        project_env_text = project_env.read_text(encoding="utf-8")
        assert "TELEGRAM_ALLOWED_USERS=\"555111\"" in project_env_text
        assert (
            "TINYHAT_SETTINGS_MINIAPP_URL=\"https://tinyloop-wt5.ngrok.app/"
            "tinyhat/miniapp/agents/agt_test/settings\"" in project_env_text
        )
        assert "OPENROUTER_API_KEY=\"sk-or-v1-test-runtime-key\"" in project_env_text
        config_text = (home / ".hermes" / "config.yaml").read_text(
            encoding="utf-8"
        )
        assert "quick_commands:" in config_text
        assert "terminal:" in config_text
        assert "shell_init_files:" in config_text
        assert str(home / ".hermes" / "tinyhat" / "terminal-env.sh") in config_text
        assert "plugins:" in config_text
        assert "enabled:" in config_text
        assert "tinyhat-codex" in config_text
        assert "tinyhat_settings:" in config_text
        assert "codex_auth:" in config_text
        assert "codex-auth:" in config_text
        assert "codex_auth_status:" in config_text
        assert "codex_auth_log:" in config_text
        assert "codex_limits:" in config_text
        assert "platforms:" in config_text
        assert "command_menu:" in config_text
        assert "priority_mode: prepend" in config_text
        assert "max_commands: 60" in config_text
        assert "priority:" in config_text
        assert "  - tinyhat_settings\n" in config_text
        assert "  - codex_auth\n" in config_text
        assert "  - codex_auth_status\n" in config_text
        assert "  - codex_auth_log\n" in config_text
        assert "  - codex_limits\n" in config_text
        plugin_dir = home / ".hermes" / "plugins" / "tinyhat-codex"
        assert (plugin_dir / "plugin.yaml").is_file()
        plugin_source = (plugin_dir / "__init__.py").read_text(encoding="utf-8")
        assert "ctx.register_command" in plugin_source
        assert "ctx.register_transcription_provider" in plugin_source
        assert "openai-codex-stt" in plugin_source
        assert "hermes_runtime.telegram_tinyhat_settings" in plugin_source
        assert "hermes_runtime.telegram_codex_auth" in plugin_source
        assert "hermes_runtime.codex_limits" in plugin_source

    assert platform.posts == [
        ("/hapi/v1/computers/local-dev/hermes/telegram-setup/v1", {})
    ]
    assert menu_calls == [
        {
            "token": "123456:secret-token",
            "text": "configure",
            "web_app_url": (
                "https://tinyloop-wt5.ngrok.app/tinyhat/miniapp/agents/"
                "agt_test/settings"
            ),
        },
        {
            "token": "123456:secret-token",
            "text": "configure",
            "web_app_url": (
                "https://tinyloop-wt5.ngrok.app/tinyhat/miniapp/agents/"
                "agt_test/settings"
            ),
            "chat_id": "555111",
        },
    ]
    assert gateway_calls == [
        ["/usr/local/bin/hermes", "config", "set", "model.provider", "auto"],
        [
            "/usr/local/bin/hermes",
            "config",
            "set",
            "model.default",
            "deepseek/deepseek-v4-pro",
        ],
        [
            "/usr/local/bin/hermes",
            "config",
            "set",
            "model.base_url",
            "https://openrouter.ai/api/v1",
        ],
        ["/usr/local/bin/hermes", "config", "set", "stt.enabled", "true"],
        ["/usr/local/bin/hermes", "config", "set", "stt.provider", "local"],
        ["/usr/local/bin/hermes", "config", "set", "stt.local.model", "base"],
        [
            "/usr/local/bin/hermes",
            "config",
            "set",
            "auxiliary.vision.provider",
            "auto",
        ],
        ["/usr/local/bin/hermes", "gateway", "stop"],
        ["/usr/local/bin/hermes", "gateway", "start"],
        ["/usr/local/bin/hermes", "gateway", "status"],
    ]
    assert result["configured"] is True
    assert result["bot_username"] == "tinyhatdevtest_4_bot"
    assert result["owner_user_id"] == "555111"
    assert "123456:secret-token" not in str(result)
    assert "sk-or-v1-test-runtime-key" not in str(result)
    assert result["model_config"]["ok"] is True
    assert result["terminal_env_hook"]["installed"] is True
    assert result["terminal_env_hook"]["hook"]["path"] == str(
        home / ".hermes" / "tinyhat" / "terminal-env.sh"
    )
    assert result["codex_auth"]["quick_commands"]["commands"] == [
        "tinyhat_settings",
        "codex_auth",
        "codex-auth",
        "codex_auth_status",
        "codex_auth_log",
        "codex_limits",
    ]
    assert result["codex_auth"]["quick_commands"]["telegram_menu_commands"] == [
        "tinyhat_settings",
        "codex_auth",
        "codex_auth_status",
        "codex_auth_log",
        "codex_limits",
    ]
    assert result["codex_auth"]["plugin_commands"] == {
        "config_file": str(home / ".hermes" / "config.yaml"),
        "plugin_dir": str(home / ".hermes" / "plugins" / "tinyhat-codex"),
        "installed": True,
        "enabled": True,
        "plugin": "tinyhat-codex",
        "mechanism": "hermes_plugin_register_command_and_transcription_provider",
        "commands": [
            "tinyhat_settings",
            "codex_auth",
            "codex_auth_status",
            "codex_auth_log",
            "codex_limits",
        ],
        "transcription_providers": ["openai-codex-stt"],
    }
    assert result["multimedia_config"]["commands"] == [
        {
            "key": "stt.enabled",
            "value": "true",
            "ok": True,
            "returncode": 0,
            "duration_ms": 21,
            "stdout": "ok\n",
            "stderr": "",
        },
        {
            "key": "stt.provider",
            "value": "local",
            "ok": True,
            "returncode": 0,
            "duration_ms": 21,
            "stdout": "ok\n",
            "stderr": "",
        },
        {
            "key": "stt.local.model",
            "value": "base",
            "ok": True,
            "returncode": 0,
            "duration_ms": 21,
            "stdout": "ok\n",
            "stderr": "",
        },
        {
            "key": "auxiliary.vision.provider",
            "value": "auto",
            "ok": True,
            "returncode": 0,
            "duration_ms": 21,
            "stdout": "ok\n",
            "stderr": "",
        },
    ]
    assert result["codex_auth"]["telegram_command_menu"] == {
        "config_file": str(home / ".hermes" / "config.yaml"),
        "installed": True,
        "mechanism": "hermes_config",
        "path": "platforms.telegram.extra.command_menu",
        "priority_mode": "prepend",
        "max_commands": 60,
        "commands": [
            "tinyhat_settings",
            "codex_auth",
            "codex_auth_status",
            "codex_auth_log",
            "codex_limits",
        ],
    }
    assert result["gateway"]["healthy"] is True
    assert result["gateway"]["mode"] == "service"
    assert result["menu_button"]["configured"] is True
    assert result["menu_button"]["text"] == "configure"


def test_configure_telegram_uses_gcloud_me_path() -> None:
    platform = FakePlatform()

    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del args, timeout_seconds, env
        return {
            "returncode": 0,
            "ok": True,
            "timed_out": False,
            "duration_ms": 1,
            "stdout": "",
            "stderr": "",
        }

    async def fake_status() -> dict[str, object]:
        return {
            "schema": "tinyhat_hermes_status_v1",
            "installed": True,
            "ok": True,
            "version": "Hermes Agent 0.1.0",
        }

    with (
        patch(
            "hermes_runtime.commands.configure_telegram.find_hermes_binary",
            return_value=Path("/usr/local/bin/hermes"),
        ),
        patch(
            "hermes_runtime.commands.configure_telegram.run_process",
            fake_run_process,
        ),
        patch(
            "hermes_runtime.commands.configure_telegram.probe_hermes_status",
            fake_status,
        ),
        patch(
            "hermes_runtime.commands.configure_telegram._telegram_delete_webhook",
            return_value={"ok": True},
        ),
        patch(
            "hermes_runtime.commands.configure_telegram._telegram_set_chat_menu_button",
            return_value={"ok": True},
        ),
        tempfile.TemporaryDirectory() as tmp,
    ):
        old_env = os.environ.copy()
        os.environ.update({"HOME": tmp, "HERMES_PROJECT_DIR": str(Path(tmp) / "x")})
        try:
            asyncio.run(
                run_command(
                    SimpleNamespace(
                        platform=platform,
                        platform_auth="gcloud",
                        computer_id="42",
                    ),
                    {"kind": "configure_telegram", "spec": {}},
                )
            )
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    assert platform.posts == [("/hapi/v1/computers/me/hermes/telegram-setup/v1", {})]


def test_configure_telegram_runs_foreground_gateway_in_containers() -> None:
    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del timeout_seconds, env
        gateway_calls.append(args)
        command = args[-1]
        stdout = "ok\n"
        if command == "start":
            stdout = (
                "Service start is not applicable inside a Docker container.\n"
                "Or run the gateway directly: hermes gateway run\n"
            )
        elif command == "status" and len(
            [call for call in gateway_calls if call[-1] == "status"]
        ) == 1:
            stdout = "✗ Gateway is not running\n"
        elif command == "status":
            stdout = "✓ Gateway is running (PID: 123)\n"
        return {
            "args": args,
            "returncode": 0,
            "ok": True,
            "timed_out": False,
            "duration_ms": 21,
            "stdout": stdout,
            "stderr": "",
        }

    async def fake_start_foreground(hermes_bin: Path) -> dict[str, object]:
        foreground_calls.append(str(hermes_bin))
        log_path.write_text("", encoding="utf-8")
        return {
            "mode": "foreground_detached",
            "pid": 123,
            "started": True,
            "returncode": None,
            "log_path": str(log_path),
        }

    async def fake_status() -> dict[str, object]:
        return {
            "schema": "tinyhat_hermes_status_v1",
            "installed": True,
            "ok": True,
            "version": "Hermes Agent 0.1.0",
        }

    platform = FakePlatform()
    gateway_calls: list[list[str]] = []
    foreground_calls: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "gateway.log"
        old_env = os.environ.copy()
        os.environ.update({"HOME": tmp, "HERMES_PROJECT_DIR": str(Path(tmp) / "x")})
        try:
            with (
                patch(
                    "hermes_runtime.commands.configure_telegram.find_hermes_binary",
                    return_value=Path("/usr/local/bin/hermes"),
                ),
                patch(
                    "hermes_runtime.commands.configure_telegram.run_process",
                    fake_run_process,
                ),
                patch(
                    "hermes_runtime.commands.configure_telegram._start_gateway_foreground",
                    fake_start_foreground,
                ),
                patch(
                    "hermes_runtime.commands.configure_telegram.probe_hermes_status",
                    fake_status,
                ),
                patch(
                    "hermes_runtime.commands.configure_telegram._telegram_delete_webhook",
                    return_value={"ok": True},
                ),
                patch(
                    "hermes_runtime.commands.configure_telegram._telegram_set_chat_menu_button",
                    return_value={"ok": True},
                ),
            ):
                result = asyncio.run(
                    run_command(
                        SimpleNamespace(
                            platform=platform,
                            platform_auth="local_dev",
                            computer_id="local-dev",
                        ),
                        {"kind": "configure_telegram", "spec": {}},
                    )
                )
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    assert foreground_calls == ["/usr/local/bin/hermes"]
    assert gateway_calls == [
        ["/usr/local/bin/hermes", "config", "set", "model.provider", "auto"],
        [
            "/usr/local/bin/hermes",
            "config",
            "set",
            "model.default",
            "deepseek/deepseek-v4-pro",
        ],
        [
            "/usr/local/bin/hermes",
            "config",
            "set",
            "model.base_url",
            "https://openrouter.ai/api/v1",
        ],
        ["/usr/local/bin/hermes", "config", "set", "stt.enabled", "true"],
        ["/usr/local/bin/hermes", "config", "set", "stt.provider", "local"],
        ["/usr/local/bin/hermes", "config", "set", "stt.local.model", "base"],
        [
            "/usr/local/bin/hermes",
            "config",
            "set",
            "auxiliary.vision.provider",
            "auto",
        ],
        ["/usr/local/bin/hermes", "gateway", "stop"],
        ["/usr/local/bin/hermes", "gateway", "start"],
        ["/usr/local/bin/hermes", "gateway", "status"],
        ["/usr/local/bin/hermes", "gateway", "status"],
    ]
    assert result["gateway"]["healthy"] is True
    assert result["gateway"]["mode"] == "foreground_detached"


def test_run_gateway_rejects_foreground_adapter_failure() -> None:
    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del timeout_seconds, env
        command = args[-1]
        stdout = "ok\n"
        if command == "start":
            stdout = "Service start is not applicable inside a Docker container.\n"
        elif command == "status":
            stdout = "✓ Gateway is running (PID: 123)\n"
        return {
            "args": args,
            "returncode": 0,
            "ok": True,
            "timed_out": False,
            "duration_ms": 1,
            "stdout": stdout,
            "stderr": "",
        }

    async def fake_start_foreground(_hermes_bin: Path) -> dict[str, object]:
        log_path.write_text(
            "WARNING gateway.platform_registry: Platform 'Telegram' requirements not met\n"
            "WARNING gateway.run: No adapter available for telegram\n",
            encoding="utf-8",
        )
        return {
            "mode": "foreground_detached",
            "pid": 123,
            "started": True,
            "returncode": None,
            "log_path": str(log_path),
        }

    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "gateway.log"
        with (
            patch(
                "hermes_runtime.commands.configure_telegram.run_process",
                fake_run_process,
            ),
            patch(
                "hermes_runtime.commands.configure_telegram._start_gateway_foreground",
                fake_start_foreground,
            ),
        ):
            result = asyncio.run(
                configure_telegram._run_gateway(Path("/usr/local/bin/hermes"))
            )

    assert result["started"] is True
    assert result["healthy"] is False
    assert result["adapter_ready"] is False


def test_start_gateway_foreground_uses_detached_popen() -> None:
    popen_calls: list[dict[str, object]] = []

    class FakeProcess:
        pid = 1234

        def poll(self) -> None:
            return None

    def fake_popen(*args: object, **kwargs: object) -> FakeProcess:
        popen_calls.append({"args": args, "kwargs": kwargs})
        return FakeProcess()

    with tempfile.TemporaryDirectory() as tmp:
        old_env = os.environ.copy()
        os.environ.update({"TINYHAT_RUNTIME_STATE_DIR": tmp})
        try:
            with patch(
                "hermes_runtime.commands.configure_telegram.subprocess.Popen",
                fake_popen,
            ):
                result = asyncio.run(
                    configure_telegram._start_gateway_foreground(
                        Path("/usr/local/bin/hermes")
                    )
                )
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    assert result["started"] is True
    assert result["pid"] == 1234
    assert popen_calls
    call = popen_calls[0]
    assert call["args"][0][:3] == [
        "/usr/local/bin/hermes",
        "gateway",
        "run",
    ]
    assert call["kwargs"]["start_new_session"] is True


def test_install_codex_auth_quick_commands_preserves_existing_config() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / "config.yaml"
        config.write_text(
            "model:\n"
            "  provider: auto\n"
            "quick_commands:\n"
            "  existing:\n"
            "    type: exec\n"
            "    command: 'echo existing'\n",
            encoding="utf-8",
        )

        result = configure_telegram._install_codex_auth_quick_commands(config)
        text = config.read_text(encoding="utf-8")

    assert result["installed"] is True
    assert "model:\n  provider: auto" in text
    assert "existing:" in text
    assert "tinyhat_settings:" in text
    assert "codex_auth:" in text
    assert "codex-auth:" in text
    assert "codex_limits:" in text
    assert "python3 -m hermes_runtime.telegram_tinyhat_settings" in text
    assert "python3 -m hermes_runtime.telegram_codex_auth start" in text
    assert "python3 -m hermes_runtime.codex_limits telegram" in text


def test_install_codex_auth_plugin_commands_enables_menu_plugin() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / ".hermes" / "config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(
            "model:\n"
            "  provider: auto\n"
            "plugins:\n"
            "  enabled:\n"
            "    - existing-plugin\n"
            "  disabled:\n"
            "    - tinyhat-codex\n",
            encoding="utf-8",
        )

        result = configure_telegram._install_codex_auth_plugin_commands(config)
        text = config.read_text(encoding="utf-8")
        plugin_dir = config.parent / "plugins" / "tinyhat-codex"
        plugin_source = (plugin_dir / "__init__.py").read_text(encoding="utf-8")

    assert result["installed"] is True
    assert result["enabled"] is True
    assert result["mechanism"] == "hermes_plugin_register_command_and_transcription_provider"
    assert result["transcription_providers"] == ["openai-codex-stt"]
    assert "existing-plugin" in text
    assert "    - tinyhat-codex\n" in text
    assert "disabled:" in text
    assert "  disabled:\n    - tinyhat-codex" not in text
    assert "ctx.register_command" in plugin_source
    assert "ctx.register_transcription_provider" in plugin_source
    assert "tinyhat_settings" in plugin_source
    assert "hermes_runtime.telegram_tinyhat_settings" in plugin_source
    assert "codex_auth" in plugin_source
    assert "hermes_runtime.telegram_codex_auth" in plugin_source
    assert "hermes_runtime.codex_limits" in plugin_source


def test_codex_auth_plugin_source_compiles_and_maps_stt_errors() -> None:
    source = configure_telegram._codex_auth_plugin_source()
    ast.parse(source)

    agent_module = ModuleType("agent")
    provider_module = ModuleType("agent.transcription_provider")

    class FakeTranscriptionProvider:
        pass

    provider_module.TranscriptionProvider = FakeTranscriptionProvider
    old_agent_module = sys.modules.get("agent")
    old_provider_module = sys.modules.get("agent.transcription_provider")
    sys.modules["agent"] = agent_module
    sys.modules["agent.transcription_provider"] = provider_module
    namespace: dict[str, object] = {"__name__": "tinyhat_codex_plugin_test"}
    try:
        exec(compile(source, "tinyhat-codex/__init__.py", "exec"), namespace)
    finally:
        if old_agent_module is None:
            sys.modules.pop("agent", None)
        else:
            sys.modules["agent"] = old_agent_module
        if old_provider_module is None:
            sys.modules.pop("agent.transcription_provider", None)
        else:
            sys.modules["agent.transcription_provider"] = old_provider_module

    response = SimpleNamespace(
        status_code=429,
        text="",
        json=lambda: {
            "error": {
                "code": "billing_not_active",
                "message": "Billing is not active.",
            }
        },
    )
    error_text = namespace["_error_from_response"](response)  # type: ignore[index]
    provider = namespace["OpenAICodexTranscriptionProvider"]()  # type: ignore[index]

    assert error_text == (
        "OpenAI Codex STT failed (429): "
        "billing_not_active: Billing is not active."
    )
    assert provider.name == "openai-codex-stt"
    assert provider.default_model() == "gpt-4o-transcribe"


def test_configure_codex_multimedia_does_not_auto_select_codex_stt() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "calls.jsonl"
        hermes_bin = Path(tmp) / "hermes"
        hermes_bin.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            f"log = pathlib.Path({str(log)!r})\n"
            "log.open('a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "print('ok')\n",
            encoding="utf-8",
        )
        hermes_bin.chmod(0o755)

        result = configure_telegram.configure_codex_multimedia(hermes_bin)
        calls = [
            json.loads(line)
            for line in log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    keys = [call[2] for call in calls]

    assert result["ok"] is True
    assert result["active_provider"] == "unchanged"
    assert result["codex_stt_provider"] == "openai-codex-stt"
    assert result["auto_selected_codex_stt"] is False
    assert "stt.provider" not in keys
    assert keys == [
        "stt.enabled",
        "stt.openai-codex-stt.model",
        "auxiliary.vision.provider",
    ]


def test_install_codex_auth_plugin_commands_normalizes_flow_plugin_lists() -> None:
    cases = [
        ("plugins:\n  enabled: []\n", []),
        (
            "plugins:\n"
            "  enabled: [existing-plugin]\n"
            "  disabled: [tinyhat-codex, other-plugin]\n",
            ["existing-plugin"],
        ),
    ]

    for config_text, existing_enabled in cases:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / ".hermes" / "config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text(config_text, encoding="utf-8")

            result = configure_telegram._install_codex_auth_plugin_commands(config)
            text = config.read_text(encoding="utf-8")

        enabled = _yaml_block_list(text, "enabled")
        disabled = _yaml_block_list(text, "disabled")
        assert result["installed"] is True
        assert "enabled: []" not in text
        assert "enabled: [existing-plugin]" not in text
        assert enabled.count("tinyhat-codex") == 1
        for plugin in existing_enabled:
            assert plugin in enabled
        assert "tinyhat-codex" not in disabled


def test_install_telegram_command_menu_priority_uses_hermes_config_shape() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / "config.yaml"
        config.write_text(
            "model:\n"
            "  provider: auto\n"
            "platforms:\n"
            "  telegram:\n"
            "    extra:\n"
            "      command_menu:\n"
            "        max_commands: 75\n"
            "        priority_mode: append\n"
            "        priority:\n"
            "          - model\n"
            "          - codex_auth\n",
            encoding="utf-8",
        )

        result = configure_telegram._install_telegram_command_menu_priority(config)
        text = config.read_text(encoding="utf-8")
        retry_result = configure_telegram._install_telegram_command_menu_priority(config)
        retry_text = config.read_text(encoding="utf-8")

    assert result["mechanism"] == "hermes_config"
    assert result["max_commands"] == 75
    assert retry_result["max_commands"] == 75
    assert retry_text == text
    assert "model:\n  provider: auto" in text
    assert "platforms:\n  telegram:\n    extra:\n      command_menu:" in text
    assert "max_commands: 75" in text
    assert "priority_mode: prepend" in text
    assert text.count("priority:") == 1
    assert text.index("- tinyhat_settings") < text.index("- codex_auth")
    assert text.index("- codex_auth") < text.index("- model")
    assert "          - codex_auth_status\n" in text
    assert "          - codex_auth_log\n" in text
    assert "          - codex_limits\n" in text
    assert text.count("- codex_auth\n") == 1


def test_install_telegram_command_menu_priority_keeps_lower_existing_cap() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / "config.yaml"
        config.write_text(
            "platforms:\n"
            "  telegram:\n"
            "    extra:\n"
            "      command_menu:\n"
            "        max_commands: 12\n"
            "        priority:\n"
            "          - model\n",
            encoding="utf-8",
        )

        result = configure_telegram._install_telegram_command_menu_priority(config)
        text = config.read_text(encoding="utf-8")

    assert result["max_commands"] == 12
    assert "max_commands: 12" in text


def test_install_telegram_command_menu_priority_adds_missing_path() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / "config.yaml"
        config.write_text(
            "quick_commands:\n"
            "  existing:\n"
            "    type: exec\n"
            "    command: 'echo existing'\n",
            encoding="utf-8",
        )

        configure_telegram._install_telegram_command_menu_priority(config)
        text = config.read_text(encoding="utf-8")

    assert "quick_commands:" in text
    assert "platforms:" in text
    assert "  telegram:" in text
    assert "    extra:" in text
    assert "      command_menu:" in text
    assert "        priority_mode: prepend" in text
