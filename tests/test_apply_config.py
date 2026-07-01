"""Focused tests for Hermes runtime apply_config handling."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hermes_runtime.commands import apply_config  # noqa: E402
from hermes_runtime.runtime_env import load_env_files_into_process  # noqa: E402


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
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.gets: list[str] = []

    async def get_json(self, path: str) -> dict[str, Any]:
        self.gets.append(path)
        return self.payload


def test_load_env_files_into_process_loads_selected_keys_only() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env_file = Path(tmp) / ".env"
        env_file.write_text(
            '\n'.join(
                [
                    'EXA_API_KEY="exa-secret"',
                    "export SECOND='two'",
                    "IGNORED=value",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        old_env = os.environ.copy()
        try:
            result = load_env_files_into_process(
                [env_file],
                keys=["EXA_API_KEY", "SECOND"],
            )
            assert os.environ["EXA_API_KEY"] == "exa-secret"
            assert os.environ["SECOND"] == "two"
            assert os.environ.get("IGNORED") is None
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    assert result["keys"] == ["EXA_API_KEY", "SECOND"]
    assert result["count"] == 2
    assert str(env_file) in result["files"]


def test_apply_config_writes_reloads_notifies_and_restarts_gateway() -> None:
    events: list[tuple[str, str]] = []
    platform = FakePlatform(
        {
            "revision": 8,
            "secrets": {
                "EXA_API_KEY": "exa-secret",
                "SECOND_SECRET": "two",
            },
        }
    )

    def fake_telegram_send(text: str, **_kwargs: Any) -> dict[str, Any]:
        events.append(("notice", text))
        return {"ok": True, "http_status": 200, "description": "sent"}

    async def fake_run_gateway(_hermes_bin: Path) -> dict[str, Any]:
        events.append(("gateway", os.environ.get("EXA_API_KEY") or ""))
        return {"healthy": True, "started": True}

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        project_dir = Path(tmp) / "project"
        project_dir.mkdir(parents=True)
        home_env = home / ".hermes" / ".env"
        home_env.parent.mkdir(parents=True)
        home_env.write_text(
            "\n".join(
                [
                    'TELEGRAM_HOME_CHANNEL="123"',
                    "",
                    "# tinyhat runtime secrets start",
                    'EXA_API_KEY="old-secret"',
                    'SECOND_SECRET="old-two"',
                    "# tinyhat runtime secrets end",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        old_env = os.environ.copy()
        os.environ.clear()
        os.environ.update(
            {
                "HOME": str(home),
                "HERMES_PROJECT_DIR": str(project_dir),
            }
        )
        try:
            with (
                patch(
                    "hermes_runtime.commands.apply_config.find_hermes_binary",
                    return_value=Path("/usr/local/bin/hermes"),
                ),
                patch(
                    "hermes_runtime.commands.apply_config._run_gateway",
                    side_effect=fake_run_gateway,
                ),
                patch(
                    "hermes_runtime.commands.apply_config._telegram_send",
                    side_effect=fake_telegram_send,
                ),
            ):
                result = asyncio.run(
                    apply_config.run(
                        SimpleNamespace(
                            platform=platform,
                            computer_id="123",
                            platform_auth="gcloud",
                        ),
                        {
                            "kind": "apply_config",
                            "spec": {
                                "desired_config_revision": 7,
                                "reason": "runtime_secrets_changed",
                            },
                        },
                    )
                )
            env_text = home_env.read_text(encoding="utf-8")
            project_env_text = (project_dir / ".env").read_text(encoding="utf-8")
            assert os.environ["EXA_API_KEY"] == "exa-secret"
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    assert platform.gets == ["/hapi/v1/computers/me/runtime-secrets"]
    assert 'EXA_API_KEY="exa-secret"' in env_text
    assert 'SECOND_SECRET="two"' in env_text
    assert 'TELEGRAM_HOME_CHANNEL="123"' in env_text
    assert "# tinyhat runtime secrets start" in env_text
    assert "# tinyhat runtime secrets end" in env_text
    assert 'EXA_API_KEY="exa-secret"' in project_env_text
    assert len(events) == 2
    assert events[0][0] == "notice"
    assert events[0][1].startswith("2 secrets are saved.")
    assert "restarting my Telegram gateway now" in events[0][1]
    assert "available to Hermes" in events[0][1]
    assert "before your next message" in events[0][1]
    assert "confirm once" not in events[0][1]
    assert events[1] == ("gateway", "exa-secret")
    assert result["schema"] == "tinyhat_hermes_apply_config_v1"
    assert result["revision"] == 8
    assert result["desired_config_revision"] == 7
    assert result["secret_names"] == ["EXA_API_KEY", "SECOND_SECRET"]
    assert result["removed_secret_names"] == []
    assert result["env_reload"]["keys"] == ["EXA_API_KEY", "SECOND_SECRET"]
    assert result["terminal_env_hook"]["installed"] is True
    assert result["secret_available_notice"]["ok"] is True
    assert result["secret_available_notice"]["sent"] is True
    assert result["secret_available_notice"]["http_status"] == 200
    assert result["secret_available_notice"]["description"] == "sent"
    assert result["gateway_restart_notice"]["ok"] is None
    assert result["gateway_restart_notice"]["sent"] is False
    assert result["gateway_restart_notice"]["http_status"] is None
    assert result["gateway_restart_notice"]["description"] is None
    assert result["gateway"]["healthy"] is True
    assert result["restart_requested"] is True
    serialized = json.dumps(result, sort_keys=True)
    assert "exa-secret" not in serialized
    assert "two" not in serialized


def test_apply_config_restarts_gateway_only_when_secret_was_removed() -> None:
    events: list[tuple[str, str]] = []
    platform = FakePlatform(
        {
            "revision": 9,
            "secrets": {
                "EXA_API_KEY": "rotated-secret",
            },
        }
    )

    async def fake_run_gateway(_hermes_bin: Path) -> dict[str, Any]:
        events.append(("gateway", os.environ.get("OLD_SECRET") or ""))
        return {"healthy": True, "started": True}

    def fake_telegram_send(text: str, **_kwargs: Any) -> dict[str, Any]:
        events.append(("notice", text))
        return {"ok": True, "http_status": 200, "description": "sent"}

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        project_dir = Path(tmp) / "project"
        project_dir.mkdir(parents=True)
        home_env = home / ".hermes" / ".env"
        home_env.parent.mkdir(parents=True)
        home_env.write_text(
            "\n".join(
                [
                    "# tinyhat runtime secrets start",
                    'EXA_API_KEY="old-secret"',
                    'OLD_SECRET="stale"',
                    "# tinyhat runtime secrets end",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        old_env = os.environ.copy()
        os.environ.clear()
        os.environ.update(
            {
                "HOME": str(home),
                "HERMES_PROJECT_DIR": str(project_dir),
                "OLD_SECRET": "stale",
            }
        )
        try:
            with (
                patch(
                    "hermes_runtime.commands.apply_config.find_hermes_binary",
                    return_value=Path("/usr/local/bin/hermes"),
                ),
                patch(
                    "hermes_runtime.commands.apply_config._run_gateway",
                    side_effect=fake_run_gateway,
                ),
                patch(
                    "hermes_runtime.commands.apply_config._telegram_send",
                    side_effect=fake_telegram_send,
                ),
            ):
                result = asyncio.run(
                    apply_config.run(
                        SimpleNamespace(
                            platform=platform,
                            computer_id="123",
                            platform_auth="gcloud",
                        ),
                        {
                            "kind": "apply_config",
                            "spec": {
                                "desired_config_revision": 9,
                                "reason": "runtime_secrets_changed",
                            },
                        },
                    )
                )
            env_text = home_env.read_text(encoding="utf-8")
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    assert 'EXA_API_KEY="rotated-secret"' in env_text
    assert "OLD_SECRET" not in env_text
    assert events[0][0] == "notice"
    assert "restarting my Telegram gateway" in events[0][1]
    assert "before your next message" in events[0][1]
    assert "confirm once" not in events[0][1]
    assert events[1] == ("gateway", "")
    assert result["removed_secret_names"] == ["OLD_SECRET"]
    assert result["secret_available_notice"]["ok"] is None
    assert result["secret_available_notice"]["sent"] is False
    assert result["secret_available_notice"]["http_status"] is None
    assert result["secret_available_notice"]["description"] is None
    assert result["gateway_restart_notice"]["ok"] is True
    assert result["gateway_restart_notice"]["sent"] is True
    assert result["gateway_restart_notice"]["http_status"] == 200
    assert result["gateway_restart_notice"]["description"] == "sent"
    assert result["gateway"]["healthy"] is True
    assert result["restart_requested"] is True


def test_apply_config_rejects_invalid_runtime_secret_names() -> None:
    platform = FakePlatform(
        {
            "revision": 1,
            "secrets": {
                "BAD-NAME": "secret",
            },
        }
    )

    with tempfile.TemporaryDirectory() as tmp:
        old_env = os.environ.copy()
        os.environ.clear()
        os.environ.update({"HOME": str(Path(tmp) / "home")})
        try:
            try:
                asyncio.run(
                    apply_config.run(
                        SimpleNamespace(
                            platform=platform,
                            computer_id="123",
                            platform_auth="gcloud",
                        ),
                        {
                            "kind": "apply_config",
                            "spec": {"desired_config_revision": 1},
                        },
                    )
                )
            except RuntimeError as exc:
                assert "invalid runtime secret name" in str(exc)
            else:
                raise AssertionError("invalid secret name should fail apply_config")
        finally:
            os.environ.clear()
            os.environ.update(old_env)
