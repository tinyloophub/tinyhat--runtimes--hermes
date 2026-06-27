"""Focused tests for the ``stop_hermes`` runtime command."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hermes_runtime.commands.stop_hermes as stop_hermes  # noqa: E402
from hermes_runtime.commands import run_command  # noqa: E402


def test_stop_hermes_reports_missing_cli_without_failing() -> None:
    with (
        patch("hermes_runtime.commands.stop_hermes.find_hermes_binary", return_value=None),
        patch(
            "hermes_runtime.commands.stop_hermes._terminate_gateway_processes",
            return_value=[],
        ),
    ):
        result = asyncio.run(run_command(SimpleNamespace(), {"kind": "stop_hermes"}))

    assert result["schema"] == "tinyhat_hermes_stop_v1"
    assert result["hermes_installed"] is False
    assert result["stopped"] is True
    assert result["gateway_stop"] is None
    assert result["terminated_processes"] == []


def test_stop_hermes_runs_gateway_stop_and_status() -> None:
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
            "stdout": "gateway stopped\n" if args[-1] == "status" else "ok\n",
            "stderr": "",
        }

    with (
        patch(
            "hermes_runtime.commands.stop_hermes.find_hermes_binary",
            return_value=Path("/usr/local/bin/hermes"),
        ),
        patch("hermes_runtime.commands.stop_hermes.run_process", fake_run_process),
        patch(
            "hermes_runtime.commands.stop_hermes._terminate_gateway_processes",
            return_value=[],
        ),
    ):
        result = asyncio.run(run_command(SimpleNamespace(), {"kind": "stop_hermes"}))

    assert calls == [
        ["/usr/local/bin/hermes", "gateway", "status"],
        ["/usr/local/bin/hermes", "gateway", "stop"],
        ["/usr/local/bin/hermes", "gateway", "status"],
    ]
    assert result["hermes_installed"] is True
    assert result["stopped"] is True
    assert result["gateway_stop"]["ok"] is True


def test_stop_hermes_terminates_foreground_gateway_process() -> None:
    process = {
        "pid": 123,
        "cmdline": ["/usr/local/bin/hermes", "gateway", "run", "--replace"],
    }

    with patch(
        "hermes_runtime.commands.stop_hermes._list_gateway_processes",
        return_value=[process],
    ):
        with patch(
            "hermes_runtime.commands.stop_hermes._terminate_process",
            return_value={**process, "terminated": True},
        ) as terminate:
            result = stop_hermes._terminate_gateway_processes(
                Path("/usr/local/bin/hermes")
            )

    terminate.assert_called_once_with(process)
    assert result == [{**process, "terminated": True}]
