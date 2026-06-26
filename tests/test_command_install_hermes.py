"""Focused tests for the ``install_hermes`` runtime command."""

from __future__ import annotations

import asyncio
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


def _raises_runtime(message: str) -> unittest._AssertRaisesContext[RuntimeError]:
    return unittest.TestCase().assertRaisesRegex(RuntimeError, message)


def _status(*, installed: bool = True, ok: bool = True) -> dict[str, object]:
    return {
        "schema": "tinyhat_hermes_status_v1",
        "installed": installed,
        "ok": ok,
        "hermes_bin": "/usr/local/bin/hermes",
        "version": "Hermes Agent 0.1.0",
        "commands": {},
    }


def test_install_hermes_is_noop_when_cli_exists() -> None:
    install_calls: list[str] = []

    async def fake_status() -> dict[str, object]:
        return _status()

    async def fake_run_shell(
        script: str,
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del timeout_seconds, env
        install_calls.append(script)
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}

    with (
        patch(
            "hermes_runtime.commands.install_hermes.find_hermes_binary",
            return_value=Path("/usr/local/bin/hermes"),
        ),
        patch(
            "hermes_runtime.commands.install_hermes.probe_hermes_status",
            fake_status,
        ),
        patch("hermes_runtime.commands.install_hermes.run_shell", fake_run_shell),
    ):
        result = asyncio.run(
            run_command(SimpleNamespace(), {"kind": "install_hermes"})
        )

    assert install_calls == []
    assert result["installed_before"] is True
    assert result["installed_now"] is False
    assert result["installed_after"] is True
    assert result["already_installed"] is True
    assert result["changed"] is False
    assert result["status"]["ok"] is True


def test_install_hermes_runs_official_installer_when_missing() -> None:
    install_calls: list[tuple[str, dict[str, str] | None]] = []

    async def fake_status() -> dict[str, object]:
        return _status()

    async def fake_prerequisites() -> dict[str, object]:
        return {"missing_before": ["curl", "git", "xz"], "attempted": True}

    async def fake_run_shell(
        script: str,
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del timeout_seconds
        install_calls.append((script, env))
        return {"ok": True, "returncode": 0, "stdout": "installed", "stderr": ""}

    with (
        patch(
            "hermes_runtime.commands.install_hermes.find_hermes_binary",
            return_value=None,
        ),
        patch(
            "hermes_runtime.commands.install_hermes.maybe_install_debian_prerequisites",
            fake_prerequisites,
        ),
        patch("hermes_runtime.commands.install_hermes.run_shell", fake_run_shell),
        patch(
            "hermes_runtime.commands.install_hermes.probe_hermes_status",
            fake_status,
        ),
    ):
        result = asyncio.run(
            run_command(SimpleNamespace(), {"kind": "install_hermes"})
        )

    assert len(install_calls) == 1
    script, env = install_calls[0]
    assert (
        "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- --skip-browser"
        in script
    )
    assert env == {"CI": "1"}
    assert result["installed_before"] is False
    assert result["installed_now"] is True
    assert result["installed_after"] is True
    assert result["already_installed"] is False
    assert result["changed"] is True
    assert result["prerequisites"]["attempted"] is True


def test_install_hermes_raises_when_installer_fails() -> None:
    async def fake_prerequisites() -> dict[str, object]:
        return {"missing_before": [], "attempted": False}

    async def fake_run_shell(
        script: str,
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del script, timeout_seconds, env
        return {"ok": False, "returncode": 1, "stdout": "", "stderr": "boom"}

    with (
        patch(
            "hermes_runtime.commands.install_hermes.find_hermes_binary",
            return_value=None,
        ),
        patch(
            "hermes_runtime.commands.install_hermes.maybe_install_debian_prerequisites",
            fake_prerequisites,
        ),
        patch("hermes_runtime.commands.install_hermes.run_shell", fake_run_shell),
    ):
        with _raises_runtime("Hermes installer failed"):
            asyncio.run(run_command(SimpleNamespace(), {"kind": "install_hermes"}))


def test_install_hermes_raises_when_cli_missing_after_install() -> None:
    async def fake_prerequisites() -> dict[str, object]:
        return {"missing_before": [], "attempted": False}

    async def fake_run_shell(
        script: str,
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del script, timeout_seconds, env
        return {"ok": True, "returncode": 0, "stdout": "installed", "stderr": ""}

    async def fake_status() -> dict[str, object]:
        return _status(installed=False, ok=False)

    with (
        patch(
            "hermes_runtime.commands.install_hermes.find_hermes_binary",
            return_value=None,
        ),
        patch(
            "hermes_runtime.commands.install_hermes.maybe_install_debian_prerequisites",
            fake_prerequisites,
        ),
        patch("hermes_runtime.commands.install_hermes.run_shell", fake_run_shell),
        patch(
            "hermes_runtime.commands.install_hermes.probe_hermes_status",
            fake_status,
        ),
    ):
        with _raises_runtime("hermes CLI was not found"):
            asyncio.run(run_command(SimpleNamespace(), {"kind": "install_hermes"}))


def test_install_hermes_raises_when_status_check_fails_after_install() -> None:
    async def fake_prerequisites() -> dict[str, object]:
        return {"missing_before": [], "attempted": False}

    async def fake_run_shell(
        script: str,
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del script, timeout_seconds, env
        return {"ok": True, "returncode": 0, "stdout": "installed", "stderr": ""}

    async def fake_status() -> dict[str, object]:
        return _status(installed=True, ok=False)

    with (
        patch(
            "hermes_runtime.commands.install_hermes.find_hermes_binary",
            return_value=None,
        ),
        patch(
            "hermes_runtime.commands.install_hermes.maybe_install_debian_prerequisites",
            fake_prerequisites,
        ),
        patch("hermes_runtime.commands.install_hermes.run_shell", fake_run_shell),
        patch(
            "hermes_runtime.commands.install_hermes.probe_hermes_status",
            fake_status,
        ),
    ):
        with _raises_runtime("status checks failed"):
            asyncio.run(run_command(SimpleNamespace(), {"kind": "install_hermes"}))
