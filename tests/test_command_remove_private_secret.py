"""Focused tests for value-blind Computer-side credential removal."""

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

from hermes_runtime.commands import remove_private_secret


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


def test_remove_private_secret_preserves_other_credentials_and_returns_no_values() -> (
    None
):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home = root / "home"
        env_file = home / ".hermes" / ".env"
        config_file = home / ".hermes" / "config.yaml"
        env_file.parent.mkdir(parents=True)
        env_file.write_text(
            "\n".join(
                [
                    "# tinyhat runtime secrets start",
                    'EXA_API_KEY="credential-under-test"',
                    'OTHER_API_KEY="other-credential"',
                    "# tinyhat runtime secrets end",
                    "",
                    "# tinyhat terminal secret aliases start",
                    '_HERMES_FORCE_EXA_API_KEY="credential-under-test"',
                    '_HERMES_FORCE_OTHER_API_KEY="other-credential"',
                    "# tinyhat terminal secret aliases end",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        config_file.write_text(
            "terminal:\n  env_passthrough:\n    - EXA_API_KEY\n    - OTHER_API_KEY\n",
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
                "EXA_API_KEY": "credential-under-test",
                "_HERMES_FORCE_EXA_API_KEY": "credential-under-test",
            }
        )
        gateway = AsyncMock(
            return_value={
                "schema": "tinyhat_hermes_heal_v1",
                "healthy": True,
                "reason": "gateway_restart_verified",
            }
        )
        try:
            with patch.object(remove_private_secret.heal_hermes, "run", gateway):
                result = asyncio.run(
                    remove_private_secret.run(
                        None,
                        {
                            "kind": "remove_private_secret",
                            "spec": {
                                "secret_name": "EXA_API_KEY",
                                "handoff_public_id": "sh_exa",
                                "removal_request_id": "scr_request",
                            },
                        },
                    )
                )
        finally:
            os.environ.clear()
            os.environ.update(old_env)

        env_text = env_file.read_text(encoding="utf-8")
        config_text = config_file.read_text(encoding="utf-8")
        serialized = str(result)
        assert "EXA_API_KEY=" not in env_text
        assert "_HERMES_FORCE_EXA_API_KEY=" not in env_text
        assert 'OTHER_API_KEY="other-credential"' in env_text
        assert '_HERMES_FORCE_OTHER_API_KEY="other-credential"' in env_text
        assert "EXA_API_KEY" not in config_text
        assert "OTHER_API_KEY" in config_text
        assert result["local_secret_absent"] is True
        assert result["credential_removal_verified"] is True
        assert result["removed_from_files"] is True
        assert result["gateway_ready"] is True
        assert "credential-under-test" not in serialized
        assert "other-credential" not in serialized
        gateway.assert_awaited_once_with(
            None,
            {
                "kind": "heal_hermes",
                "spec": {
                    "reason": "private_credential_removed",
                    "restart": True,
                    "deadline_seconds": 90,
                },
            },
        )


def test_remove_private_secret_rejects_invalid_name_before_local_changes() -> None:
    try:
        asyncio.run(
            remove_private_secret.run(
                None,
                {"spec": {"secret_name": "NOT-A-VALID-NAME"}},
            )
        )
    except RuntimeError as exc:
        assert "valid env-style name" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("invalid credential names must be rejected")


def test_remove_private_secret_requires_gateway_proof_even_when_files_are_clean() -> (
    None
):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home = root / "home"
        env_file = home / ".hermes" / ".env"
        config_file = home / ".hermes" / "config.yaml"
        env_file.parent.mkdir(parents=True)
        env_file.write_text("OTHER_API_KEY=still-present\n", encoding="utf-8")
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
        gateway = AsyncMock(
            return_value={
                "schema": "tinyhat_hermes_heal_v1",
                "healthy": False,
                "reason": "gateway_restart_unverified",
            }
        )
        try:
            with patch.object(remove_private_secret.heal_hermes, "run", gateway):
                result = asyncio.run(
                    remove_private_secret.run(
                        None,
                        {
                            "kind": "remove_private_secret",
                            "spec": {"secret_name": "EXA_API_KEY"},
                        },
                    )
                )
        finally:
            os.environ.clear()
            os.environ.update(old_env)

        assert result["local_secret_absent"] is True
        assert result["credential_removal_verified"] is False
        assert result["gateway_ready"] is False
        assert result["restart_requested"] is True
        gateway.assert_awaited_once()
