"""Focused tests for the ``codex_limits`` runtime command."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hermes_runtime.codex_limits as codex_limits  # noqa: E402
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


def sample_limits_payload() -> dict[str, object]:
    return {
        "rateLimits": {
            "limitId": "codex",
            "primary": {
                "usedPercent": 20,
                "windowDurationMins": 300,
                "resetsAt": 1_800_000_000,
            },
            "secondary": {
                "usedPercent": 50,
                "windowDurationMins": 10080,
                "resetsAt": 1_800_086_400,
            },
            "credits": {"hasCredits": True, "unlimited": False, "balance": "42.5"},
            "planType": "pro",
        },
        "rateLimitResetCredits": {"availableCount": 3},
    }


def test_summary_calculates_remaining_windows() -> None:
    summary = codex_limits.summarize_rate_limits(
        sample_limits_payload(),
        now=1_799_990_000,
    )

    codex = summary["limits"][0]
    assert codex["label"] == "Codex"
    assert codex["plan_type"] == "pro"
    assert codex["windows"][0]["text"].startswith(
        "Primary, 80% remaining, estimated quota left 4h",
    )
    assert codex["windows"][1]["text"].startswith(
        "Weekly, 50% remaining, estimated quota left 84h",
    )
    assert summary["rate_limit_reset_credits"] == {"availableCount": 3}


def test_telegram_summary_is_copyable() -> None:
    result = {
        "ok": True,
        "summary": codex_limits.summarize_rate_limits(
            sample_limits_payload(),
            now=1_799_990_000,
        ),
    }

    text = codex_limits.format_telegram_summary(result)

    assert "OpenAI Codex usage limits" in text
    assert "Codex, plan pro" in text
    assert "Credits remaining: 42.5" in text
    assert "Primary\n[████████░░] 80% remaining" in text
    assert "Estimated time left: 4h" in text
    assert "Weekly\n[█████░░░░░] 50% remaining" in text
    assert "Reset credits available: 3" in text


def test_structured_snapshot_is_written_without_terminal_logs() -> None:
    result = {
        "ok": True,
        "source": "codex app-server",
        "method": codex_limits.APP_SERVER_METHOD,
        "duration_ms": 123,
        "limits": sample_limits_payload(),
        "summary": codex_limits.summarize_rate_limits(
            sample_limits_payload(),
            now=1_799_990_000,
        ),
        "stderr_tail": "not persisted",
    }

    with tempfile.TemporaryDirectory() as tmp:
        old_env = os.environ.copy()
        os.environ.update({"TINYHAT_RUNTIME_STATE_DIR": tmp})
        try:
            path = codex_limits.persist_limits_snapshot(result)
        finally:
            os.environ.clear()
            os.environ.update(old_env)

        assert path is not None
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        assert payload["source"] == "codex app-server"
        assert payload["method"] == codex_limits.APP_SERVER_METHOD
        assert payload["limits"] == sample_limits_payload()
        assert "stderr_tail" not in payload


def test_runtime_command_returns_codex_limits() -> None:
    async def fake_read() -> dict[str, object]:
        return {
            "schema": codex_limits.SCHEMA,
            "ok": True,
            "source": "codex app-server",
            "limits": sample_limits_payload(),
        }

    with patch("hermes_runtime.commands.codex_limits.read_codex_limits", fake_read):
        result = asyncio.run(
            run_command(SimpleNamespace(), {"kind": "codex_limits", "spec": {}})
        )

    assert result["schema"] == codex_limits.SCHEMA
    assert result["ok"] is True
    assert result["source"] == "codex app-server"
