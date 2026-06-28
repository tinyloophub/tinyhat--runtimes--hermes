"""Focused tests for the ``hermes_status`` runtime command."""

from __future__ import annotations

import asyncio
import sys
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


def test_hermes_status_reports_missing_cli() -> None:
    with patch("hermes_runtime.hermes_cli.find_hermes_binary", return_value=None):
        result = asyncio.run(
            run_command(SimpleNamespace(), {"kind": "hermes_status"})
        )

    assert result["schema"] == "tinyhat_hermes_status_v1"
    assert result["installed"] is False
    assert result["ok"] is False
    assert result["commands"] == {}


def test_hermes_status_runs_official_status_commands() -> None:
    calls: list[list[str]] = []

    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del timeout_seconds, env
        calls.append(args)
        return {
            "args": args,
            "returncode": 0,
            "ok": True,
            "timed_out": False,
            "duration_ms": 12,
            "stdout": "Hermes Agent 0.1.0\n",
            "stderr": "",
        }

    with (
        patch(
            "hermes_runtime.hermes_cli.find_hermes_binary",
            return_value=Path("/usr/local/bin/hermes"),
        ),
        patch("hermes_runtime.hermes_cli.run_process", fake_run_process),
    ):
        result = asyncio.run(
            run_command(SimpleNamespace(), {"kind": "hermes_status"})
        )

    assert calls == [
        ["/usr/local/bin/hermes", "--version"],
        ["/usr/local/bin/hermes", "status"],
        ["/usr/local/bin/hermes", "status", "--all"],
    ]
    assert result["installed"] is True
    assert result["ok"] is True
    assert result["version"] == "Hermes Agent 0.1.0"
