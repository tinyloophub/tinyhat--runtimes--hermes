"""Focused tests for :mod:`hermes_runtime.gateway_readiness`.

Usage (unittest, from repo root):
    python3 -m unittest tests.test_gateway_readiness -v
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
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


# --- _log_telegram_evidence -------------------------------------------------


def test_log_telegram_evidence_returns_none_when_marker_absent() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "hermes-gateway.log"
        log.write_text(
            "gateway starting up\nconnecting to telegram ...\n",
            encoding="utf-8",
        )
        # New bytes since offset 0, but none carry a positive marker: absence
        # is reported as unavailable (None), never as a negative (False).
        result = gateway_readiness._log_telegram_evidence(log, 0)
    assert result is None


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


def test_journal_telegram_evidence_returns_none_when_marker_absent() -> None:
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
        result = asyncio.run(gateway_readiness._journal_telegram_evidence(1000.0))
    assert result is None


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
            "stdout": "gateway running with 3 tools\nStarted polling\n",
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
        result = asyncio.run(gateway_readiness._journal_telegram_evidence(1000.0))
    assert result is True


def test_journal_telegram_evidence_returns_none_when_journalctl_missing() -> None:
    with patch(
        "hermes_runtime.gateway_readiness.shutil.which",
        return_value=None,
    ):
        result = asyncio.run(gateway_readiness._journal_telegram_evidence(1000.0))
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
    # No positive marker available -> connection evidence is unavailable, which
    # must NOT block readiness.
    assert result["telegram_connected"] is None
    assert result["telegram_evidence"] == "unavailable"
    assert result["ready"] is True


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
