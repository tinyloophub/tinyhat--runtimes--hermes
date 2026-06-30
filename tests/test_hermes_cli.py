"""Focused tests for Hermes CLI helper process handling."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hermes_runtime import hermes_cli  # noqa: E402


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


def test_debian_prerequisites_include_media_vision_and_build_tools() -> None:
    calls: list[str] = []

    def fake_which(name: str) -> str | None:
        if name == "apt-get":
            return "/usr/bin/apt-get"
        return None

    async def fake_run_shell(
        script: str,
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del timeout_seconds, env
        calls.append(script)
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}

    with (
        patch("hermes_runtime.hermes_cli.shutil.which", fake_which),
        patch.object(hermes_cli.os, "name", "posix"),
        patch("hermes_runtime.hermes_cli.os.geteuid", return_value=0),
        patch("hermes_runtime.hermes_cli.run_shell", fake_run_shell),
    ):
        result = asyncio.run(hermes_cli.maybe_install_debian_prerequisites())

    assert result["attempted"] is True
    assert {"ffmpeg", "rg", "g++", "xclip", "wl-paste"}.issubset(
        set(result["missing_before"])
    )
    assert len(calls) == 1
    install_script = calls[0]
    assert "build-essential" in install_script
    assert "ffmpeg" in install_script
    assert "ripgrep" in install_script
    assert "xclip" in install_script
    assert "wl-clipboard" in install_script
