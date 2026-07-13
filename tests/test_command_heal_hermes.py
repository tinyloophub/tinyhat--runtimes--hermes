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
            "already_running": False,
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
    assert result["healed"] is False
    assert result["telegram"]["configured"] is True
    assert result["gateway"] == {"healthy": True, "mode": "service"}
    assert result["reason"] == "gateway_started_unverified"
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
        "already_running": False,
        "gateway": {"healthy": True, "mode": "service"},
        "hermes": {"ok": True, "version": "Hermes Agent 0.1.0"},
        "env_reload": {"loaded": True, "keys": ["TELEGRAM_BOT_TOKEN"]},
    }


def test_restart_heal_applies_network_fallback_before_reload_and_restart() -> None:
    events: list[str] = []
    network_metadata = {
        "ok": True,
        "seeded": True,
        "preserved_existing": False,
        "key": "TELEGRAM_FALLBACK_IPS",
        "files": [
            {
                "path": "managed.env",
                "updated": True,
                "keys": ["TELEGRAM_FALLBACK_IPS"],
            }
        ],
    }

    def fake_ensure(paths: list[Path]) -> dict[str, object]:
        assert len(paths) == 1
        events.append("network_fallback")
        return network_metadata

    def fake_reload(paths: list[Path]) -> dict[str, object]:
        assert len(paths) == 1
        events.append("env_reload")
        return {"loaded": True, "keys": ["TELEGRAM_FALLBACK_IPS"]}

    async def fake_restart(
        _ctx: object,
        *,
        hermes_bin: Path,
        reason: str,
        deadline_seconds: int,
    ) -> dict[str, object]:
        del hermes_bin, reason, deadline_seconds
        events.append("restart")
        return {
            "verified": True,
            "functionally_verified": True,
            "deadline_exceeded": False,
            "restart_command_ok": True,
            "performed": True,
            "method": "official",
            "fallback_attempted": False,
            "failure_reason": None,
            "milestones_ms": {
                "restart_started": 0,
                "restart_done": 10,
                "verified": 20,
            },
            "readiness": {
                "status_healthy": True,
                "telegram_evidence": "journal",
                "telegram_connected": True,
                "status": {"ok": True},
            },
            "restart_result": {"ok": True},
            "fallback_actions": {},
            "generation": {"owner": "user", "changed": True, "active": True},
        }

    with tempfile.TemporaryDirectory() as tmp:
        env = _configured_heal_env(tmp)
        env_path = Path(env["TINYHAT_HERMES_HOME"]) / ".env"
        with (
            patch.dict(os.environ, env, clear=True),
            patch(
                "hermes_runtime.commands.heal_hermes.find_hermes_binary",
                return_value=Path("/usr/local/bin/hermes"),
            ),
            patch(
                "hermes_runtime.commands.heal_hermes._env_file_candidates",
                return_value=[env_path],
            ),
            patch(
                "hermes_runtime.commands.heal_hermes.ensure_telegram_network_fallback_env",
                fake_ensure,
            ),
            patch(
                "hermes_runtime.commands.heal_hermes.load_env_files_into_process",
                fake_reload,
            ),
            patch(
                "hermes_runtime.commands.heal_hermes._run_gateway_restart",
                fake_restart,
            ),
        ):
            result = asyncio.run(
                run_command(
                    SimpleNamespace(),
                    {"kind": "heal_hermes", "spec": {"restart": True}},
                )
            )

    assert events == ["network_fallback", "env_reload", "restart"]
    assert result["healthy"] is True
    assert result["telegram_network"] == network_metadata
    assert result["env_reload"]["keys"] == ["TELEGRAM_FALLBACK_IPS"]


def test_start_only_heal_applies_network_fallback_before_gateway_start() -> None:
    events: list[str] = []
    network_metadata = {
        "ok": True,
        "seeded": True,
        "preserved_existing": False,
        "key": "TELEGRAM_FALLBACK_IPS",
        "files": [],
    }

    def fake_ensure(paths: list[Path]) -> dict[str, object]:
        assert len(paths) == 1
        events.append("network_fallback")
        return network_metadata

    async def fake_start(
        _ctx: object, _command: dict[str, object]
    ) -> dict[str, object]:
        events.append("start")
        return _fake_start_result()

    with tempfile.TemporaryDirectory() as tmp:
        env = _configured_heal_env(tmp)
        env_path = Path(env["TINYHAT_HERMES_HOME"]) / ".env"
        with (
            patch.dict(os.environ, env, clear=True),
            patch(
                "hermes_runtime.commands.heal_hermes.find_hermes_binary",
                return_value=Path("/usr/local/bin/hermes"),
            ),
            patch(
                "hermes_runtime.commands.heal_hermes._env_file_candidates",
                return_value=[env_path],
            ),
            patch(
                "hermes_runtime.commands.heal_hermes.ensure_telegram_network_fallback_env",
                fake_ensure,
            ),
            patch("hermes_runtime.commands.heal_hermes.start_hermes.run", fake_start),
        ):
            result = asyncio.run(
                run_command(
                    SimpleNamespace(),
                    {
                        "kind": "heal_hermes",
                        "spec": {"restart": False, "reason": "health_check"},
                    },
                )
            )

    assert events == ["network_fallback", "start"]
    assert result["telegram_network"] == network_metadata


def test_heal_network_preparation_failure_never_starts_or_restarts() -> None:
    sensitive_detail = "203.0.113.80"
    network_metadata = {
        "ok": False,
        "seeded": False,
        "preserved_existing": True,
        "source": "process_env",
        "key": "TELEGRAM_FALLBACK_IPS",
        "files": [
            {
                "path": "managed.env",
                "updated": False,
                "keys": ["TELEGRAM_FALLBACK_IPS"],
                "error_type": "PermissionError",
            }
        ],
        "error": "env_write_failed",
    }

    async def fail_start(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("gateway start must not run after env preparation fails")

    async def fail_restart(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("gateway restart must not run after env preparation fails")

    async def fake_status() -> dict[str, object]:
        return {"installed": True, "ok": True, "version": "Hermes Agent 0.1.0"}

    for restart in (False, True):
        with tempfile.TemporaryDirectory() as tmp:
            env = _configured_heal_env(tmp)
            with (
                patch.dict(
                    os.environ,
                    {**env, "TELEGRAM_FALLBACK_IPS": sensitive_detail},
                    clear=True,
                ),
                patch(
                    "hermes_runtime.commands.heal_hermes.find_hermes_binary",
                    return_value=Path("/usr/local/bin/hermes"),
                ),
                patch(
                    "hermes_runtime.commands.heal_hermes.ensure_telegram_network_fallback_env",
                    return_value=network_metadata,
                ),
                patch("hermes_runtime.commands.heal_hermes.start_hermes.run", fail_start),
                patch(
                    "hermes_runtime.commands.heal_hermes._run_gateway_restart",
                    fail_restart,
                ),
                patch(
                    "hermes_runtime.commands.heal_hermes.probe_hermes_status",
                    fake_status,
                ),
            ):
                result = asyncio.run(
                    run_command(
                        SimpleNamespace(),
                        {"kind": "heal_hermes", "spec": {"restart": restart}},
                    )
                )

        assert result["healthy"] is False
        assert result["healed"] is False
        assert result["reason"] == "telegram_network_env_failed"
        assert result["telegram_network"] == network_metadata
        assert result["restart"]["requested"] is restart
        assert result["restart"]["performed"] is False
        assert sensitive_detail not in str(result)


def test_start_only_heal_does_not_claim_already_running_gateway_was_healed() -> None:
    async def fake_start(_ctx: object, _command: dict[str, object]) -> dict[str, object]:
        return {
            **_fake_start_result(),
            "already_running": True,
        }

    with tempfile.TemporaryDirectory() as tmp:
        with (
            patch.dict(os.environ, _configured_heal_env(tmp), clear=True),
            patch(
                "hermes_runtime.commands.heal_hermes.find_hermes_binary",
                return_value=Path("/usr/local/bin/hermes"),
            ),
            patch("hermes_runtime.commands.heal_hermes.start_hermes.run", fake_start),
        ):
            result = asyncio.run(
                run_command(
                    SimpleNamespace(),
                    {
                        "kind": "heal_hermes",
                        "spec": {"restart": False, "reason": "health_check"},
                    },
                )
            )

    assert result["healthy"] is True
    assert result["healed"] is False
    assert result["reason"] == "gateway_checked_healthy"
    assert result["message"] == "Hermes Telegram gateway was already running."


def _generation(
    invocation_id: str,
    pid: int,
    *,
    manager: str = "user",
) -> dict[str, object]:
    return {
        "manager": manager,
        "load_state": "loaded",
        "active_state": "active",
        "sub_state": "running",
        "result": "success",
        "main_pid": pid,
        "invocation_id": invocation_id,
        "active_enter_timestamp_monotonic": pid * 100,
        "exec_main_start_timestamp_monotonic": pid * 100,
    }


def _discovery(generation: dict[str, object]) -> dict[str, object]:
    return {
        "ok": True,
        "reason": "gateway_service_owner_found",
        "owner": {
            "manager": generation["manager"],
            "systemctl": "/bin/systemctl",
        },
        "generation": generation,
    }


def test_heal_hermes_restart_runs_gateway_restart_then_verify() -> None:
    events: list[str] = []
    run_process_calls: list[list[str]] = []
    before = _generation("old-invocation", 100)
    after = _generation("new-invocation", 200)
    process_options: list[tuple[float, bool]] = []

    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: float,
        env: dict[str, str] | None = None,
        kill_process_group: bool = False,
    ) -> dict[str, object]:
        del env
        run_process_calls.append(list(args))
        process_options.append((timeout_seconds, kill_process_group))
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

    async def fake_snapshot(
        _owner: dict[str, str], *, timeout_seconds: float = 5
    ) -> dict[str, object]:
        del timeout_seconds
        return after

    async def fake_probe(
        hermes_bin: Path,
        *,
        since_unix: float,
        log_path: Path | None = None,
        log_offset: int = 0,
        service_manager: str = "user",
        service_invocation_id: str | None = None,
        service_main_pid: int | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        del hermes_bin, since_unix, log_path, log_offset, service_manager
        assert service_invocation_id == "new-invocation"
        assert service_main_pid == 200
        assert timeout_seconds is not None and timeout_seconds > 0
        events.append("verify")
        return {
            "ready": True,
            "status_healthy": True,
            "telegram_evidence": "journal",
            "telegram_connected": True,
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
                "hermes_runtime.commands.heal_hermes.discover_gateway_service",
                return_value=_discovery(before),
            ),
            patch(
                "hermes_runtime.commands.heal_hermes.snapshot_gateway_service",
                fake_snapshot,
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

    # The official one-shot Hermes CLI restart is invoked exactly once; the
    # hand-rolled stop/start commands are never used on the restart path.
    assert run_process_calls == [["/usr/local/bin/hermes", "gateway", "restart"]]
    assert process_options == [(20, True)]
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
    assert restart["verified"] is True
    assert restart["functionally_verified"] is True
    assert restart["generation"]["before"]["invocation_id"] == "old-invocation"
    assert restart["generation"]["after"]["invocation_id"] == "new-invocation"
    assert restart["generation"]["changed"] is True
    milestones = restart["milestones_ms"]
    for key in ("restart_started", "restart_done", "verified"):
        assert isinstance(milestones[key], int)
        assert milestones[key] >= 0
    assert milestones["restart_started"] <= milestones["restart_done"]
    assert milestones["restart_done"] <= milestones["verified"]
    assert result["env_reload"]["loaded"] is True
    assert "123456:token" not in str(result)


def test_restart_preserves_official_command_without_systemd_owner() -> None:
    for discovery_reason in (
        "systemctl_unavailable",
        "gateway_service_not_found",
    ):
        run_process_calls: list[list[str]] = []

        async def fake_run_process(
            args: list[str],
            *,
            timeout_seconds: float,
            env: dict[str, str] | None = None,
            kill_process_group: bool = False,
        ) -> dict[str, object]:
            del timeout_seconds, env
            assert kill_process_group is True
            run_process_calls.append(list(args))
            return {
                "args": list(args),
                "returncode": 0,
                "ok": True,
                "timed_out": False,
                "duration_ms": 12,
                "stdout": "Gateway restarted\n",
                "stderr": "",
            }

        async def fake_probe(
            hermes_bin: Path,
            *,
            since_unix: float,
            log_path: Path | None = None,
            log_offset: int = 0,
            service_manager: str = "user",
            service_invocation_id: str | None = None,
            service_main_pid: int | None = None,
            timeout_seconds: float | None = None,
        ) -> dict[str, object]:
            del hermes_bin, since_unix, log_path, log_offset, service_manager
            assert service_invocation_id is None
            assert service_main_pid is None
            assert timeout_seconds is not None and timeout_seconds > 0
            return {
                "ready": True,
                "functionally_ready": True,
                "status_healthy": True,
                "telegram_evidence": "log",
                "telegram_connected": True,
                "status": {"ok": True, "stdout": "Gateway is running"},
            }

        async def fail_service_action(
            *_args: object, **_kwargs: object
        ) -> dict[str, object]:
            raise AssertionError("foreground restart must not force-cycle systemd")

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.dict(os.environ, _configured_heal_env(tmp), clear=True),
                patch(
                    "hermes_runtime.commands.heal_hermes.find_hermes_binary",
                    return_value=Path("/usr/local/bin/hermes"),
                ),
                patch(
                    "hermes_runtime.commands.heal_hermes.discover_gateway_service",
                    return_value={
                        "ok": False,
                        "reason": discovery_reason,
                        "owner": None,
                        "generation": None,
                    },
                ),
                patch(
                    "hermes_runtime.commands.heal_hermes.run_process",
                    fake_run_process,
                ),
                patch(
                    "hermes_runtime.commands.heal_hermes.run_gateway_service_action",
                    fail_service_action,
                ),
                patch(
                    "hermes_runtime.commands.heal_hermes.probe_functional_readiness",
                    fake_probe,
                ),
            ):
                result = asyncio.run(
                    run_command(
                        SimpleNamespace(),
                        {"kind": "heal_hermes", "spec": {"restart": True}},
                    )
                )

        assert run_process_calls == [
            ["/usr/local/bin/hermes", "gateway", "restart"]
        ]
        assert result["healthy"] is True
        assert result["healed"] is True
        assert result["reason"] == "gateway_restart_verified"
        restart = result["restart"]
        assert restart["performed"] is True
        assert restart["verified"] is True
        assert restart["functionally_verified"] is True
        assert restart["method"] == "official_foreground"
        assert restart["fallback_attempted"] is False
        assert restart["telegram_evidence"] == "log"
        assert restart["generation"] == {
            "owner": "foreground",
            "before": None,
            "after": None,
            "changed": False,
            "active": False,
        }


def test_heal_hermes_restart_deadline_exceeded_reports_unhealthy() -> None:
    clock = {"now": 0.0}
    before = _generation("old-invocation", 100)
    after = _generation("new-invocation", 200)

    def fake_monotonic() -> float:
        return clock["now"]

    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: float,
        env: dict[str, str] | None = None,
        kill_process_group: bool = False,
    ) -> dict[str, object]:
        del timeout_seconds, env, kill_process_group
        return {
            "args": list(args),
            "returncode": 0,
            "ok": True,
            "timed_out": False,
            "duration_ms": 5,
            "stdout": "Gateway restarted\n",
            "stderr": "",
        }

    async def fake_snapshot(
        _owner: dict[str, str], *, timeout_seconds: float = 5
    ) -> dict[str, object]:
        del timeout_seconds
        return after

    async def fake_probe(
        hermes_bin: Path,
        *,
        since_unix: float,
        log_path: Path | None = None,
        log_offset: int = 0,
        service_manager: str = "user",
        service_invocation_id: str | None = None,
        service_main_pid: int | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        del hermes_bin, since_unix, log_path, log_offset, service_manager
        assert service_invocation_id == "new-invocation"
        assert service_main_pid == 200
        assert timeout_seconds is not None and timeout_seconds > 0
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
            patch(
                "hermes_runtime.commands.heal_hermes.discover_gateway_service",
                return_value=_discovery(before),
            ),
            patch(
                "hermes_runtime.commands.heal_hermes.snapshot_gateway_service",
                fake_snapshot,
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


def test_heal_hermes_rejects_positive_readiness_returned_after_deadline() -> None:
    clock = {"now": 0.0}
    before = _generation("old-invocation", 100)
    after = _generation("new-invocation", 200)

    def fake_monotonic() -> float:
        return clock["now"]

    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: float,
        env: dict[str, str] | None = None,
        kill_process_group: bool = False,
    ) -> dict[str, object]:
        del timeout_seconds, env, kill_process_group
        return {
            "args": list(args),
            "returncode": 0,
            "ok": True,
            "timed_out": False,
            "duration_ms": 5,
            "stdout": "Gateway restarted\n",
            "stderr": "",
        }

    async def fake_snapshot(
        _owner: dict[str, str], *, timeout_seconds: float = 5
    ) -> dict[str, object]:
        del timeout_seconds
        return after

    async def fake_probe(
        hermes_bin: Path,
        *,
        since_unix: float,
        log_path: Path | None = None,
        log_offset: int = 0,
        service_manager: str = "user",
        service_invocation_id: str | None = None,
        service_main_pid: int | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        del hermes_bin, since_unix, log_path, log_offset, service_manager
        assert service_invocation_id == "new-invocation"
        assert service_main_pid == 200
        assert timeout_seconds is not None and timeout_seconds > 0
        clock["now"] += 31.0
        return {
            "ready": True,
            "functionally_ready": True,
            "status_healthy": True,
            "telegram_evidence": "journal",
            "telegram_connected": True,
            "status": {"ok": True, "stdout": "Active: active (running)"},
        }

    with tempfile.TemporaryDirectory() as tmp:
        with (
            patch.dict(os.environ, _configured_heal_env(tmp), clear=True),
            patch(
                "hermes_runtime.commands.heal_hermes.find_hermes_binary",
                return_value=Path("/usr/local/bin/hermes"),
            ),
            patch("hermes_runtime.commands.heal_hermes._monotonic", fake_monotonic),
            patch("hermes_runtime.commands.heal_hermes.run_process", fake_run_process),
            patch(
                "hermes_runtime.commands.heal_hermes.discover_gateway_service",
                return_value=_discovery(before),
            ),
            patch(
                "hermes_runtime.commands.heal_hermes.snapshot_gateway_service",
                fake_snapshot,
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
                        "spec": {"restart": True, "deadline_seconds": 30},
                    },
                )
            )

    assert result["healthy"] is False
    assert result["healed"] is False
    assert result["reason"] == "gateway_restart_deadline_exceeded"
    assert result["restart"]["verified"] is False
    assert result["restart"]["functionally_verified"] is False
    assert result["restart"]["deadline_exceeded"] is True


def test_heal_hermes_never_starts_force_kill_without_full_cycle_reserve() -> None:
    clock = {"now": 0.0}
    before = _generation("old-invocation", 100)
    discovery_calls = 0

    def fake_monotonic() -> float:
        return clock["now"]

    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: float,
        env: dict[str, str] | None = None,
        kill_process_group: bool = False,
    ) -> dict[str, object]:
        del timeout_seconds, env, kill_process_group
        return {
            "args": list(args),
            "returncode": None,
            "ok": False,
            "timed_out": True,
            "duration_ms": 15_000,
            "stdout": "",
            "stderr": "restart timed out",
        }

    async def fake_snapshot(
        _owner: dict[str, str], *, timeout_seconds: float = 5
    ) -> dict[str, object]:
        del timeout_seconds
        return before

    async def fake_discovery() -> dict[str, object]:
        nonlocal discovery_calls
        discovery_calls += 1
        if discovery_calls == 2:
            clock["now"] = 20.0
        return _discovery(before)

    async def fail_action(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("force cycle must not begin without its full reserve")

    with tempfile.TemporaryDirectory() as tmp:
        with (
            patch.dict(os.environ, _configured_heal_env(tmp), clear=True),
            patch(
                "hermes_runtime.commands.heal_hermes.find_hermes_binary",
                return_value=Path("/usr/local/bin/hermes"),
            ),
            patch("hermes_runtime.commands.heal_hermes._monotonic", fake_monotonic),
            patch("hermes_runtime.commands.heal_hermes.run_process", fake_run_process),
            patch(
                "hermes_runtime.commands.heal_hermes.discover_gateway_service",
                fake_discovery,
            ),
            patch(
                "hermes_runtime.commands.heal_hermes.snapshot_gateway_service",
                fake_snapshot,
            ),
            patch(
                "hermes_runtime.commands.heal_hermes.run_gateway_service_action",
                fail_action,
            ),
        ):
            result = asyncio.run(
                run_command(
                    SimpleNamespace(),
                    {
                        "kind": "heal_hermes",
                        "spec": {"restart": True, "deadline_seconds": 30},
                    },
                )
            )

    assert result["healthy"] is False
    assert result["healed"] is False
    assert result["reason"] == "gateway_restart_deadline_exceeded"
    assert result["restart"]["fallback_attempted"] is False
    assert result["restart"]["deadline_exceeded"] is True
    assert result["restart_fallback"] == {}


def test_heal_hermes_force_cycles_same_generation_after_official_failure() -> None:
    before = _generation("old-invocation", 100)
    after = _generation("new-invocation", 200)
    snapshot_results = [before, after]
    actions: list[str] = []

    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: float,
        env: dict[str, str] | None = None,
        kill_process_group: bool = False,
    ) -> dict[str, object]:
        del timeout_seconds, env
        assert kill_process_group is True
        return {
            "args": list(args),
            "returncode": 1,
            "ok": False,
            "timed_out": False,
            "duration_ms": 12,
            "stdout": "",
            "stderr": "Failed to restart hermes-gateway.service\n",
        }

    async def fake_snapshot(
        _owner: dict[str, str], *, timeout_seconds: float = 5
    ) -> dict[str, object]:
        del timeout_seconds
        return snapshot_results.pop(0)

    async def fake_action(
        _owner: dict[str, str], action: str, *, timeout_seconds: float = 5
    ) -> dict[str, object]:
        del timeout_seconds
        actions.append(action)
        return {
            "ok": True,
            "returncode": 0,
            "timed_out": False,
            "duration_ms": 1,
        }

    async def fake_probe(
        hermes_bin: Path,
        *,
        since_unix: float,
        log_path: Path | None = None,
        log_offset: int = 0,
        service_manager: str = "user",
        service_invocation_id: str | None = None,
        service_main_pid: int | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        del hermes_bin, since_unix, log_path, log_offset, service_manager
        assert service_invocation_id == "new-invocation"
        assert service_main_pid == 200
        assert timeout_seconds is not None and timeout_seconds > 0
        return {
            "ready": True,
            "functionally_ready": True,
            "status_healthy": True,
            "telegram_evidence": "journal",
            "telegram_connected": True,
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
                "hermes_runtime.commands.heal_hermes.run_process", fake_run_process
            ),
            patch(
                "hermes_runtime.commands.heal_hermes.discover_gateway_service",
                side_effect=[_discovery(before), _discovery(before)],
            ),
            patch(
                "hermes_runtime.commands.heal_hermes.snapshot_gateway_service",
                fake_snapshot,
            ),
            patch(
                "hermes_runtime.commands.heal_hermes.run_gateway_service_action",
                fake_action,
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

    assert result["healthy"] is True
    assert result["healed"] is True
    assert result["reason"] == "gateway_restart_verified"
    assert actions == ["kill", "reset_failed", "start"]
    restart = result["restart"]
    assert restart["performed"] is True
    assert restart["fallback_attempted"] is True
    assert restart["method"] == "systemd_force"
    assert restart["generation"]["changed"] is True


def test_heal_hermes_completes_start_after_kill_timeout_may_have_stopped_unit() -> (
    None
):
    before = _generation("old-invocation", 100)
    inactive = {
        **before,
        "active_state": "inactive",
        "sub_state": "dead",
        "main_pid": 0,
    }
    after = _generation("new-invocation", 200)
    snapshot_results = [before, inactive, after]
    actions: list[str] = []

    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: float,
        env: dict[str, str] | None = None,
        kill_process_group: bool = False,
    ) -> dict[str, object]:
        del timeout_seconds, env, kill_process_group
        return {
            "args": list(args),
            "returncode": None,
            "ok": False,
            "timed_out": True,
            "duration_ms": 20_000,
            "stdout": "",
            "stderr": "restart timed out",
        }

    async def fake_snapshot(
        _owner: dict[str, str], *, timeout_seconds: float = 5
    ) -> dict[str, object]:
        del timeout_seconds
        return snapshot_results.pop(0)

    async def fake_action(
        _owner: dict[str, str], action: str, *, timeout_seconds: float = 5
    ) -> dict[str, object]:
        del timeout_seconds
        actions.append(action)
        if action == "kill":
            return {
                "ok": False,
                "returncode": None,
                "timed_out": True,
                "duration_ms": 5_000,
            }
        return {
            "ok": True,
            "returncode": 0,
            "timed_out": False,
            "duration_ms": 1,
        }

    async def fake_probe(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "ready": True,
            "functionally_ready": True,
            "status_healthy": True,
            "telegram_evidence": "journal",
            "telegram_connected": True,
            "status": {"ok": True, "stdout": "Active: active (running)"},
        }

    with tempfile.TemporaryDirectory() as tmp:
        with (
            patch.dict(os.environ, _configured_heal_env(tmp), clear=True),
            patch(
                "hermes_runtime.commands.heal_hermes.find_hermes_binary",
                return_value=Path("/usr/local/bin/hermes"),
            ),
            patch("hermes_runtime.commands.heal_hermes.run_process", fake_run_process),
            patch(
                "hermes_runtime.commands.heal_hermes.discover_gateway_service",
                side_effect=[_discovery(before), _discovery(before)],
            ),
            patch(
                "hermes_runtime.commands.heal_hermes.snapshot_gateway_service",
                fake_snapshot,
            ),
            patch(
                "hermes_runtime.commands.heal_hermes.run_gateway_service_action",
                fake_action,
            ),
            patch(
                "hermes_runtime.commands.heal_hermes.probe_functional_readiness",
                fake_probe,
            ),
        ):
            result = asyncio.run(
                run_command(
                    SimpleNamespace(),
                    {"kind": "heal_hermes", "spec": {"restart": True}},
                )
            )

    assert actions == ["kill", "reset_failed", "start"]
    assert result["healthy"] is True
    assert result["healed"] is True
    assert result["restart"]["functionally_verified"] is True


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


def test_admin_heal_compat_defaults_absent_restart_to_restart() -> None:
    generation = {
        "owner": "user",
        "before": _generation("old-invocation", 100),
        "after": _generation("new-invocation", 200),
        "changed": True,
        "active": True,
    }

    async def fake_restart(
        _ctx: object,
        *,
        hermes_bin: Path,
        reason: str,
        deadline_seconds: int,
    ) -> dict[str, object]:
        del hermes_bin
        assert reason == "admin_heal_hermes"
        assert deadline_seconds == 90
        return {
            "verified": True,
            "functionally_verified": True,
            "deadline_exceeded": False,
            "restart_command_ok": True,
            "performed": True,
            "method": "official",
            "fallback_attempted": False,
            "failure_reason": None,
            "milestones_ms": {
                "restart_started": 0,
                "restart_done": 10,
                "verified": 20,
            },
            "readiness": {
                "status_healthy": True,
                "telegram_evidence": "journal",
                "telegram_connected": True,
                "status": {"ok": True},
            },
            "restart_result": {"ok": True},
            "fallback_actions": {},
            "generation": generation,
        }

    with tempfile.TemporaryDirectory() as tmp:
        with (
            patch.dict(os.environ, _configured_heal_env(tmp), clear=True),
            patch(
                "hermes_runtime.commands.heal_hermes.find_hermes_binary",
                return_value=Path("/usr/local/bin/hermes"),
            ),
            patch(
                "hermes_runtime.commands.heal_hermes._run_gateway_restart",
                fake_restart,
            ),
        ):
            result = asyncio.run(
                run_command(
                    SimpleNamespace(),
                    {
                        "kind": "heal_hermes",
                        "spec": {"reason": "admin_heal_hermes"},
                    },
                )
            )

    assert result["healthy"] is True
    assert result["healed"] is True
    assert result["restart"]["requested"] is True
    assert result["restart"]["performed"] is True
    assert result["restart"]["compat_defaulted"] is True


def test_restart_without_telegram_evidence_is_not_reported_healed() -> None:
    async def fake_restart(
        _ctx: object,
        *,
        hermes_bin: Path,
        reason: str,
        deadline_seconds: int,
    ) -> dict[str, object]:
        del hermes_bin, reason, deadline_seconds
        return {
            "verified": False,
            "generation_verified": True,
            "functionally_verified": False,
            "deadline_exceeded": False,
            "restart_command_ok": True,
            "performed": True,
            "method": "official",
            "fallback_attempted": False,
            "failure_reason": "telegram_readiness_unavailable",
            "milestones_ms": {
                "restart_started": 0,
                "restart_done": 10,
                "verified": None,
            },
            "readiness": {
                "status_healthy": True,
                "telegram_evidence": "unavailable",
                "telegram_connected": None,
                "status": {"ok": True},
            },
            "restart_result": {"ok": True},
            "fallback_actions": {},
            "generation": {
                "owner": "user",
                "before": _generation("old-invocation", 100),
                "after": _generation("new-invocation", 200),
                "changed": True,
                "active": True,
            },
        }

    with tempfile.TemporaryDirectory() as tmp:
        with (
            patch.dict(os.environ, _configured_heal_env(tmp), clear=True),
            patch(
                "hermes_runtime.commands.heal_hermes.find_hermes_binary",
                return_value=Path("/usr/local/bin/hermes"),
            ),
            patch(
                "hermes_runtime.commands.heal_hermes._run_gateway_restart",
                fake_restart,
            ),
        ):
            result = asyncio.run(
                run_command(
                    SimpleNamespace(),
                    {"kind": "heal_hermes", "spec": {"restart": True}},
                )
            )

    assert result["healthy"] is False
    assert result["healed"] is False
    assert result["reason"] == "telegram_readiness_unavailable"
    assert result["restart"]["verified"] is False
    assert result["restart"]["functionally_verified"] is False


def test_admin_heal_explicit_restart_false_remains_start_only() -> None:
    start_calls: list[dict[str, object]] = []

    async def fake_start(_ctx: object, command: dict[str, object]) -> dict[str, object]:
        start_calls.append(command)
        return _fake_start_result()

    with tempfile.TemporaryDirectory() as tmp:
        with (
            patch.dict(os.environ, _configured_heal_env(tmp), clear=True),
            patch(
                "hermes_runtime.commands.heal_hermes.find_hermes_binary",
                return_value=Path("/usr/local/bin/hermes"),
            ),
            patch("hermes_runtime.commands.heal_hermes.start_hermes.run", fake_start),
        ):
            result = asyncio.run(
                run_command(
                    SimpleNamespace(),
                    {
                        "kind": "heal_hermes",
                        "spec": {
                            "reason": "admin_heal_hermes",
                            "restart": False,
                        },
                    },
                )
            )

    assert start_calls
    assert result["restart"]["requested"] is False
    assert result["restart"]["performed"] is False
    assert result["restart"]["compat_defaulted"] is False


def test_restart_fails_closed_when_service_owner_is_ambiguous() -> None:
    async def fail_official(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("ambiguous ownership must prevent restart")

    with tempfile.TemporaryDirectory() as tmp:
        with (
            patch.dict(os.environ, _configured_heal_env(tmp), clear=True),
            patch(
                "hermes_runtime.commands.heal_hermes.find_hermes_binary",
                return_value=Path("/usr/local/bin/hermes"),
            ),
            patch(
                "hermes_runtime.commands.heal_hermes.discover_gateway_service",
                return_value={
                    "ok": False,
                    "reason": "gateway_service_owner_ambiguous",
                    "owner": None,
                    "generation": None,
                },
            ),
            patch("hermes_runtime.commands.heal_hermes.run_process", fail_official),
        ):
            result = asyncio.run(
                run_command(
                    SimpleNamespace(),
                    {"kind": "heal_hermes", "spec": {"restart": True}},
                )
            )

    assert result["healthy"] is False
    assert result["healed"] is False
    assert result["reason"] == "gateway_service_owner_ambiguous"
    assert result["restart"]["requested"] is True
    assert result["restart"]["performed"] is False
    assert result["restart"]["generation"]["changed"] is False


def test_restart_fails_closed_when_service_probe_is_unavailable() -> None:
    async def fail_official(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("unavailable ownership probe must prevent restart")

    with tempfile.TemporaryDirectory() as tmp:
        with (
            patch.dict(os.environ, _configured_heal_env(tmp), clear=True),
            patch(
                "hermes_runtime.commands.heal_hermes.find_hermes_binary",
                return_value=Path("/usr/local/bin/hermes"),
            ),
            patch(
                "hermes_runtime.commands.heal_hermes.discover_gateway_service",
                return_value={
                    "ok": False,
                    "reason": "gateway_service_probe_unavailable",
                    "owner": None,
                    "generation": None,
                },
            ),
            patch("hermes_runtime.commands.heal_hermes.run_process", fail_official),
        ):
            result = asyncio.run(
                run_command(
                    SimpleNamespace(),
                    {"kind": "heal_hermes", "spec": {"restart": True}},
                )
            )

    assert result["healthy"] is False
    assert result["healed"] is False
    assert result["reason"] == "gateway_service_probe_unavailable"
    assert result["restart"]["requested"] is True
    assert result["restart"]["performed"] is False


def test_restart_reports_unproven_generation_instead_of_owner_found() -> None:
    before = _generation("old-invocation", 100, manager="user")
    unexpected = _generation("other-invocation", 200, manager="system")

    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: float,
        env: dict[str, str] | None = None,
        kill_process_group: bool = False,
    ) -> dict[str, object]:
        del timeout_seconds, env, kill_process_group
        return {
            "args": list(args),
            "returncode": 0,
            "ok": True,
            "timed_out": False,
            "duration_ms": 10,
            "stdout": "Gateway restarted\n",
            "stderr": "",
        }

    async def fake_snapshot(
        _owner: dict[str, str], *, timeout_seconds: float = 5
    ) -> dict[str, object]:
        del timeout_seconds
        return before

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
                "hermes_runtime.commands.heal_hermes.discover_gateway_service",
                side_effect=[_discovery(before), _discovery(unexpected)],
            ),
            patch(
                "hermes_runtime.commands.heal_hermes.snapshot_gateway_service",
                fake_snapshot,
            ),
        ):
            result = asyncio.run(
                run_command(
                    SimpleNamespace(),
                    {"kind": "heal_hermes", "spec": {"restart": True}},
                )
            )

    assert result["healthy"] is False
    assert result["healed"] is False
    assert result["reason"] == "gateway_generation_not_proven"
    assert result["restart"]["performed"] is True
    assert result["restart"]["fallback_attempted"] is False


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
