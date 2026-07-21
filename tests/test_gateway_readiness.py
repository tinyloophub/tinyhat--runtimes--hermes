"""Focused tests for :mod:`hermes_runtime.gateway_readiness`.

Usage (unittest, from repo root):
    python3 -m unittest tests.test_gateway_readiness -v
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hermes_runtime.gateway_readiness as gateway_readiness  # noqa: E402


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


def _status_process(stdout: str, *, ok: bool = True) -> dict[str, object]:
    return {
        "args": ["/usr/local/bin/hermes", "gateway", "status"],
        "returncode": 0 if ok else 3,
        "ok": ok,
        "timed_out": False,
        "duration_ms": 8,
        "stdout": stdout,
        "stderr": "",
    }


def test_gateway_argv_normalizes_upstream_runtime_entrypoints_and_profiles() -> None:
    cases = (
        (
            ["python", "-m", "hermes_cli.main", "gateway", "run", "-p=work"],
            ["--profile", "work", "gateway", "run"],
        ),
        (
            ["hermes", "-p", "gateway", "gateway", "restart"],
            ["--profile", "gateway", "gateway", "restart"],
        ),
        (
            ["C:\\Hermes\\hermes-gateway.exe", "--profile", "work"],
            ["--profile", "work", "gateway", "run"],
        ),
        (
            ["python", "/opt/hermes/gateway/run.py", "--profile=work"],
            ["--profile", "work", "gateway", "run"],
        ),
        (["/usr/local/bin/hermes", "gateway"], ["gateway", "run"]),
    )

    for argv, expected in cases:
        assert gateway_readiness._gateway_argv_tail(argv) == expected


def test_gateway_argv_rejects_management_and_unrelated_gateway_commands() -> None:
    assert (
        gateway_readiness._gateway_argv_tail(["hermes", "gateway", "status"])
        is None
    )
    assert gateway_readiness._gateway_argv_tail(["other", "gateway", "run"]) is None


def test_gateway_argv_preserves_tinyhat_foreground_runtime_flags() -> None:
    runtime_argv = [
        "gateway",
        "run",
        "--replace",
        "--force",
        "--accept-hooks",
    ]
    assert gateway_readiness._gateway_argv_tail(
        ["/usr/local/bin/hermes", *runtime_argv]
    ) == runtime_argv


def test_public_gateway_generation_omits_arbitrary_argv_values() -> None:
    generation = {
        "pid": 42,
        "start_time": 1234,
        "started_at_unix": 1000.0,
        "argv": ["gateway", "run", "--token", "SECRET"],
    }

    public = gateway_readiness.public_gateway_runtime_generation(generation)

    assert public == {
        "pid": 42,
        "process_start_time": 1234,
        "started_at_unix": 1000.0,
        "command_kind": "gateway_run",
        "identity_verified": True,
    }
    assert "SECRET" not in repr(public)
    assert "argv" not in public
    assert gateway_readiness.public_gateway_runtime_generation_same(
        generation, public
    )


def test_public_foreground_generation_is_allowlisted_and_marks_match() -> None:
    generation = {
        "pid": 42,
        "process_start_time": 1234,
        "started_at_unix": 1000.0,
        "argv": ["gateway", "run", "--token", "SECRET"],
    }

    public = gateway_readiness.public_gateway_foreground_generation(
        generation,
        matches_runtime=True,
    )

    assert public == {
        "pid": 42,
        "process_start_time": 1234,
        "started_at_unix": 1000.0,
        "command_kind": "gateway_run",
        "identity_verified": True,
        "matches_runtime": True,
    }
    assert "SECRET" not in repr(public)


def test_public_gateway_generation_rejects_unrecognized_commands() -> None:
    assert (
        gateway_readiness.public_gateway_runtime_generation(
            {
                "pid": 42,
                "start_time": 1234,
                "argv": ["gateway", "status", "--token", "SECRET"],
            }
        )
        is None
    )


def test_hermes_process_start_time_uses_public_hermes_helper() -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(args: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((args, dict(kwargs["env"])))
        return SimpleNamespace(returncode=0, stdout="123400\n", stderr="")

    with (
        patch(
            "hermes_runtime.gateway_readiness.hermes_python",
            return_value=Path("/hermes/venv/bin/python"),
        ),
        patch("hermes_runtime.gateway_readiness.Path.is_file", return_value=True),
        patch("hermes_runtime.gateway_readiness.subprocess.run", fake_run),
        patch.dict(
            gateway_readiness.os.environ,
            {"PYTHONPATH": "unsafe", "PYTHONHOME": "unsafe"},
        ),
    ):
        start_time = gateway_readiness._hermes_process_start_time(42)

    assert start_time == 123400
    args, env = calls[0]
    assert args[0] == "/hermes/venv/bin/python"
    assert "from gateway.status import get_process_start_time" in args[2]
    assert args[-1] == "42"
    assert "PYTHONPATH" not in env
    assert "PYTHONHOME" not in env


def test_hermes_process_start_time_rejects_failed_or_invalid_helper_output() -> None:
    for returncode, stdout in ((1, "123400\n"), (0, "not-a-number\n")):
        with (
            patch(
                "hermes_runtime.gateway_readiness.hermes_python",
                return_value=Path("/hermes/venv/bin/python"),
            ),
            patch("hermes_runtime.gateway_readiness.Path.is_file", return_value=True),
            patch(
                "hermes_runtime.gateway_readiness.subprocess.run",
                return_value=SimpleNamespace(
                    returncode=returncode,
                    stdout=stdout,
                    stderr="",
                ),
            ),
        ):
            assert gateway_readiness._hermes_process_start_time(42) is None


def test_ps_process_argv_preserves_quoted_profile_value() -> None:
    with (
        patch(
            "hermes_runtime.gateway_readiness.shutil.which",
            return_value="/bin/ps",
        ),
        patch(
            "hermes_runtime.gateway_readiness.subprocess.run",
            return_value=SimpleNamespace(
                returncode=0,
                stdout='hermes --profile "work space" gateway run\n',
                stderr="",
            ),
        ),
    ):
        argv = gateway_readiness._read_ps_process_argv(42)

    assert argv == ["hermes", "--profile", "work space", "gateway", "run"]


def test_live_process_identity_uses_hermes_helper_without_proc() -> None:
    with (
        patch(
            "hermes_runtime.gateway_readiness._read_proc_start_time",
            return_value=None,
        ),
        patch(
            "hermes_runtime.commands.stop_hermes._read_proc_cmdline",
            return_value=None,
        ),
        patch.object(gateway_readiness.sys, "platform", "darwin"),
        patch(
            "hermes_runtime.gateway_readiness._hermes_process_start_time",
            return_value=123400,
        ),
        patch(
            "hermes_runtime.gateway_readiness._read_ps_process_argv",
            return_value=["hermes", "--profile", "work", "gateway", "run"],
        ),
    ):
        identity = gateway_readiness._live_process_identity(42)

    assert identity == (
        123400,
        ["hermes", "--profile", "work", "gateway", "run"],
        1234.0,
    )


def test_live_process_identity_retains_proc_ticks_when_ps_supplies_argv() -> None:
    with (
        patch(
            "hermes_runtime.gateway_readiness._read_proc_start_time",
            return_value=456,
        ),
        patch(
            "hermes_runtime.commands.stop_hermes._read_proc_cmdline",
            return_value=None,
        ),
        patch(
            "hermes_runtime.gateway_readiness._read_ps_process_argv",
            return_value=["hermes", "gateway", "run"],
        ),
        patch(
            "hermes_runtime.gateway_readiness.Path.read_text",
            return_value="100.0 0.0\n",
        ),
        patch("hermes_runtime.gateway_readiness.os.sysconf", return_value=100),
        patch("hermes_runtime.gateway_readiness.time.time", return_value=1000.0),
        patch(
            "hermes_runtime.gateway_readiness._hermes_process_start_time",
            side_effect=AssertionError("Linux /proc ticks must be retained"),
        ),
    ):
        identity = gateway_readiness._live_process_identity(42)

    assert identity == (456, ["hermes", "gateway", "run"], 904.56)


def test_live_process_identity_fails_closed_without_cross_platform_argv() -> None:
    with (
        patch(
            "hermes_runtime.gateway_readiness._read_proc_start_time",
            return_value=None,
        ),
        patch(
            "hermes_runtime.commands.stop_hermes._read_proc_cmdline",
            return_value=None,
        ),
        patch.object(gateway_readiness.sys, "platform", "darwin"),
        patch(
            "hermes_runtime.gateway_readiness._hermes_process_start_time",
            return_value=123400,
        ),
        patch(
            "hermes_runtime.gateway_readiness._read_ps_process_argv",
            return_value=None,
        ),
    ):
        identity = gateway_readiness._live_process_identity(42)

    assert identity is None


# --- _log_telegram_evidence -------------------------------------------------


def test_log_telegram_evidence_returns_false_when_fresh_marker_absent() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "hermes-gateway.log"
        log.write_text(
            "gateway starting up\nconnecting to telegram ...\n",
            encoding="utf-8",
        )
        # The source is usable and has fresh bytes, so an absent marker is a
        # real pending/not-ready observation rather than unavailable evidence.
        result = gateway_readiness._log_telegram_evidence(log, 0)
    assert result is False


def test_log_telegram_evidence_returns_true_when_marker_present() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "hermes-gateway.log"
        log.write_text(
            "gateway starting up\n[Telegram] Connected to Telegram (bot @dev)\n",
            encoding="utf-8",
        )
        result = gateway_readiness._log_telegram_evidence(log, 0)
    assert result is True


def test_log_telegram_evidence_returns_none_when_no_new_bytes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "hermes-gateway.log"
        log.write_text("Connected to Telegram\n", encoding="utf-8")
        size = log.stat().st_size
        # Offset at end-of-file: nothing appended since the restart began, so
        # the (possibly stale) marker above must not count.
        result = gateway_readiness._log_telegram_evidence(log, size)
    assert result is None


def test_log_telegram_evidence_returns_none_when_path_missing() -> None:
    assert gateway_readiness._log_telegram_evidence(None, 0) is None


# --- _journal_telegram_evidence --------------------------------------------


def test_journal_telegram_evidence_returns_false_when_marker_absent() -> None:
    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del args, timeout_seconds, env
        return {"ok": True, "stdout": "hermes gateway starting up\n", "stderr": ""}

    with (
        patch(
            "hermes_runtime.gateway_readiness.shutil.which",
            return_value="/usr/bin/journalctl",
        ),
        patch(
            "hermes_runtime.gateway_readiness.run_process",
            fake_run_process,
        ),
    ):
        result = asyncio.run(
            gateway_readiness._journal_telegram_evidence(
                1000.0,
                service_invocation_id="new-invocation",
            )
        )
    assert result is False


def test_journal_telegram_evidence_returns_true_when_marker_present() -> None:
    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del args, timeout_seconds, env
        return {
            "ok": True,
            "stdout": "[Telegram] Connected to Telegram (polling mode)\n",
            "stderr": "",
        }

    with (
        patch(
            "hermes_runtime.gateway_readiness.shutil.which",
            return_value="/usr/bin/journalctl",
        ),
        patch(
            "hermes_runtime.gateway_readiness.run_process",
            fake_run_process,
        ),
    ):
        result = asyncio.run(
            gateway_readiness._journal_telegram_evidence(
                1000.0,
                service_invocation_id="new-invocation",
            )
        )
    assert result is True


def test_journal_telegram_evidence_rejects_zero_connected_platforms() -> None:
    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del args, timeout_seconds, env
        return {
            "ok": True,
            "stdout": "Gateway running with 0 platform(s)\n",
            "stderr": "",
        }

    with (
        patch(
            "hermes_runtime.gateway_readiness.shutil.which",
            return_value="/usr/bin/journalctl",
        ),
        patch(
            "hermes_runtime.gateway_readiness.run_process",
            fake_run_process,
        ),
    ):
        result = asyncio.run(
            gateway_readiness._journal_telegram_evidence(
                1000.0,
                service_invocation_id="new-invocation",
            )
        )
    assert result is False


def test_journal_telegram_evidence_returns_none_when_journalctl_missing() -> None:
    with patch(
        "hermes_runtime.gateway_readiness.shutil.which",
        return_value=None,
    ):
        result = asyncio.run(
            gateway_readiness._journal_telegram_evidence(
                1000.0,
                service_invocation_id="new-invocation",
            )
        )
    assert result is None


# --- _runtime_state_telegram_evidence --------------------------------------


def _write_runtime_state(
    path: Path,
    *,
    pid: int = 123,
    gateway_state: str = "running",
    telegram_state: str = "connected",
    updated_at: str = "1970-01-01T00:20:00+00:00",
    telegram_updated_at: str = "1970-01-01T00:20:00+00:00",
    kind: str = "hermes-gateway",
    start_time: int = 456,
    argv: list[str] | None = None,
) -> None:
    path.write_text(
        json.dumps(
            {
                "pid": pid,
                "kind": kind,
                "start_time": start_time,
                "argv": argv or ["hermes", "gateway", "run"],
                "gateway_state": gateway_state,
                "updated_at": updated_at,
                "platforms": {
                    "telegram": {
                        "state": telegram_state,
                        "updated_at": telegram_updated_at,
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def test_runtime_generation_requires_live_kind_start_time_and_argv() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "profiles" / "work" / "gateway_state.json"
        state.parent.mkdir(parents=True)
        _write_runtime_state(
            state,
            pid=123,
            start_time=456,
            argv=["hermes", "-p", "work", "gateway", "run"],
        )
        with patch(
            "hermes_runtime.gateway_readiness._live_process_identity",
            return_value=(
                456,
                ["python", "hermes", "-p", "work", "gateway", "run"],
                1000.25,
            ),
        ):
            generation = gateway_readiness.read_gateway_runtime_generation(state)

    assert generation == {
        "pid": 123,
        "start_time": 456,
        "argv": ["--profile", "work", "gateway", "run"],
        "started_at_unix": 1000.25,
    }


def test_runtime_generation_rejects_profile_that_is_not_current_home() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        default_state = Path(tmp) / "gateway_state.json"
        _write_runtime_state(
            default_state,
            pid=123,
            start_time=456,
            argv=["hermes", "--profile", "work", "gateway", "run"],
        )
        named_state = Path(tmp) / "profiles" / "work" / "gateway_state.json"
        named_state.parent.mkdir(parents=True)
        _write_runtime_state(
            named_state,
            pid=123,
            start_time=456,
            argv=["hermes", "gateway", "run"],
        )
        with patch(
            "hermes_runtime.gateway_readiness._live_process_identity",
            return_value=(456, ["hermes", "gateway", "run"], 1000.25),
        ):
            wrong_default = (
                gateway_readiness.read_gateway_runtime_generation(default_state)
            )
            missing_named_profile = (
                gateway_readiness.read_gateway_runtime_generation(named_state)
            )

    assert wrong_default is None
    assert missing_named_profile is None


def test_runtime_generation_accepts_named_profile_explicit_home() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile_home = Path(tmp) / "profiles" / "work"
        profile_home.mkdir(parents=True)
        state = profile_home / "gateway_state.json"
        argv = [f"HERMES_HOME={profile_home}", "hermes", "gateway", "run"]
        _write_runtime_state(
            state,
            pid=123,
            start_time=456,
            argv=argv,
        )
        with patch(
            "hermes_runtime.gateway_readiness._live_process_identity",
            return_value=(456, argv, 1000.25),
        ):
            generation = gateway_readiness.read_gateway_runtime_generation(state)

    assert generation is not None
    assert generation["argv"] == [
        "HERMES_HOME",
        str(profile_home),
        "gateway",
        "run",
    ]


def test_gateway_argv_home_binding_rejects_conflicting_explicit_home() -> None:
    default_home = Path("/srv/hermes")
    named_home = default_home / "profiles" / "work"

    assert gateway_readiness._gateway_argv_belongs_to_home(
        ["gateway", "run"], default_home
    )
    assert gateway_readiness._gateway_argv_belongs_to_home(
        ["HERMES_HOME", str(default_home), "gateway", "run"],
        default_home,
    )
    assert not gateway_readiness._gateway_argv_belongs_to_home(
        ["HERMES_HOME", "/srv/other", "gateway", "run"],
        default_home,
    )
    if os.name != "nt":
        assert not gateway_readiness._gateway_argv_belongs_to_home(
            ["HERMES_HOME", "/srv/Hermes", "gateway", "run"],
            default_home,
        )
    assert not gateway_readiness._gateway_argv_belongs_to_home(
        ["gateway", "run", "--profile", "work"], default_home
    )
    assert gateway_readiness._gateway_argv_belongs_to_home(
        ["gateway", "run", "--profile", "work"], named_home
    )
    assert not gateway_readiness._gateway_argv_belongs_to_home(
        ["gateway", "run", "--profile", "personal"], named_home
    )


def test_runtime_generation_rejects_conflicting_profile_selectors() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile_home = Path(tmp) / "profiles" / "work"
        profile_home.mkdir(parents=True)
        state = profile_home / "gateway_state.json"
        argv = [
            "hermes",
            "--profile",
            "personal",
            "--profile",
            "work",
            "gateway",
            "run",
        ]
        _write_runtime_state(
            state,
            pid=123,
            start_time=456,
            argv=argv,
        )
        with patch(
            "hermes_runtime.gateway_readiness._live_process_identity",
            return_value=(456, argv, 1000.25),
        ):
            generation = gateway_readiness.read_gateway_runtime_generation(state)

    assert generation is None


def test_runtime_generation_rejects_pid_reuse_and_profile_argv_mismatch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "gateway_state.json"
        _write_runtime_state(
            state,
            pid=123,
            start_time=456,
            argv=["hermes", "gateway", "run", "--profile", "work"],
        )
        with patch(
            "hermes_runtime.gateway_readiness._live_process_identity",
            return_value=(
                999,
                ["hermes", "gateway", "run", "--profile", "work"],
                1000.25,
            ),
        ):
            reused = gateway_readiness.read_gateway_runtime_generation(state)
        with patch(
            "hermes_runtime.gateway_readiness._live_process_identity",
            return_value=(
                456,
                ["hermes", "gateway", "run", "--profile", "personal"],
                1000.25,
            ),
        ):
            wrong_profile = gateway_readiness.read_gateway_runtime_generation(
                state
            )

    assert reused is None
    assert wrong_profile is None


def test_runtime_generation_identity_ignores_unix_boundary_recalculation() -> None:
    first = {
        "pid": 123,
        "start_time": 456,
        "argv": ["gateway", "run"],
        "started_at_unix": 1000.1,
    }
    second = {**first, "started_at_unix": 1000.9}
    reused = {**second, "start_time": 999}

    assert gateway_readiness.gateway_runtime_generation_same(first, second)
    assert not gateway_readiness.gateway_runtime_generation_same(first, reused)


def test_runtime_generation_active_distinguishes_match_reuse_and_ambiguity() -> None:
    generation = {
        "pid": 123,
        "start_time": 456,
        "argv": ["gateway", "run"],
        "started_at_unix": 1000.1,
    }
    home = Path("/tmp/hermes-home")
    with (
        patch("hermes_runtime.gateway_readiness.os.kill"),
        patch(
            "hermes_runtime.gateway_readiness._live_process_identity",
            return_value=(456, ["hermes", "gateway", "run"], 1000.1),
        ),
    ):
        active = gateway_readiness.gateway_runtime_generation_active(
            generation, expected_home=home
        )
    with (
        patch("hermes_runtime.gateway_readiness.os.kill"),
        patch(
            "hermes_runtime.gateway_readiness._live_process_identity",
            return_value=(999, ["hermes", "gateway", "run"], 1000.1),
        ),
    ):
        reused = gateway_readiness.gateway_runtime_generation_active(
            generation, expected_home=home
        )
    with (
        patch("hermes_runtime.gateway_readiness.os.kill"),
        patch(
            "hermes_runtime.gateway_readiness._live_process_identity",
            return_value=None,
        ),
    ):
        unreadable = gateway_readiness.gateway_runtime_generation_active(
            generation, expected_home=home
        )

    assert active is True
    assert reused is False
    assert unreadable is None


def test_runtime_generation_active_treats_exact_linux_zombie_as_exited() -> None:
    generation = {
        "pid": 123,
        "start_time": 456,
        "argv": ["gateway", "run"],
        "started_at_unix": 1000.1,
    }
    with (
        patch("hermes_runtime.gateway_readiness.os.kill"),
        patch.object(gateway_readiness.sys, "platform", "linux"),
        patch(
            "hermes_runtime.gateway_readiness._read_proc_state_and_start_time",
            return_value=("Z", 456),
        ),
        patch(
            "hermes_runtime.gateway_readiness._live_process_identity",
            side_effect=AssertionError("a proven zombie is already exited"),
        ),
    ):
        active = gateway_readiness.gateway_runtime_generation_active(
            generation,
            expected_home=Path("/tmp/hermes-home"),
        )

    assert active is False


def test_runtime_generation_active_rejects_reused_zombie_pid() -> None:
    generation = {
        "pid": 123,
        "start_time": 456,
        "argv": ["gateway", "run"],
        "started_at_unix": 1000.1,
    }
    with (
        patch("hermes_runtime.gateway_readiness.os.kill"),
        patch.object(gateway_readiness.sys, "platform", "linux"),
        patch(
            "hermes_runtime.gateway_readiness._read_proc_state_and_start_time",
            return_value=("Z", 999),
        ),
        patch(
            "hermes_runtime.gateway_readiness._live_process_identity",
            side_effect=AssertionError("PID reuse is decided from /proc"),
        ),
    ):
        active = gateway_readiness.gateway_runtime_generation_active(
            generation,
            expected_home=Path("/tmp/hermes-home"),
        )

    assert active is False


def test_runtime_state_evidence_accepts_new_connected_gateway_pid() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "gateway_state.json"
        _write_runtime_state(state)
        result = gateway_readiness._runtime_state_telegram_evidence(
            state,
            service_main_pid=123,
            since_unix=1000.0,
        )
    assert result is True


def test_runtime_state_evidence_requires_foreground_identity_fields() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "gateway_state.json"
        _write_runtime_state(state, start_time=456)
        accepted = gateway_readiness._runtime_state_telegram_evidence(
            state,
            service_main_pid=123,
            since_unix=1000.0,
            expected_start_time=456,
            expected_argv=["gateway", "run"],
        )
        rejected = gateway_readiness._runtime_state_telegram_evidence(
            state,
            service_main_pid=123,
            since_unix=1000.0,
            expected_start_time=999,
            expected_argv=["gateway", "run"],
        )

    assert accepted is True
    assert rejected is None


def test_runtime_state_evidence_rejects_stale_platform_state() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "gateway_state.json"
        _write_runtime_state(
            state,
            telegram_updated_at="1970-01-01T00:15:00+00:00",
        )
        result = gateway_readiness._runtime_state_telegram_evidence(
            state,
            service_main_pid=123,
            since_unix=1000.0,
        )
    assert result is False


def test_runtime_state_evidence_rejects_inherited_connected_row_while_starting() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "gateway_state.json"
        _write_runtime_state(state, gateway_state="starting")
        result = gateway_readiness._runtime_state_telegram_evidence(
            state,
            service_main_pid=123,
            since_unix=1000.0,
        )
    assert result is False


def test_runtime_state_evidence_rejects_retrying_telegram() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "gateway_state.json"
        _write_runtime_state(state, telegram_state="retrying")
        result = gateway_readiness._runtime_state_telegram_evidence(
            state,
            service_main_pid=123,
            since_unix=1000.0,
        )
    assert result is False


def test_runtime_state_evidence_rejects_missing_telegram_row() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "gateway_state.json"
        state.write_text(
            json.dumps(
                {
                    "pid": 123,
                    "gateway_state": "running",
                    "updated_at": "1970-01-01T00:20:00+00:00",
                    "platforms": {},
                }
            ),
            encoding="utf-8",
        )
        result = gateway_readiness._runtime_state_telegram_evidence(
            state,
            service_main_pid=123,
            since_unix=1000.0,
        )
    assert result is False


def test_runtime_state_evidence_ignores_malformed_json() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "gateway_state.json"
        state.write_text("{not-json", encoding="utf-8")
        result = gateway_readiness._runtime_state_telegram_evidence(
            state,
            service_main_pid=123,
            since_unix=1000.0,
        )
    assert result is None


def test_runtime_state_evidence_ignores_a_different_service_pid() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "gateway_state.json"
        _write_runtime_state(state, pid=122)
        result = gateway_readiness._runtime_state_telegram_evidence(
            state,
            service_main_pid=123,
            since_unix=1000.0,
        )
    assert result is None


# --- probe_functional_readiness --------------------------------------------


def test_probe_ready_when_status_healthy_and_no_positive_telegram_marker() -> None:
    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del timeout_seconds, env
        # Only the status probe should reach run_process here (journalctl is
        # patched away below), and it reports a healthy, active unit.
        return _status_process("     Active: active (running)\n")

    with patch(
        "hermes_runtime.gateway_readiness.shutil.which",
        return_value=None,
    ):
        with patch(
            "hermes_runtime.gateway_readiness.run_process",
            fake_run_process,
        ):
            result = asyncio.run(
                gateway_readiness.probe_functional_readiness(
                    Path("/usr/local/bin/hermes"),
                    since_unix=1000.0,
                    log_path=None,
                    log_offset=0,
                )
            )

    assert result["status_healthy"] is True
    # No positive marker available -> connection evidence is unavailable, so
    # functional readiness remains unverified.
    assert result["telegram_connected"] is None
    assert result["telegram_evidence"] == "unavailable"
    assert result["ready"] is False
    assert result["functionally_ready"] is False


def test_probe_not_ready_while_fresh_journal_has_no_telegram_marker() -> None:
    calls: list[list[str]] = []

    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: float,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del timeout_seconds, env
        calls.append(list(args))
        if args[-1] == "status":
            return _status_process("     Active: active (running)\n")
        return {"ok": True, "stdout": "Connecting to Telegram ...\n", "stderr": ""}

    with (
        patch(
            "hermes_runtime.gateway_readiness.shutil.which",
            return_value="/usr/bin/journalctl",
        ),
        patch(
            "hermes_runtime.gateway_readiness.run_process",
            fake_run_process,
        ),
    ):
        result = asyncio.run(
            gateway_readiness.probe_functional_readiness(
                Path("/usr/local/bin/hermes"),
                since_unix=1000.0,
                service_manager="system",
                service_invocation_id="new-invocation",
            )
        )

    assert result["status_healthy"] is True
    assert result["telegram_connected"] is False
    assert result["telegram_evidence"] == "journal"
    assert result["ready"] is False
    assert result["functionally_ready"] is False
    journal_call = calls[-1]
    assert "--user" not in journal_call
    assert "_SYSTEMD_INVOCATION_ID=new-invocation" in journal_call


def test_probe_ready_from_new_gateway_runtime_state_without_info_logs() -> None:
    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: float,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del timeout_seconds, env
        assert args[-1] == "status", "runtime state should avoid journal fallback"
        return _status_process("     Active: active (running)\n")

    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "gateway_state.json"
        _write_runtime_state(state)
        with patch(
            "hermes_runtime.gateway_readiness.run_process",
            fake_run_process,
        ):
            result = asyncio.run(
                gateway_readiness.probe_functional_readiness(
                    Path("/usr/local/bin/hermes"),
                    since_unix=1000.0,
                    service_manager="user",
                    service_invocation_id="new-invocation",
                    service_main_pid=123,
                    runtime_state_path=state,
                )
            )

    assert result["status_healthy"] is True
    assert result["telegram_connected"] is True
    assert result["telegram_evidence"] == "runtime_state"
    assert result["functionally_ready"] is True


def test_probe_foreground_requires_exact_runtime_state_identity() -> None:
    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: float,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del timeout_seconds, env
        assert args[-1] == "status"
        return _status_process("     Active: active (running)\n")

    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "gateway_state.json"
        _write_runtime_state(state, start_time=456)
        with patch(
            "hermes_runtime.gateway_readiness.run_process",
            fake_run_process,
        ):
            result = asyncio.run(
                gateway_readiness.probe_functional_readiness(
                    Path("/usr/local/bin/hermes"),
                    since_unix=1000.0,
                    service_main_pid=123,
                    expected_process_start_time=456,
                    expected_gateway_argv=["gateway", "run"],
                    runtime_state_path=state,
                )
            )

    assert result["status_healthy"] is True
    assert result["telegram_connected"] is True
    assert result["telegram_evidence"] == "runtime_state"
    assert result["functionally_ready"] is True


def test_probe_service_invocation_ignores_unscoped_log_marker() -> None:
    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: float,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del timeout_seconds, env
        if args[-1] == "status":
            return _status_process("     Active: active (running)\n")
        return {"ok": True, "stdout": "gateway starting\n", "stderr": ""}

    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "hermes-gateway.log"
        log.write_text("[Telegram] Connected to Telegram\n", encoding="utf-8")
        with (
            patch(
                "hermes_runtime.gateway_readiness.shutil.which",
                return_value="/usr/bin/journalctl",
            ),
            patch(
                "hermes_runtime.gateway_readiness.run_process",
                fake_run_process,
            ),
        ):
            result = asyncio.run(
                gateway_readiness.probe_functional_readiness(
                    Path("/usr/local/bin/hermes"),
                    since_unix=1000.0,
                    log_path=log,
                    service_invocation_id="new-invocation",
                )
            )

    assert result["telegram_evidence"] == "journal"
    assert result["telegram_connected"] is False
    assert result["functionally_ready"] is False


def test_probe_not_ready_when_status_unhealthy() -> None:
    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del timeout_seconds, env
        return _status_process("     Active: inactive (dead)\n")

    with patch(
        "hermes_runtime.gateway_readiness.shutil.which",
        return_value=None,
    ):
        with patch(
            "hermes_runtime.gateway_readiness.run_process",
            fake_run_process,
        ):
            result = asyncio.run(
                gateway_readiness.probe_functional_readiness(
                    Path("/usr/local/bin/hermes"),
                    since_unix=1000.0,
                    log_path=None,
                    log_offset=0,
                )
            )

    assert result["status_healthy"] is False
    assert result["ready"] is False


def test_probe_rejects_fatal_telegram_status_even_with_old_connect_marker() -> None:
    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: float,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del timeout_seconds, env
        if args[-1] == "status":
            return _status_process(
                "Active: active (running)\n"
                "Recent gateway health:\n"
                "  ⚠ telegram: polling task stopped\n"
            )
        return {
            "ok": True,
            "stdout": "[Telegram] Connected to Telegram\n",
            "stderr": "",
        }

    with (
        patch(
            "hermes_runtime.gateway_readiness.shutil.which",
            return_value="/usr/bin/journalctl",
        ),
        patch(
            "hermes_runtime.gateway_readiness.run_process",
            fake_run_process,
        ),
    ):
        result = asyncio.run(
            gateway_readiness.probe_functional_readiness(
                Path("/usr/local/bin/hermes"),
                since_unix=0,
                service_invocation_id="current-invocation",
            )
        )

    assert result["telegram_fatal"] is True
    assert result["status_healthy"] is False
    assert result["telegram_connected"] is True
    assert result["functionally_ready"] is False
    assert result["ready"] is False


def test_probe_ready_when_positive_log_marker_present() -> None:
    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del timeout_seconds, env
        return _status_process("     Active: active (running)\n")

    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "hermes-gateway.log"
        log.write_text("[Telegram] Connected to Telegram\n", encoding="utf-8")
        with patch(
            "hermes_runtime.gateway_readiness.shutil.which",
            return_value=None,
        ):
            with patch(
                "hermes_runtime.gateway_readiness.run_process",
                fake_run_process,
            ):
                result = asyncio.run(
                    gateway_readiness.probe_functional_readiness(
                        Path("/usr/local/bin/hermes"),
                        since_unix=1000.0,
                        log_path=log,
                        log_offset=0,
                    )
                )

    assert result["status_healthy"] is True
    assert result["telegram_connected"] is True
    assert result["telegram_evidence"] == "log"
    assert result["ready"] is True


if __name__ == "__main__":
    unittest.main()
