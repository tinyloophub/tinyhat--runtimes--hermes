"""Focused tests for complete Computer-side Slack disconnect."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hermes_runtime.commands import disconnect_slack  # noqa: E402


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


def test_disconnect_slack_revokes_bundle_and_preserves_other_values() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home = root / "home"
        env_file = home / ".hermes" / ".env"
        config_file = home / ".hermes" / "config.yaml"
        env_file.parent.mkdir(parents=True)
        env_file.write_text(
            "\n".join(
                [
                    'SLACK_BOT_TOKEN="bot-token-under-test"',
                    'SLACK_APP_TOKEN="app-token-under-test"',
                    'SLACK_ALLOWED_USERS="U12345678"',
                    'SLACK_HOME_CHANNEL="D12345678"',
                    'SLACK_HOME_CHANNEL_NAME="Owner DM"',
                    'OTHER_API_KEY="other-value"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        config_file.write_text(
            "terminal:\n  env_passthrough:\n    - SLACK_BOT_TOKEN\n    - OTHER_API_KEY\n",
            encoding="utf-8",
        )
        old_env = os.environ.copy()
        os.environ.update(
            {
                "HOME": str(home),
                "TINYHAT_HERMES_HOME": str(home / ".hermes"),
                "HERMES_ENV_FILE": str(env_file),
                "HERMES_CONFIG_FILE": str(config_file),
                "HERMES_PROJECT_DIR": str(root / "missing-project"),
                "SLACK_BOT_TOKEN": "bot-token-under-test",
                "SLACK_APP_TOKEN": "app-token-under-test",
            }
        )
        gateway = AsyncMock(return_value={"healthy": True})
        try:
            with (
                patch.object(
                    disconnect_slack,
                    "_revoke_slack_bot_access",
                    return_value={"status": "revoked", "confirmed": True},
                ) as revoke,
                patch.object(disconnect_slack.heal_hermes, "run", gateway),
            ):
                result = asyncio.run(
                    disconnect_slack.run(
                        None,
                        {
                            "kind": "disconnect_slack",
                            "spec": {
                                "handoff_public_id": "sh_slack",
                                "removal_request_id": "scr_slack",
                            },
                        },
                    )
                )
        finally:
            os.environ.clear()
            os.environ.update(old_env)

        env_text = env_file.read_text(encoding="utf-8")
        serialized = str(result)
        revoke.assert_called_once_with("bot-token-under-test")
        assert not any(f"{name}=" in env_text for name in disconnect_slack.SLACK_ENV_NAMES)
        assert 'OTHER_API_KEY="other-value"' in env_text
        assert "SLACK_BOT_TOKEN" not in config_file.read_text(encoding="utf-8")
        assert "OTHER_API_KEY" in config_file.read_text(encoding="utf-8")
        assert result["local_bundle_absent"] is True
        assert result["disconnect_verified"] is True
        assert result["slack_access"]["status"] == "revoked"
        assert "bot-token-under-test" not in serialized
        assert "app-token-under-test" not in serialized
        assert "other-value" not in serialized


def test_disconnect_slack_treats_removed_app_token_as_already_revoked() -> None:
    response = unittest.mock.MagicMock()
    response.read.return_value = b'{"ok":false,"error":"invalid_auth"}'
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    with patch.object(disconnect_slack.request, "urlopen", return_value=response):
        result = disconnect_slack._revoke_slack_bot_access("not-returned")
    assert result == {
        "status": "already_revoked",
        "confirmed": True,
        "error_code": "invalid_auth",
    }
    assert "not-returned" not in str(result)


def test_disconnect_slack_requires_gateway_proof() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home = root / "home"
        env_file = home / ".hermes" / ".env"
        config_file = home / ".hermes" / "config.yaml"
        env_file.parent.mkdir(parents=True)
        env_file.write_text("", encoding="utf-8")
        config_file.write_text("terminal:\n  env_passthrough: []\n", encoding="utf-8")
        old_env = os.environ.copy()
        os.environ.update(
            {
                "HOME": str(home),
                "TINYHAT_HERMES_HOME": str(home / ".hermes"),
                "HERMES_ENV_FILE": str(env_file),
                "HERMES_CONFIG_FILE": str(config_file),
                "HERMES_PROJECT_DIR": str(root / "missing-project"),
            }
        )
        try:
            with (
                patch.object(
                    disconnect_slack,
                    "_revoke_slack_bot_access",
                    return_value={"status": "not_present", "confirmed": False},
                ),
                patch.object(
                    disconnect_slack.heal_hermes,
                    "run",
                    AsyncMock(return_value={"healthy": False}),
                ),
            ):
                result = asyncio.run(disconnect_slack.run(None, {"spec": {}}))
        finally:
            os.environ.clear()
            os.environ.update(old_env)
    assert result["local_bundle_absent"] is True
    assert result["disconnect_verified"] is False
    assert result["gateway_ready"] is False
