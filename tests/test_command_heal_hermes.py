"""Focused tests for the ``heal_hermes`` runtime command."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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


def test_heal_hermes_starts_gateway_when_telegram_is_configured() -> None:
    async def fake_start(_ctx: object, command: dict[str, object]) -> dict[str, object]:
        start_calls.append(command)
        return {
            "schema": "tinyhat_hermes_start_v1",
            "started": True,
            "healthy": True,
            "gateway": {"healthy": True, "mode": "service"},
            "hermes": {"ok": True, "version": "Hermes Agent 0.1.0"},
            "env_reload": {"loaded": True, "keys": ["TELEGRAM_BOT_TOKEN"]},
        }

    start_calls: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "hermes-home"
        home.mkdir()
        (home / ".env").write_text(
            'TELEGRAM_BOT_TOKEN="123456:token"\n'
            'TELEGRAM_ALLOWED_USERS="101"\n'
            'TELEGRAM_HOME_CHANNEL="101"\n',
            encoding="utf-8",
        )
        with (
            patch.dict(
                os.environ,
                {
                    "HOME": str(Path(tmp) / "home"),
                    "TINYHAT_HERMES_HOME": str(home),
                    "HERMES_PROJECT_DIR": str(home),
                },
                clear=True,
            ),
            patch(
                "hermes_runtime.commands.heal_hermes.find_hermes_binary",
                return_value=Path("/usr/local/bin/hermes"),
            ),
            patch("hermes_runtime.commands.heal_hermes.start_hermes.run", fake_start),
        ):
            result = asyncio.run(
                run_command(
                    SimpleNamespace(),
                    {"kind": "heal_hermes", "spec": {"reason": "admin_heal"}},
                )
            )

    assert result["schema"] == "tinyhat_hermes_heal_v1"
    assert result["healthy"] is True
    assert result["healed"] is True
    assert result["telegram"]["configured"] is True
    assert result["gateway"] == {"healthy": True, "mode": "service"}
    assert result["reason"] == "gateway_healthy"
    assert start_calls[0]["spec"] == {"reason": "admin_heal"}
    assert "123456:token" not in str(result)


def test_heal_hermes_reports_missing_telegram_config() -> None:
    async def fake_status() -> dict[str, object]:
        return {
            "schema": "tinyhat_hermes_status_v1",
            "installed": True,
            "ok": True,
            "version": "Hermes Agent 0.1.0",
        }

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "hermes-home"
        home.mkdir()
        with (
            patch.dict(
                os.environ,
                {
                    "HOME": str(Path(tmp) / "home"),
                    "TINYHAT_HERMES_HOME": str(home),
                    "HERMES_PROJECT_DIR": str(home),
                },
                clear=True,
            ),
            patch(
                "hermes_runtime.commands.heal_hermes.find_hermes_binary",
                return_value=Path("/usr/local/bin/hermes"),
            ),
            patch("hermes_runtime.commands.heal_hermes.probe_hermes_status", fake_status),
        ):
            result = asyncio.run(run_command(SimpleNamespace(), {"kind": "heal_hermes"}))

    assert result["schema"] == "tinyhat_hermes_heal_v1"
    assert result["healthy"] is False
    assert result["healed"] is False
    assert result["reason"] == "telegram_not_configured"
    assert result["gateway"] is None
