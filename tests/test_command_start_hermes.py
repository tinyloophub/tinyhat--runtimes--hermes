"""Focused tests for the ``start_hermes`` runtime command."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hermes_runtime.commands import run_command  # noqa: E402


def test_start_hermes_reports_missing_cli_without_failing() -> None:
    with patch(
        "hermes_runtime.commands.start_hermes.find_hermes_binary",
        return_value=None,
    ):
        result = asyncio.run(run_command(SimpleNamespace(), {"kind": "start_hermes"}))

    assert result["schema"] == "tinyhat_hermes_start_v1"
    assert result["hermes_installed"] is False
    assert result["started"] is False
    assert result["healthy"] is False
    assert result["gateway"] is None


def test_start_hermes_noops_when_gateway_is_already_healthy() -> None:
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
            "stdout": "gateway running\n",
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

    with (
        patch(
            "hermes_runtime.commands.start_hermes.find_hermes_binary",
            return_value=Path("/usr/local/bin/hermes"),
        ),
        patch("hermes_runtime.commands.start_hermes.run_process", fake_run_process),
        patch("hermes_runtime.commands.start_hermes.probe_hermes_status", fake_status),
    ):
        result = asyncio.run(run_command(SimpleNamespace(), {"kind": "start_hermes"}))

    assert calls == [["/usr/local/bin/hermes", "gateway", "status"]]
    assert result["started"] is True
    assert result["healthy"] is True
    assert result["already_running"] is True
    assert result["gateway"]["start"] is None


def test_start_hermes_runs_gateway_start_when_not_healthy() -> None:
    calls: list[list[str]] = []

    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del timeout_seconds, env
        calls.append(args)
        stdout = "gateway is not running\n" if len(calls) == 1 else "gateway running\n"
        return {
            "args": args,
            "returncode": 0,
            "ok": True,
            "timed_out": False,
            "duration_ms": 12,
            "stdout": stdout,
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

    with (
        patch(
            "hermes_runtime.commands.start_hermes.find_hermes_binary",
            return_value=Path("/usr/local/bin/hermes"),
        ),
        patch("hermes_runtime.commands.start_hermes.run_process", fake_run_process),
        patch("hermes_runtime.commands.start_hermes.probe_hermes_status", fake_status),
    ):
        result = asyncio.run(run_command(SimpleNamespace(), {"kind": "start_hermes"}))

    assert calls == [
        ["/usr/local/bin/hermes", "gateway", "status"],
        ["/usr/local/bin/hermes", "gateway", "start"],
        ["/usr/local/bin/hermes", "gateway", "status"],
    ]
    assert result["started"] is True
    assert result["healthy"] is True
    assert result["already_running"] is False
    assert result["gateway"]["mode"] == "service"
