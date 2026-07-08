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
    assert result["restart"]["requested"] is False
    assert result["restart"]["performed"] is False
    assert start_calls[0]["spec"] == {"reason": "admin_heal"}
    assert "123456:token" not in str(result)


def _configured_heal_env(tmp: str) -> dict[str, str]:
    home = Path(tmp) / "hermes-home"
    home.mkdir(exist_ok=True)
    (home / ".env").write_text(
        'TELEGRAM_BOT_TOKEN="123456:token"\n'
        'TELEGRAM_ALLOWED_USERS="101"\n'
        'TELEGRAM_HOME_CHANNEL="101"\n',
        encoding="utf-8",
    )
    return {
        "HOME": str(Path(tmp) / "home"),
        "TINYHAT_HERMES_HOME": str(home),
        "HERMES_PROJECT_DIR": str(home),
        "TINYHAT_RUNTIME_STATE_DIR": str(Path(tmp) / "state"),
    }


def _fake_start_result() -> dict[str, object]:
    return {
        "schema": "tinyhat_hermes_start_v1",
        "started": True,
        "healthy": True,
        "gateway": {"healthy": True, "mode": "service"},
        "hermes": {"ok": True, "version": "Hermes Agent 0.1.0"},
        "env_reload": {"loaded": True, "keys": ["TELEGRAM_BOT_TOKEN"]},
    }


def test_heal_hermes_restart_runs_gateway_restart_then_verify() -> None:
    events: list[str] = []
    run_process_calls: list[list[str]] = []

    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del timeout_seconds, env
        run_process_calls.append(list(args))
        events.append("restart")
        return {
            "args": list(args),
            "returncode": 0,
            "ok": True,
            "timed_out": False,
            "duration_ms": 15,
            "stdout": "Gateway restarted\n",
            "stderr": "",
        }

    async def fail_stop(_ctx: object, _command: dict[str, object]) -> dict[str, object]:
        raise AssertionError("stop_hermes must not run for a gateway restart")

    async def fail_start(_ctx: object, _command: dict[str, object]) -> dict[str, object]:
        raise AssertionError("start_hermes must not run for a gateway restart")

    async def fake_probe(
        hermes_bin: Path,
        *,
        since_unix: float,
        log_path: Path | None = None,
        log_offset: int = 0,
    ) -> dict[str, object]:
        del hermes_bin, since_unix, log_path, log_offset
        events.append("verify")
        return {
            "ready": True,
            "status_healthy": True,
            "telegram_evidence": "journal",
            # Positive-only Telegram evidence: only True or None ever reach here.
            "telegram_connected": None,
            "status": {"ok": True, "stdout": "Active: active (running)"},
        }

    with tempfile.TemporaryDirectory() as tmp:
        with (
            patch.dict(os.environ, _configured_heal_env(tmp), clear=True),
            patch(
                "hermes_runtime.commands.heal_hermes.find_hermes_binary",
                return_value=Path("/usr/local/bin/hermes"),
            ),
            patch(
                "hermes_runtime.commands.heal_hermes.run_process",
                fake_run_process,
            ),
            patch("hermes_runtime.commands.heal_hermes.stop_hermes.run", fail_stop),
            patch("hermes_runtime.commands.heal_hermes.start_hermes.run", fail_start),
            patch(
                "hermes_runtime.commands.heal_hermes.probe_functional_readiness",
                fake_probe,
            ),
        ):
            result = asyncio.run(
                run_command(
                    SimpleNamespace(),
                    {
                        "kind": "heal_hermes",
                        "spec": {"restart": True, "reason": "secret_saved_restart"},
                    },
                )
            )

    # The official one-shot Hermes CLI restart is invoked exactly once; the
    # hand-rolled stop/start commands are never used on the restart path.
    assert run_process_calls == [["/usr/local/bin/hermes", "gateway", "restart"]]
    assert events == ["restart", "verify"]
    assert result["schema"] == "tinyhat_hermes_heal_v1"
    assert result["healthy"] is True
    assert result["healed"] is True
    assert result["reason"] == "gateway_restart_verified"
    restart = result["restart"]
    assert restart["requested"] is True
    assert restart["performed"] is True
    assert restart["deadline_seconds"] == 90
    assert restart["deadline_exceeded"] is False
    assert restart["telegram_evidence"] == "journal"
    milestones = restart["milestones_ms"]
    for key in ("restart_started", "restart_done", "verified"):
        assert isinstance(milestones[key], int)
        assert milestones[key] >= 0
    assert milestones["restart_started"] <= milestones["restart_done"]
    assert milestones["restart_done"] <= milestones["verified"]
    assert result["env_reload"]["loaded"] is True
    assert "123456:token" not in str(result)


def test_heal_hermes_restart_deadline_exceeded_reports_unhealthy() -> None:
    clock = {"now": 0.0}

    def fake_monotonic() -> float:
        return clock["now"]

    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del timeout_seconds, env
        return {
            "args": list(args),
            "returncode": 0,
            "ok": True,
            "timed_out": False,
            "duration_ms": 5,
            "stdout": "Gateway restarted\n",
            "stderr": "",
        }

    async def fail_stop(_ctx: object, _command: dict[str, object]) -> dict[str, object]:
        raise AssertionError("stop_hermes must not run for a gateway restart")

    async def fail_start(_ctx: object, _command: dict[str, object]) -> dict[str, object]:
        raise AssertionError("start_hermes must not run for a gateway restart")

    async def fake_probe(
        hermes_bin: Path,
        *,
        since_unix: float,
        log_path: Path | None = None,
        log_offset: int = 0,
    ) -> dict[str, object]:
        del hermes_bin, since_unix, log_path, log_offset
        # Advance the injected clock past the (clamped) 30s deadline so the
        # first probe that is not ready ends the poll loop.
        clock["now"] += 31.0
        return {
            "ready": False,
            "status_healthy": False,
            "telegram_evidence": "unavailable",
            # Positive-only evidence: absence is None, never False.
            "telegram_connected": None,
            "status": {"ok": True, "stdout": "Active: inactive (dead)"},
        }

    with tempfile.TemporaryDirectory() as tmp:
        with (
            patch.dict(os.environ, _configured_heal_env(tmp), clear=True),
            patch(
                "hermes_runtime.commands.heal_hermes.find_hermes_binary",
                return_value=Path("/usr/local/bin/hermes"),
            ),
            patch("hermes_runtime.commands.heal_hermes._monotonic", fake_monotonic),
            patch(
                "hermes_runtime.commands.heal_hermes.run_process",
                fake_run_process,
            ),
            patch("hermes_runtime.commands.heal_hermes.stop_hermes.run", fail_stop),
            patch("hermes_runtime.commands.heal_hermes.start_hermes.run", fail_start),
            patch(
                "hermes_runtime.commands.heal_hermes.probe_functional_readiness",
                fake_probe,
            ),
        ):
            result = asyncio.run(
                run_command(
                    SimpleNamespace(),
                    {
                        "kind": "heal_hermes",
                        # 5 is below the floor and must clamp up to 30.
                        "spec": {"restart": True, "deadline_seconds": 5},
                    },
                )
            )

    assert result["healthy"] is False
    assert result["healed"] is False
    assert result["reason"] == "gateway_restart_deadline_exceeded"
    restart = result["restart"]
    assert restart["requested"] is True
    assert restart["performed"] is True
    assert restart["deadline_seconds"] == 30
    assert restart["deadline_exceeded"] is True
    assert restart["telegram_evidence"] == "unavailable"
    assert isinstance(restart["milestones_ms"]["restart_started"], int)
    assert isinstance(restart["milestones_ms"]["restart_done"], int)
    assert restart["milestones_ms"]["verified"] is None


def test_heal_hermes_restart_command_failure_is_not_verified() -> None:
    """A failed ``hermes gateway restart`` must not report a false success even
    when the OLD gateway unit is still active (the command never cycled it)."""

    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del timeout_seconds, env
        return {
            "args": list(args),
            "returncode": 1,
            "ok": False,
            "timed_out": False,
            "duration_ms": 12,
            "stdout": "",
            "stderr": "Failed to restart hermes-gateway.service\n",
        }

    async def fake_probe(
        hermes_bin: Path,
        *,
        since_unix: float,
        log_path: Path | None = None,
        log_offset: int = 0,
    ) -> dict[str, object]:
        # The old gateway is still active; status-only readiness with no
        # Telegram evidence would say "ready" -- the false-positive we guard.
        del hermes_bin, since_unix, log_path, log_offset
        return {
            "ready": True,
            "status_healthy": True,
            "telegram_evidence": "unavailable",
            "telegram_connected": None,
            "status": {"ok": True, "stdout": "Active: active (running)"},
        }

    with tempfile.TemporaryDirectory() as tmp:
        with (
            patch.dict(os.environ, _configured_heal_env(tmp), clear=True),
            patch(
                "hermes_runtime.commands.heal_hermes.find_hermes_binary",
                return_value=Path("/usr/local/bin/hermes"),
            ),
            patch(
                "hermes_runtime.commands.heal_hermes.run_process",
                fake_run_process,
            ),
            patch(
                "hermes_runtime.commands.heal_hermes.probe_functional_readiness",
                fake_probe,
            ),
        ):
            result = asyncio.run(
                run_command(
                    SimpleNamespace(),
                    {
                        "kind": "heal_hermes",
                        "spec": {"restart": True, "reason": "secret_saved_restart"},
                    },
                )
            )

    # The restart command failed, so heal must not claim verified success even
    # though the status probe reported an active (old) gateway.
    assert result["healthy"] is False
    assert result["healed"] is False
    assert result["reason"] == "gateway_restart_command_failed"
    restart = result["restart"]
    assert restart["requested"] is True
    assert restart["performed"] is True
    assert restart["deadline_exceeded"] is False
    assert restart["milestones_ms"]["verified"] is None


def test_heal_hermes_without_restart_flag_never_restarts_gateway() -> None:
    run_process_calls: list[list[str]] = []

    async def recording_run_process(
        args: list[str],
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del timeout_seconds, env
        run_process_calls.append(list(args))
        raise AssertionError(
            "run_process (gateway restart) must not run for a start-only heal"
        )

    async def fail_stop(_ctx: object, _command: dict[str, object]) -> dict[str, object]:
        raise AssertionError("stop_hermes must not run for a start-only heal")

    async def fake_start(_ctx: object, _command: dict[str, object]) -> dict[str, object]:
        return _fake_start_result()

    with tempfile.TemporaryDirectory() as tmp:
        with (
            patch.dict(os.environ, _configured_heal_env(tmp), clear=True),
            patch(
                "hermes_runtime.commands.heal_hermes.find_hermes_binary",
                return_value=Path("/usr/local/bin/hermes"),
            ),
            patch(
                "hermes_runtime.commands.heal_hermes.run_process",
                recording_run_process,
            ),
            patch("hermes_runtime.commands.heal_hermes.stop_hermes.run", fail_stop),
            patch("hermes_runtime.commands.heal_hermes.start_hermes.run", fake_start),
        ):
            result = asyncio.run(
                run_command(
                    SimpleNamespace(),
                    {"kind": "heal_hermes", "spec": {"reason": "admin_heal"}},
                )
            )

    # A start-only heal reuses the durable start path and never invokes the
    # one-shot `hermes gateway restart`.
    assert run_process_calls == []
    assert result["healthy"] is True
    assert result["restart"]["requested"] is False
    assert result["restart"]["performed"] is False
    assert result["restart"]["deadline_exceeded"] is False
    assert result["restart"]["milestones_ms"] == {}


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
