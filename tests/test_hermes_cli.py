"""Focused tests for Hermes CLI helper process handling."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hermes_runtime import hermes_cli  # noqa: E402


def test_run_process_waits_for_child_after_timeout_kill() -> None:
    class FakeProcess:
        returncode: int | None = None
        killed = False
        waited = False

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.sleep(60)
            return b"", b""

        def kill(self) -> None:
            self.killed = True

        async def wait(self) -> int:
            self.waited = True
            self.returncode = -9
            return self.returncode

    process = FakeProcess()

    async def fake_create_subprocess_exec(*_args: object, **_kwargs: object) -> FakeProcess:
        return process

    with patch(
        "hermes_runtime.hermes_cli.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    ):
        result = asyncio.run(
            hermes_cli.run_process(["slow-command"], timeout_seconds=0.001)
        )

    assert process.killed is True
    assert process.waited is True
    assert result["ok"] is False
    assert result["timed_out"] is True
    assert result["stderr"] == "command timed out after 0.001s"
