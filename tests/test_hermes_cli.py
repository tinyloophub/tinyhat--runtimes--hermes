"""Focused tests for Hermes CLI helper process handling."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
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


def test_run_process_kills_restart_process_group_after_timeout() -> None:
    class FakeProcess:
        pid = 4321
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
    create_kwargs: dict[str, object] = {}

    async def fake_create_subprocess_exec(
        *_args: object, **kwargs: object
    ) -> FakeProcess:
        create_kwargs.update(kwargs)
        return process

    with (
        patch(
            "hermes_runtime.hermes_cli.asyncio.create_subprocess_exec",
            fake_create_subprocess_exec,
        ),
        patch.object(hermes_cli.os, "name", "posix"),
        patch("hermes_runtime.hermes_cli.os.killpg") as killpg,
    ):
        result = asyncio.run(
            hermes_cli.run_process(
                ["hermes", "gateway", "restart"],
                timeout_seconds=0.001,
                kill_process_group=True,
            )
        )

    assert create_kwargs["start_new_session"] is True
    killpg.assert_called_once_with(4321, hermes_cli.signal.SIGKILL)
    assert process.killed is False
    assert process.waited is True
    assert result["timed_out"] is True


def test_run_process_reaps_management_process_group_when_cancelled() -> None:
    class FakeProcess:
        pid = 4321
        returncode: int | None = None
        killed = False
        waited = False

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.Future()
            return b"", b""

        def kill(self) -> None:
            self.killed = True

        async def wait(self) -> int:
            self.waited = True
            self.returncode = -9
            return self.returncode

    process = FakeProcess()
    started = asyncio.Event()

    async def fake_create_subprocess_exec(
        *_args: object, **_kwargs: object
    ) -> FakeProcess:
        started.set()
        return process

    async def scenario() -> None:
        task = asyncio.create_task(
            hermes_cli.run_process(
                ["hermes", "gateway", "start"],
                timeout_seconds=60,
                kill_process_group=True,
            )
        )
        await started.wait()
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("run_process must preserve cancellation")

    with (
        patch(
            "hermes_runtime.hermes_cli.asyncio.create_subprocess_exec",
            fake_create_subprocess_exec,
        ),
        patch.object(hermes_cli.os, "name", "posix"),
        patch("hermes_runtime.hermes_cli.os.killpg") as killpg,
    ):
        asyncio.run(scenario())

    killpg.assert_called_once_with(4321, hermes_cli.signal.SIGKILL)
    assert process.killed is False
    assert process.waited is True


def _process_is_live(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _pipe_holding_child_command(pid_path: Path) -> list[str]:
    script = (
        "import pathlib, subprocess, sys; "
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(30)']); "
        f"pathlib.Path({str(pid_path)!r}).write_text(str(child.pid)); "
        "sys.stdout.flush()"
    )
    return [sys.executable, "-c", script]


async def _wait_for_file(path: Path, timeout_seconds: float = 3.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError("child PID file was not written")
        await asyncio.sleep(0.01)


async def _wait_for_process_exit(pid: int, timeout_seconds: float = 3.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while _process_is_live(pid):
        if time.monotonic() >= deadline:
            raise AssertionError(f"orphaned process {pid} remained alive")
        await asyncio.sleep(0.02)


def test_run_process_timeout_kills_child_after_cli_leader_exits() -> None:
    if os.name != "posix":
        return
    with tempfile.TemporaryDirectory() as tmp:
        pid_path = Path(tmp) / "child.pid"
        result = asyncio.run(
            hermes_cli.run_process(
                _pipe_holding_child_command(pid_path),
                timeout_seconds=0.2,
                kill_process_group=True,
            )
        )
        child_pid = int(pid_path.read_text(encoding="utf-8"))
        asyncio.run(_wait_for_process_exit(child_pid))

    assert result["timed_out"] is True


def test_run_process_cancellation_kills_child_after_cli_leader_exits() -> None:
    if os.name != "posix":
        return

    async def scenario(pid_path: Path) -> None:
        task = asyncio.create_task(
            hermes_cli.run_process(
                _pipe_holding_child_command(pid_path),
                timeout_seconds=60,
                kill_process_group=True,
            )
        )
        await _wait_for_file(pid_path)
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("run_process must preserve cancellation")
        await _wait_for_process_exit(int(pid_path.read_text(encoding="utf-8")))

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario(Path(tmp) / "child.pid"))


def test_run_process_cancellation_after_closed_pipes_does_not_signal_group() -> None:
    class FakeProcess:
        pid = 4321
        returncode: int | None = None
        killed = False
        waited = False

        async def communicate(self) -> tuple[bytes, bytes]:
            self.returncode = 0
            return b"complete", b""

        def kill(self) -> None:
            self.killed = True

        async def wait(self) -> int:
            self.waited = True
            assert self.returncode is not None
            return self.returncode

    process = FakeProcess()

    async def fake_create_subprocess_exec(
        *_args: object, **_kwargs: object
    ) -> FakeProcess:
        return process

    async def cancel_after_communication_closed(
        task: asyncio.Task[tuple[bytes, bytes]], *, timeout_seconds: float
    ) -> tuple[bytes, bytes]:
        del timeout_seconds
        await task
        raise asyncio.CancelledError

    with (
        patch(
            "hermes_runtime.hermes_cli.asyncio.create_subprocess_exec",
            fake_create_subprocess_exec,
        ),
        patch(
            "hermes_runtime.hermes_cli._wait_for_communicate",
            cancel_after_communication_closed,
        ),
        patch.object(hermes_cli.os, "name", "posix"),
        patch("hermes_runtime.hermes_cli.os.killpg") as killpg,
    ):
        try:
            asyncio.run(
                hermes_cli.run_process(
                    ["completed-command"],
                    timeout_seconds=60,
                    kill_process_group=True,
                )
            )
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("run_process must preserve cancellation")

    killpg.assert_not_called()
    assert process.killed is False
    assert process.waited is True


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
