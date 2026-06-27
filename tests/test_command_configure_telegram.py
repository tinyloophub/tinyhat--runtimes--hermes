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
        assert "TELEGRAM_ALLOWED_USERS=\"555111\"" in project_env.read_text(
            encoding="utf-8"
        )

    assert platform.posts == [
        ("/hapi/v1/computers/local-dev/hermes/telegram-setup/v1", {})
    ]
    assert gateway_calls == [
        ["/usr/local/bin/hermes", "gateway", "stop"],
        ["/usr/local/bin/hermes", "gateway", "start"],
        ["/usr/local/bin/hermes", "gateway", "status"],
    ]
    assert result["configured"] is True
    assert result["bot_username"] == "tinyhatdevtest_4_bot"
    assert result["owner_user_id"] == "555111"
    assert "123456:secret-token" not in str(result)


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
