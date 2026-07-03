"""Focused tests for activating imported OpenAI Codex auth models."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

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


def test_activate_codex_auth_models_uses_imported_hermes_auth() -> None:
    multimedia = Mock(
        return_value={
            "ok": True,
            "vision_provider": "openai-codex",
            "vision_model": "gpt-5.5",
            "active_provider": "openrouter",
        }
    )
    config_switch = Mock(
        return_value={
            "ok": True,
            "model_provider": "openai-codex",
            "model_default": "gpt-5.5",
            "output": "Default model set to: gpt-5.5",
        }
    )

    with (
        patch(
            "hermes_runtime.commands.activate_codex_auth_models.find_hermes_binary",
            return_value=Path("/usr/local/bin/hermes"),
        ),
        patch(
            "hermes_runtime.commands.activate_codex_auth_models._auth_status",
            return_value={"ok": True, "provider": "openai-codex"},
        ),
        patch(
            "hermes_runtime.commands.activate_codex_auth_models.find_codex_binary",
            return_value=None,
        ),
        patch(
            "hermes_runtime.commands.activate_codex_auth_models._run_config_switch",
            config_switch,
        ),
        patch(
            "hermes_runtime.commands.activate_codex_auth_models._configure_multimedia_after_auth",
            multimedia,
        ),
        patch(
            "hermes_runtime.commands.activate_codex_auth_models._restart_gateway_after_auth",
            Mock(return_value={"healthy": True}),
        ) as gateway_restart,
    ):
        result = asyncio.run(
            run_command(
                SimpleNamespace(),
                {"kind": "activate_codex_auth_models", "spec": {}},
            )
        )

    assert result["schema"] == "tinyhat_hermes_activate_codex_auth_models_v1"
    assert result["activated"] is True
    assert result["status"] == "applied"
    assert result["model_provider"] == "openai-codex"
    assert result["model_default"] == "gpt-5.5"
    config_switch.assert_called_once_with(Path("/usr/local/bin/hermes"))
    multimedia.assert_called_once_with(
        Path("/usr/local/bin/hermes"),
        codex_chat_model="gpt-5.5",
    )
    gateway_restart.assert_not_called()


def test_activate_codex_auth_models_skips_without_existing_auth() -> None:
    config_switch = Mock(return_value={"ok": True})

    with (
        patch(
            "hermes_runtime.commands.activate_codex_auth_models.find_hermes_binary",
            return_value=Path("/usr/local/bin/hermes"),
        ),
        patch(
            "hermes_runtime.commands.activate_codex_auth_models._auth_status",
            return_value={"ok": False, "provider": "codex-oauth"},
        ),
        patch(
            "hermes_runtime.commands.activate_codex_auth_models.find_codex_binary",
            return_value=None,
        ),
        patch(
            "hermes_runtime.commands.activate_codex_auth_models._run_config_switch",
            config_switch,
        ),
    ):
        result = asyncio.run(
            run_command(
                SimpleNamespace(),
                {"kind": "activate_codex_auth_models", "spec": {}},
            )
        )

    assert result["activated"] is False
    assert result["status"] == "skipped"
    assert result["reason"] == "codex_auth_not_found"
    config_switch.assert_not_called()


def test_activate_codex_auth_models_can_restart_gateway_when_requested() -> None:
    gateway_restart = Mock(return_value={"healthy": True, "started": True})

    with (
        patch(
            "hermes_runtime.commands.activate_codex_auth_models.find_hermes_binary",
            return_value=Path("/usr/local/bin/hermes"),
        ),
        patch(
            "hermes_runtime.commands.activate_codex_auth_models._auth_status",
            return_value={"ok": True, "provider": "openai-codex"},
        ),
        patch(
            "hermes_runtime.commands.activate_codex_auth_models.find_codex_binary",
            return_value=Path("/usr/local/bin/codex"),
        ),
        patch(
            "hermes_runtime.commands.activate_codex_auth_models._codex_cli_status",
            return_value={"ok": True},
        ),
        patch(
            "hermes_runtime.commands.activate_codex_auth_models._run_config_switch",
            return_value={
                "ok": True,
                "model_provider": "openai-codex",
                "model_default": "gpt-5.5",
            },
        ),
        patch(
            "hermes_runtime.commands.activate_codex_auth_models._configure_multimedia_after_auth",
            return_value={"ok": True},
        ),
        patch(
            "hermes_runtime.commands.activate_codex_auth_models._restart_gateway_after_auth",
            gateway_restart,
        ),
    ):
        result = asyncio.run(
            run_command(
                SimpleNamespace(),
                {
                    "kind": "activate_codex_auth_models",
                    "spec": {"restart_gateway": True},
                },
            )
        )

    assert result["activated"] is True
    assert result["gateway_restart_requested"] is True
    assert result["gateway_restart"] == {"healthy": True, "started": True}
    gateway_restart.assert_called_once_with(Path("/usr/local/bin/hermes"))
