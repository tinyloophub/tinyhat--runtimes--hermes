"""Focused tests for the ``start_hermes`` runtime command."""

from __future__ import annotations

import asyncio
import os
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
        patch("hermes_runtime.commands.start_hermes.shutil.which", return_value=None),
        patch("hermes_runtime.commands.start_hermes.run_process", fake_run_process),
        patch("hermes_runtime.commands.start_hermes.probe_hermes_status", fake_status),
        patch(
            "hermes_runtime.commands.start_hermes.load_env_files_into_process",
            return_value={"loaded": True, "keys": []},
        ),
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
        patch("hermes_runtime.commands.start_hermes.shutil.which", return_value=None),
        patch("hermes_runtime.commands.start_hermes.run_process", fake_run_process),
        patch("hermes_runtime.commands.start_hermes.probe_hermes_status", fake_status),
        patch(
            "hermes_runtime.commands.start_hermes.load_env_files_into_process",
            return_value={"loaded": True, "keys": ["EXA_API_KEY"]},
        ),
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
    assert result["env_reload"]["keys"] == ["EXA_API_KEY"]


def _systemctl_result(args: list[str], returncode: int) -> dict[str, object]:
    return {
        "args": args,
        "returncode": returncode,
        "ok": returncode == 0,
        "timed_out": False,
        "duration_ms": 9,
        "stdout": "failed\n" if returncode == 0 else "inactive\n",
        "stderr": "",
    }


def test_start_hermes_resets_failed_service_before_starting() -> None:
    calls: list[list[str]] = []

    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del timeout_seconds, env
        calls.append(args)
        if args[0] == "/bin/systemctl":
            # The system manager owns the unit and reports it failed:
            # `is-failed` exits 0 exactly when the unit is failed.
            return _systemctl_result(args, returncode=0)
        command = args[-1]
        status_calls = len([call for call in calls if call[-1] == "status"])
        stdout = "ok\n"
        ok = True
        returncode = 0
        if command == "status" and status_calls == 1:
            ok = False
            returncode = 3
            stdout = "gateway is not running\n"
        elif command == "status":
            stdout = "gateway running\n"
        return {
            "args": args,
            "returncode": returncode,
            "ok": ok,
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
        patch("hermes_runtime.commands.start_hermes.shutil.which", return_value="/bin/systemctl"),
        patch("hermes_runtime.commands.start_hermes.run_process", fake_run_process),
        patch("hermes_runtime.commands.start_hermes.probe_hermes_status", fake_status),
        patch(
            "hermes_runtime.commands.start_hermes.load_env_files_into_process",
            return_value={"loaded": True, "keys": ["TELEGRAM_BOT_TOKEN"]},
        ),
    ):
        result = asyncio.run(run_command(SimpleNamespace(), {"kind": "start_hermes"}))

    assert calls == [
        ["/usr/local/bin/hermes", "gateway", "status"],
        ["/bin/systemctl", "is-failed", "hermes-gateway.service"],
        ["/bin/systemctl", "reset-failed", "hermes-gateway.service"],
        ["/usr/local/bin/hermes", "gateway", "start"],
        ["/usr/local/bin/hermes", "gateway", "status"],
    ]
    assert result["healthy"] is True
    assert result["gateway"]["reset_failed"]["manager"] == "system"
    assert result["gateway"]["reset_failed"]["ok"] is True
    assert result["gateway"]["reset_failed"]["bus_env_injected"] is False
    assert result["gateway"]["reset_failed"]["is_failed"]["ok"] is True


def test_start_hermes_is_failed_exit_code_triggers_reset_then_retry() -> None:
    calls: list[list[str]] = []

    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del timeout_seconds, env
        calls.append(args)
        if args[0] == "/bin/systemctl":
            if "--user" in args:
                # The user manager never reports the unit failed here.
                return _systemctl_result(args, returncode=1)
            if args[1] == "is-failed":
                system_is_failed_calls = len(
                    [
                        call
                        for call in calls
                        if call[0] == "/bin/systemctl"
                        and "--user" not in call
                        and "is-failed" in call
                    ]
                )
                # Not failed before the first start; failed after it
                # (start-limit hit), detected purely by exit code.
                return _systemctl_result(
                    args,
                    returncode=1 if system_is_failed_calls == 1 else 0,
                )
            return _systemctl_result(args, returncode=0)
        command = args[-1]
        status_calls = len([call for call in calls if call[-1] == "status"])
        stdout = "ok\n"
        ok = True
        returncode = 0
        if command == "status" and status_calls <= 2:
            ok = False
            returncode = 3
            stdout = "gateway is not running\n"
        elif command == "status":
            stdout = "gateway running\n"
        return {
            "args": args,
            "returncode": returncode,
            "ok": ok,
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
        patch("hermes_runtime.commands.start_hermes.shutil.which", return_value="/bin/systemctl"),
        patch("hermes_runtime.commands.start_hermes.run_process", fake_run_process),
        patch("hermes_runtime.commands.start_hermes.probe_hermes_status", fake_status),
        patch(
            "hermes_runtime.commands.start_hermes.load_env_files_into_process",
            return_value={"loaded": True, "keys": ["TELEGRAM_BOT_TOKEN"]},
        ),
    ):
        result = asyncio.run(run_command(SimpleNamespace(), {"kind": "start_hermes"}))

    assert calls == [
        ["/usr/local/bin/hermes", "gateway", "status"],
        ["/bin/systemctl", "is-failed", "hermes-gateway.service"],
        ["/bin/systemctl", "--user", "is-failed", "hermes-gateway.service"],
        ["/usr/local/bin/hermes", "gateway", "start"],
        ["/usr/local/bin/hermes", "gateway", "status"],
        ["/bin/systemctl", "is-failed", "hermes-gateway.service"],
        ["/bin/systemctl", "reset-failed", "hermes-gateway.service"],
        ["/usr/local/bin/hermes", "gateway", "start"],
        ["/usr/local/bin/hermes", "gateway", "status"],
    ]
    assert result["healthy"] is True
    assert result["gateway"]["reset_failed"]["manager"] == "system"
    assert result["gateway"]["reset_failed"]["ok"] is True


def test_start_hermes_reset_failed_targets_user_manager_with_bus_env() -> None:
    calls: list[tuple[list[str], dict[str, str] | None]] = []

    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del timeout_seconds
        calls.append((args, env))
        if args[0] == "/bin/systemctl":
            if "--user" in args:
                # The user manager owns the unit and reports it failed.
                return _systemctl_result(args, returncode=0)
            # The system manager does not know the unit as failed.
            return _systemctl_result(args, returncode=1)
        command = args[-1]
        status_calls = len([call for call, _env in calls if call[-1] == "status"])
        stdout = "ok\n"
        ok = True
        returncode = 0
        if command == "status" and status_calls == 1:
            ok = False
            returncode = 3
            stdout = "gateway is not running\n"
        elif command == "status":
            stdout = "gateway running\n"
        return {
            "args": args,
            "returncode": returncode,
            "ok": ok,
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

    expected_bus_env = {
        "XDG_RUNTIME_DIR": "/run/user/0",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/0/bus",
    }
    with (
        patch.dict(os.environ, {}, clear=True),
        patch(
            "hermes_runtime.commands.start_hermes.os.geteuid",
            return_value=0,
            create=True,
        ),
        patch(
            "hermes_runtime.commands.start_hermes.find_hermes_binary",
            return_value=Path("/usr/local/bin/hermes"),
        ),
        patch("hermes_runtime.commands.start_hermes.shutil.which", return_value="/bin/systemctl"),
        patch("hermes_runtime.commands.start_hermes.run_process", fake_run_process),
        patch("hermes_runtime.commands.start_hermes.probe_hermes_status", fake_status),
        patch(
            "hermes_runtime.commands.start_hermes.load_env_files_into_process",
            return_value={"loaded": True, "keys": ["TELEGRAM_BOT_TOKEN"]},
        ),
    ):
        result = asyncio.run(run_command(SimpleNamespace(), {"kind": "start_hermes"}))

    assert [args for args, _env in calls[:4]] == [
        ["/usr/local/bin/hermes", "gateway", "status"],
        ["/bin/systemctl", "is-failed", "hermes-gateway.service"],
        ["/bin/systemctl", "--user", "is-failed", "hermes-gateway.service"],
        ["/bin/systemctl", "--user", "reset-failed", "hermes-gateway.service"],
    ]
    user_leg_envs = [env for args, env in calls if "--user" in args]
    assert user_leg_envs == [expected_bus_env, expected_bus_env]
    assert result["healthy"] is True
    assert result["gateway"]["reset_failed"]["manager"] == "user"
    assert result["gateway"]["reset_failed"]["ok"] is True
    assert result["gateway"]["reset_failed"]["bus_env_injected"] is True


def test_start_hermes_user_systemd_env_only_for_root_without_runtime_dir() -> None:
    from hermes_runtime.commands.start_hermes import _user_systemd_env

    with (
        patch.dict(os.environ, {}, clear=True),
        patch(
            "hermes_runtime.commands.start_hermes.os.geteuid",
            return_value=0,
            create=True,
        ),
    ):
        assert _user_systemd_env() == {
            "XDG_RUNTIME_DIR": "/run/user/0",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/0/bus",
        }

    with (
        patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/1000"}, clear=True),
        patch(
            "hermes_runtime.commands.start_hermes.os.geteuid",
            return_value=0,
            create=True,
        ),
    ):
        assert _user_systemd_env() is None

    with (
        patch.dict(os.environ, {}, clear=True),
        patch(
            "hermes_runtime.commands.start_hermes.os.geteuid",
            return_value=1000,
            create=True,
        ),
    ):
        assert _user_systemd_env() is None


def test_start_hermes_installs_gateway_service_before_foreground_fallback() -> None:
    calls: list[list[str]] = []

    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del timeout_seconds, env
        calls.append(args)
        command = args[-1]
        stdout = "ok\n"
        ok = True
        returncode = 0
        status_calls = len([call for call in calls if call[-1] == "status"])
        if command == "status" and status_calls <= 2:
            stdout = "✗ Gateway is not running\n"
        elif command == "start" and len([call for call in calls if call[-1] == "start"]) == 1:
            ok = False
            returncode = 1
            stdout = "✗ Gateway service is not installed\n  Run: hermes gateway install\n"
        elif command == "install":
            stdout = "✓ Gateway service installed\n"
        elif command == "status":
            stdout = "✓ Gateway is running (PID: 123)\n"
        return {
            "args": args,
            "returncode": returncode,
            "ok": ok,
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
        patch("hermes_runtime.commands.start_hermes.shutil.which", return_value=None),
        patch("hermes_runtime.commands.start_hermes.run_process", fake_run_process),
        patch("hermes_runtime.commands.start_hermes.probe_hermes_status", fake_status),
        patch(
            "hermes_runtime.commands.start_hermes.load_env_files_into_process",
            return_value={"loaded": True, "keys": ["TELEGRAM_BOT_TOKEN"]},
        ),
    ):
        result = asyncio.run(run_command(SimpleNamespace(), {"kind": "start_hermes"}))

    assert calls == [
        ["/usr/local/bin/hermes", "gateway", "status"],
        ["/usr/local/bin/hermes", "gateway", "start"],
        ["/usr/local/bin/hermes", "gateway", "status"],
        ["/usr/local/bin/hermes", "gateway", "install"],
        ["/usr/local/bin/hermes", "gateway", "start"],
        ["/usr/local/bin/hermes", "gateway", "status"],
    ]
    assert result["started"] is True
    assert result["healthy"] is True
    assert result["gateway"]["install"]["ok"] is True
    assert result["gateway"]["foreground"] is None
