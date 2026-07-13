"""Focused tests for unit-scoped Hermes gateway recovery helpers.

Usage (unittest, from repo root):
    python3 -m unittest tests.test_gateway_service -v
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hermes_runtime.gateway_service as gateway_service  # noqa: E402


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


def _show(*, invocation: str, pid: int, active: str = "active") -> str:
    return (
        "LoadState=loaded\n"
        f"ActiveState={active}\n"
        "SubState=running\n"
        "Result=success\n"
        f"MainPID={pid}\n"
        f"InvocationID={invocation}\n"
        f"ActiveEnterTimestampMonotonic={pid * 100}\n"
        f"ExecMainStartTimestampMonotonic={pid * 100}\n"
    )


def test_discover_selects_user_manager_when_system_unit_is_missing() -> None:
    async def fake_run_process(
        args: list[str], *, timeout_seconds: float, env: dict[str, str] | None = None
    ) -> dict[str, object]:
        del timeout_seconds, env
        if "--user" in args:
            return {"ok": True, "stdout": _show(invocation="user-1", pid=41)}
        return {"ok": True, "stdout": "LoadState=not-found\n"}

    with (
        patch("hermes_runtime.gateway_service.shutil.which", return_value="/bin/systemctl"),
        patch("hermes_runtime.gateway_service.run_process", fake_run_process),
    ):
        result = asyncio.run(gateway_service.discover_gateway_service())

    assert result["ok"] is True
    assert result["owner"]["manager"] == "user"
    assert result["generation"]["invocation_id"] == "user-1"


def test_discover_fails_closed_when_both_managers_have_live_units() -> None:
    async def fake_run_process(
        args: list[str], *, timeout_seconds: float, env: dict[str, str] | None = None
    ) -> dict[str, object]:
        del timeout_seconds, env
        manager = "user" if "--user" in args else "system"
        return {"ok": True, "stdout": _show(invocation=manager, pid=len(args))}

    with (
        patch("hermes_runtime.gateway_service.shutil.which", return_value="/bin/systemctl"),
        patch("hermes_runtime.gateway_service.run_process", fake_run_process),
    ):
        result = asyncio.run(gateway_service.discover_gateway_service())

    assert result["ok"] is False
    assert result["reason"] == "gateway_service_owner_ambiguous"


def test_discover_fails_closed_when_second_loaded_unit_is_inactive() -> None:
    async def fake_run_process(
        args: list[str], *, timeout_seconds: float, env: dict[str, str] | None = None
    ) -> dict[str, object]:
        del timeout_seconds, env
        if "--user" in args:
            return {"ok": True, "stdout": _show(invocation="user", pid=42)}
        return {
            "ok": True,
            "stdout": _show(invocation="system", pid=0, active="inactive"),
        }

    with (
        patch("hermes_runtime.gateway_service.shutil.which", return_value="/bin/systemctl"),
        patch("hermes_runtime.gateway_service.run_process", fake_run_process),
    ):
        result = asyncio.run(gateway_service.discover_gateway_service())

    assert result["ok"] is False
    assert result["reason"] == "gateway_service_owner_ambiguous"


def test_discover_reports_explicitly_absent_systemd_managers() -> None:
    environments: list[dict[str, str] | None] = []

    async def fake_run_process(
        args: list[str], *, timeout_seconds: float, env: dict[str, str] | None = None
    ) -> dict[str, object]:
        del timeout_seconds
        environments.append(env)
        error = (
            "Failed to connect to bus: No medium found"
            if "--user" in args
            else "System has not been booted with systemd as init system (PID 1)."
        )
        return {"ok": False, "returncode": 1, "stdout": "", "stderr": error}

    with (
        patch("hermes_runtime.gateway_service.shutil.which", return_value="/bin/systemctl"),
        patch("hermes_runtime.gateway_service.run_process", fake_run_process),
    ):
        result = asyncio.run(gateway_service.discover_gateway_service())

    assert result["ok"] is False
    assert result["reason"] == "systemd_manager_absent"
    assert environments == [
        {"LC_ALL": "C", "LANG": "C"},
        {"LC_ALL": "C", "LANG": "C"},
    ]


def test_discover_selects_loaded_unit_when_other_manager_is_absent() -> None:
    async def fake_run_process(
        args: list[str], *, timeout_seconds: float, env: dict[str, str] | None = None
    ) -> dict[str, object]:
        del timeout_seconds, env
        if "--user" in args:
            return {
                "ok": False,
                "returncode": 1,
                "stdout": "",
                "stderr": "Failed to connect to bus: Host is down",
            }
        return {"ok": True, "stdout": _show(invocation="system-1", pid=52)}

    with (
        patch("hermes_runtime.gateway_service.shutil.which", return_value="/bin/systemctl"),
        patch("hermes_runtime.gateway_service.run_process", fake_run_process),
    ):
        result = asyncio.run(gateway_service.discover_gateway_service())

    assert result["ok"] is True
    assert result["owner"]["manager"] == "system"


def test_discover_keeps_generic_bus_failure_fail_closed() -> None:
    async def fake_run_process(
        args: list[str], *, timeout_seconds: float, env: dict[str, str] | None = None
    ) -> dict[str, object]:
        del timeout_seconds, env
        if "--user" in args:
            return {
                "ok": False,
                "returncode": 1,
                "stdout": "",
                "stderr": "Failed to connect to bus: Access denied",
            }
        return {"ok": True, "stdout": "LoadState=not-found\n"}

    with (
        patch("hermes_runtime.gateway_service.shutil.which", return_value="/bin/systemctl"),
        patch("hermes_runtime.gateway_service.run_process", fake_run_process),
    ):
        result = asyncio.run(gateway_service.discover_gateway_service())

    assert result["ok"] is False
    assert result["reason"] == "gateway_service_probe_unavailable"


def test_generation_change_prefers_invocation_id_and_falls_back_to_pid() -> None:
    before = {"manager": "user", "invocation_id": "one", "main_pid": 10}
    reused_pid = {"manager": "user", "invocation_id": "two", "main_pid": 10}
    no_ids_before = {"manager": "user", "invocation_id": None, "main_pid": 10}
    no_ids_after = {"manager": "user", "invocation_id": None, "main_pid": 11}

    assert gateway_service.gateway_generation_changed(before, reused_pid) is True
    assert (
        gateway_service.gateway_generation_changed(no_ids_before, no_ids_after) is True
    )


def test_generation_same_requires_positive_identity_evidence() -> None:
    unknown = {
        "manager": "user",
        "invocation_id": None,
        "main_pid": 0,
        "active_enter_timestamp_monotonic": 0,
        "exec_main_start_timestamp_monotonic": 0,
    }

    assert gateway_service.gateway_generation_same(unknown, dict(unknown)) is False


def test_force_action_targets_only_proven_unit_and_manager() -> None:
    calls: list[list[str]] = []

    async def fake_run_process(
        args: list[str], *, timeout_seconds: float, env: dict[str, str] | None = None
    ) -> dict[str, object]:
        del timeout_seconds, env
        calls.append(args)
        return {"ok": True, "returncode": 0}

    with patch("hermes_runtime.gateway_service.run_process", fake_run_process):
        result = asyncio.run(
            gateway_service.run_gateway_service_action(
                {"manager": "user", "systemctl": "/bin/systemctl"},
                "kill",
            )
        )

    assert result["ok"] is True
    assert calls == [
        [
            "/bin/systemctl",
            "--user",
            "kill",
            "--signal=SIGKILL",
            "--kill-whom=all",
            "hermes-gateway.service",
        ]
    ]


def test_transitional_generation_with_no_main_pid_still_needs_force_kill() -> None:
    generation = {
        "manager": "user",
        "active_state": "deactivating",
        "sub_state": "stop-sigterm",
        "main_pid": 0,
    }

    assert gateway_service.gateway_generation_live(generation) is False
    assert gateway_service.gateway_generation_needs_force_kill(generation) is True


if __name__ == "__main__":
    unittest.main()
