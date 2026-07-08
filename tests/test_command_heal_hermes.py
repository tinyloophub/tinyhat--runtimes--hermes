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


def test_heal_hermes_restart_runs_stop_start_then_verify() -> None:
    events: list[str] = []

    async def fake_stop(_ctx: object, command: dict[str, object]) -> dict[str, object]:
        events.append("stop")
        assert command["kind"] == "stop_hermes"
        assert command["spec"] == {"reason": "secret_saved_restart"}
        return {"schema": "tinyhat_hermes_stop_v1", "stopped": True}

    async def fake_start(_ctx: object, command: dict[str, object]) -> dict[str, object]:
        events.append("start")
        assert command["kind"] == "start_hermes"
        assert command["spec"] == {"reason": "secret_saved_restart"}
        return _fake_start_result()

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
            "telegram_connected": True,
            "status": {"ok": True},
        }

    with tempfile.TemporaryDirectory() as tmp:
        with (
            patch.dict(os.environ, _configured_heal_env(tmp), clear=True),
            patch(
                "hermes_runtime.commands.heal_hermes.find_hermes_binary",
                return_value=Path("/usr/local/bin/hermes"),
            ),
            patch("hermes_runtime.commands.heal_hermes.stop_hermes.run", fake_stop),
            patch("hermes_runtime.commands.heal_hermes.start_hermes.run", fake_start),
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

    assert events == ["stop", "start", "verify"]
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
    for key in ("stop_started", "stop_done", "start_started", "start_done", "verified"):
        assert isinstance(milestones[key], int)
        assert milestones[key] >= 0
    assert milestones["stop_started"] <= milestones["stop_done"]
    assert milestones["start_started"] <= milestones["start_done"]
    assert milestones["start_done"] <= milestones["verified"]
    assert result["env_reload"]["loaded"] is True
    assert "123456:token" not in str(result)


def test_heal_hermes_restart_deadline_exceeded_reports_unhealthy() -> None:
    clock = {"now": 0.0}

    def fake_monotonic() -> float:
        return clock["now"]

    async def fake_stop(_ctx: object, _command: dict[str, object]) -> dict[str, object]:
        return {"schema": "tinyhat_hermes_stop_v1", "stopped": True}

    async def fake_start(_ctx: object, _command: dict[str, object]) -> dict[str, object]:
        return _fake_start_result()

    async def fake_probe(
        hermes_bin: Path,
        *,
        since_unix: float,
        log_path: Path | None = None,
        log_offset: int = 0,
    ) -> dict[str, object]:
        del hermes_bin, since_unix, log_path, log_offset
        clock["now"] += 31.0
        return {
            "ready": False,
            "status_healthy": True,
            "telegram_evidence": "log",
            "telegram_connected": False,
            "status": {"ok": True},
        }

    with tempfile.TemporaryDirectory() as tmp:
        with (
            patch.dict(os.environ, _configured_heal_env(tmp), clear=True),
            patch(
                "hermes_runtime.commands.heal_hermes.find_hermes_binary",
                return_value=Path("/usr/local/bin/hermes"),
            ),
            patch("hermes_runtime.commands.heal_hermes._monotonic", fake_monotonic),
            patch("hermes_runtime.commands.heal_hermes.stop_hermes.run", fake_stop),
            patch("hermes_runtime.commands.heal_hermes.start_hermes.run", fake_start),
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
    assert restart["telegram_evidence"] == "log"
    assert restart["milestones_ms"]["verified"] is None


def test_heal_hermes_without_restart_flag_never_stops_gateway() -> None:
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
            patch("hermes_runtime.commands.heal_hermes.stop_hermes.run", fail_stop),
            patch("hermes_runtime.commands.heal_hermes.start_hermes.run", fake_start),
        ):
            result = asyncio.run(
                run_command(
                    SimpleNamespace(),
                    {"kind": "heal_hermes", "spec": {"reason": "admin_heal"}},
                )
            )

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
