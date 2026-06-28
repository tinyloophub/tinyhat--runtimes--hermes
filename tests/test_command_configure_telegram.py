"""Focused tests for the ``configure_telegram`` runtime command."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hermes_runtime.commands.configure_telegram as configure_telegram  # noqa: E402
from hermes_runtime.commands import run_command  # noqa: E402


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
            "expires_at": "2026-06-26T10:00:00Z",
            "openrouter_api_key": "sk-or-v1-test-runtime-key",
            "openrouter_base_url": "https://openrouter.ai/api/v1",
            "openrouter_default_model": "deepseek/deepseek-v4-pro",
            "openrouter_model_package": {
                "default_model": "deepseek/deepseek-v4-pro"
            },
        }


def test_configure_telegram_writes_env_and_starts_gateway() -> None:
    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del timeout_seconds, env
        gateway_calls.append(args)
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
                    "hermes_runtime.commands.configure_telegram._telegram_api_json",
                    side_effect=[
                        {
                            "ok": True,
                            "result": [
                                {
                                    "command": "model",
                                    "description": "Change model",
                                }
                            ],
                        },
                        {"ok": True, "result": True},
                    ],
                ) as telegram_api,
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
        assert "OPENROUTER_API_KEY=\"sk-or-v1-test-runtime-key\"" in project_env_text
        config_text = (home / ".hermes" / "config.yaml").read_text(
            encoding="utf-8"
        )
        assert "quick_commands:" in config_text
        assert "codex_auth:" in config_text
        assert "codex-auth:" in config_text
        assert "codex_auth_status:" in config_text
        assert "codex_auth_log:" in config_text

    assert platform.posts == [
        ("/hapi/v1/computers/local-dev/hermes/telegram-setup/v1", {})
    ]
    assert telegram_api.call_args_list[0].args == ("123456:secret-token", "getMyCommands")
    assert telegram_api.call_args_list[1].args[0:2] == (
        "123456:secret-token",
        "setMyCommands",
    )
    registered_commands = {
        item["command"]
        for item in telegram_api.call_args_list[1].args[2]["commands"]
    }
    assert {
        "codex_auth",
        "codex_auth_status",
        "codex_auth_log",
        "model",
    }.issubset(registered_commands)
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
    assert result["codex_auth"]["quick_commands"]["commands"] == [
        "codex_auth",
        "codex-auth",
        "codex_auth_status",
        "codex_auth_log",
    ]
    assert result["codex_auth"]["quick_commands"]["telegram_menu_commands"] == [
        "codex_auth",
        "codex_auth_status",
        "codex_auth_log",
    ]
    assert result["codex_auth"]["telegram_commands"]["ok"] is True
    assert result["gateway"]["healthy"] is True
    assert result["gateway"]["mode"] == "service"


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
            "hermes_runtime.commands.configure_telegram._telegram_api_json",
            side_effect=[{"ok": True, "result": []}, {"ok": True, "result": True}],
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
                    "hermes_runtime.commands.configure_telegram._telegram_api_json",
                    side_effect=[
                        {"ok": True, "result": []},
                        {"ok": True, "result": True},
                    ],
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
    assert "codex_auth:" in text
    assert "codex-auth:" in text
    assert "python3 -m hermes_runtime.telegram_codex_auth start" in text
