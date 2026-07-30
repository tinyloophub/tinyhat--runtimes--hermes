"""Focused tests for the ``install_hermes`` runtime command."""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hermes_runtime.commands import install_hermes, run_command  # noqa: E402


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


async def _fake_local_stt_model_prefetch() -> dict[str, object]:
    return {
        "ok": True,
        "changed": True,
        "skipped": False,
        "model": "small",
        "project_dir": "/usr/local/lib/hermes-agent",
    }


async def _fake_day_one_capabilities(_hermes_bin: Path) -> dict[str, object]:
    return {
        "ok": True,
        "baseline": {"ok": True},
        "multimedia": {"ok": True},
        "commands": [
            {
                "key": "web.search_backend",
                "value": "ddgs",
                "ok": True,
            },
            {
                "key": "browser.cloud_provider",
                "value": "local",
                "ok": True,
            },
        ],
    }


async def _fake_browser_smoke() -> dict[str, object]:
    return {
        "ok": True,
        "smoke_status": "passed",
        "browser_bin": "/root/.hermes/node/bin/agent-browser",
    }


def test_pip_command_prefers_venv_pip_when_available() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        python_bin = Path(tmp) / "venv" / "bin" / "python"
        python_bin.parent.mkdir(parents=True)
        python_bin.write_text("", encoding="utf-8")
        (python_bin.parent / "pip").write_text("", encoding="utf-8")

        with patch(
            "hermes_runtime.commands.install_hermes.shutil.which",
            return_value="/usr/bin/pip",
        ):
            command = install_hermes._pip_command_for_python(python_bin)

    assert command == f"{python_bin} -m pip"


def test_pip_command_uses_system_pip_python_when_venv_lacks_pip() -> None:
    python_bin = Path("/opt/hermes/venv/bin/python")

    with (
        patch(
            "hermes_runtime.commands.install_hermes.shutil.which",
            return_value="/usr/bin/pip",
        ),
        patch(
            "hermes_runtime.commands.install_hermes._pip_supports_python_option",
            return_value=True,
        ),
    ):
        command = install_hermes._pip_command_for_python(python_bin)

    assert command == "/usr/bin/pip --python /opt/hermes/venv/bin/python"


def test_ensure_messaging_dependencies_installs_project_extra() -> None:
    process_calls: list[list[str]] = []
    shell_calls: list[tuple[str, dict[str, str] | None]] = []
    probe_results = [
        {"ok": False, "returncode": 1, "stdout": "missing:telegram", "stderr": ""},
        {"ok": True, "returncode": 0, "stdout": "ok", "stderr": ""},
    ]

    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: int,
    ) -> dict[str, object]:
        del timeout_seconds
        process_calls.append(args)
        return probe_results.pop(0)

    async def fake_run_shell(
        script: str,
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del timeout_seconds
        shell_calls.append((script, env))
        return {"ok": True, "returncode": 0, "stdout": "installed", "stderr": ""}

    with tempfile.TemporaryDirectory() as tmp:
        project_dir = Path(tmp) / "hermes-agent"
        python_bin = project_dir / "venv" / "bin" / "python"
        python_bin.parent.mkdir(parents=True)
        python_bin.write_text("", encoding="utf-8")
        (python_bin.parent / "pip").write_text("", encoding="utf-8")
        (project_dir / "pyproject.toml").write_text(
            "[project]\nname='hermes-agent'\n",
            encoding="utf-8",
        )

        with (
            patch.dict(os.environ, {"HERMES_PROJECT_DIR": str(project_dir)}),
            patch(
                "hermes_runtime.commands.install_hermes.shutil.which",
                return_value="/usr/bin/pip",
            ),
            patch(
                "hermes_runtime.commands.install_hermes.run_process",
                fake_run_process,
            ),
            patch("hermes_runtime.commands.install_hermes.run_shell", fake_run_shell),
        ):
            result = asyncio.run(install_hermes._ensure_messaging_dependencies())

    assert result["ok"] is True
    assert result["changed"] is True
    assert len(process_calls) == 2
    assert len(shell_calls) == 1
    script, env = shell_calls[0]
    assert f"cd {project_dir}" in script
    package_spec = shlex.quote(f"{project_dir}[messaging,voice]")
    assert f"{python_bin} -m pip install -e {package_spec}" in script
    assert f"{python_bin} -m pip install ddgs==9.14.4 edge-tts==7.2.7" in script
    assert "--python" not in script
    assert env == {"PIP_DISABLE_PIP_VERSION_CHECK": "1"}


def test_browser_smoke_opens_snapshots_and_closes_blank_page() -> None:
    calls: list[tuple[list[str], int]] = []

    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: int,
    ) -> dict[str, object]:
        calls.append((args, timeout_seconds))
        return {
            "args": args,
            "returncode": 0,
            "ok": True,
            "timed_out": False,
            "stdout": "ok",
            "stderr": "",
        }

    browser_bin = Path("/opt/hermes/bin/agent-browser")
    with (
        patch(
            "hermes_runtime.commands.install_hermes._agent_browser_binary",
            return_value=browser_bin,
        ),
        patch(
            "hermes_runtime.commands.install_hermes.run_process",
            fake_run_process,
        ),
    ):
        result = asyncio.run(install_hermes._probe_browser_automation())

    assert calls == [
        (
            [
                str(browser_bin),
                "--session",
                "tinyhat-provisioning-smoke",
                "open",
                "about:blank",
            ],
            120,
        ),
        (
            [
                str(browser_bin),
                "--session",
                "tinyhat-provisioning-smoke",
                "snapshot",
            ],
            60,
        ),
        (
            [
                str(browser_bin),
                "--session",
                "tinyhat-provisioning-smoke",
                "close",
            ],
            30,
        ),
    ]
    assert result["ok"] is True
    assert result["smoke_status"] == "passed"


def test_x86_browser_fallback_installs_managed_chrome_for_testing() -> None:
    calls: list[tuple[list[str], int]] = []

    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: int,
    ) -> dict[str, object]:
        calls.append((args, timeout_seconds))
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}

    browser_bin = Path("/opt/hermes/bin/agent-browser")
    with (
        patch(
            "hermes_runtime.commands.install_hermes.platform.system",
            return_value="Linux",
        ),
        patch(
            "hermes_runtime.commands.install_hermes.platform.machine",
            return_value="x86_64",
        ),
        patch(
            "hermes_runtime.commands.install_hermes._agent_browser_binary",
            return_value=browser_bin,
        ),
        patch(
            "hermes_runtime.commands.install_hermes.run_process",
            fake_run_process,
        ),
    ):
        result = asyncio.run(
            install_hermes._install_managed_browser_fallback()
        )

    assert result["attempted"] is True
    assert result["reason"] == "managed_chrome_for_testing_missing"
    assert result["browser_bin"] == str(browser_bin)
    assert calls == [([str(browser_bin), "install", "--with-deps"], 900)]


def test_managed_browser_fallback_skips_arm() -> None:
    with (
        patch(
            "hermes_runtime.commands.install_hermes.platform.system",
            return_value="Linux",
        ),
        patch(
            "hermes_runtime.commands.install_hermes.platform.machine",
            return_value="aarch64",
        ),
        patch(
            "hermes_runtime.commands.install_hermes.run_process",
            side_effect=AssertionError("managed browser install should not run"),
        ),
    ):
        result = asyncio.run(
            install_hermes._install_managed_browser_fallback()
        )

    assert result == {
        "attempted": False,
        "architecture": "aarch64",
        "reason": "managed_browser_not_applicable",
        "result": None,
    }


def test_arm_browser_fallback_installs_distro_chromium_after_probe_failure() -> None:
    calls: list[tuple[str, int]] = []

    async def fake_run_shell(
        script: str,
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del env
        calls.append((script, timeout_seconds))
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}

    with (
        patch(
            "hermes_runtime.commands.install_hermes.platform.machine",
            return_value="aarch64",
        ),
        patch.object(install_hermes.os, "name", "posix"),
        patch(
            "hermes_runtime.commands.install_hermes.os.geteuid",
            return_value=0,
        ),
        patch(
            "hermes_runtime.commands.install_hermes.shutil.which",
            return_value="/usr/bin/apt-get",
        ),
        patch(
            "hermes_runtime.commands.install_hermes.run_shell",
            fake_run_shell,
        ),
    ):
        result = asyncio.run(
            install_hermes._install_arm_system_browser_fallback()
        )

    assert result["attempted"] is True
    assert result["reason"] == "arm_playwright_browser_unavailable"
    assert calls == [
        (
            "export DEBIAN_FRONTEND=noninteractive\n"
            "apt-get update\n"
            "apt-get install -y --no-install-recommends chromium",
            600,
        )
    ]


def test_x86_browser_failure_does_not_install_ubuntu_snap_chromium() -> None:
    with (
        patch(
            "hermes_runtime.commands.install_hermes.platform.machine",
            return_value="x86_64",
        ),
        patch(
            "hermes_runtime.commands.install_hermes.run_shell",
            side_effect=AssertionError("system browser install should not run"),
        ),
    ):
        result = asyncio.run(
            install_hermes._install_arm_system_browser_fallback()
        )

    assert result == {
        "attempted": False,
        "architecture": "x86_64",
        "reason": "playwright_browser_preferred",
        "result": None,
    }


def test_browser_failure_summary_reports_the_failing_step() -> None:
    result = install_hermes._browser_failure_summary(
        {
            "open": {
                "ok": False,
                "returncode": None,
                "timed_out": True,
                "stdout": "",
                "stderr": "command timed out after 120s\nmore detail",
            },
            "snapshot": None,
        }
    )

    assert result == "open: command timed out after 120s"


def test_agent_browser_binary_uses_upstream_project_install() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project_dir = Path(tmp) / "hermes-agent"
        (project_dir / "venv" / "bin").mkdir(parents=True)
        (project_dir / "venv" / "bin" / "python").write_text("", encoding="utf-8")
        (project_dir / "pyproject.toml").write_text(
            "[project]\nname='hermes-agent'\n",
            encoding="utf-8",
        )
        browser_bin = project_dir / "node_modules" / ".bin" / "agent-browser"
        browser_bin.parent.mkdir(parents=True)
        browser_bin.write_text("#!/bin/sh\n", encoding="utf-8")
        browser_bin.chmod(0o755)

        with (
            patch.dict(
                os.environ,
                {"HERMES_PROJECT_DIR": str(project_dir)},
                clear=False,
            ),
            patch(
                "hermes_runtime.commands.install_hermes.shutil.which",
                return_value=None,
            ),
        ):
            result = install_hermes._agent_browser_binary()

    assert result == browser_bin


def test_install_hermes_is_noop_when_cli_exists() -> None:
    install_calls: list[str] = []

    async def fake_status(*, timeout_seconds: int = 30) -> dict[str, object]:
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

    async def fake_messaging() -> dict[str, object]:
        return {"ok": True, "changed": False}

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
        patch(
            "hermes_runtime.commands.install_hermes._ensure_messaging_dependencies",
            fake_messaging,
        ),
        patch(
            "hermes_runtime.commands.install_hermes._prefetch_local_stt_model",
            _fake_local_stt_model_prefetch,
        ),
        patch(
            "hermes_runtime.commands.install_hermes._configure_day_one_capabilities",
            _fake_day_one_capabilities,
        ),
        patch(
            "hermes_runtime.commands.install_hermes._probe_browser_automation",
            _fake_browser_smoke,
        ),
        patch(
            "hermes_runtime.commands.install_hermes._install_codex_auth_quick_commands",
            return_value={"installed": True, "commands": ["codex_auth"]},
        ),
        patch(
            "hermes_runtime.commands.install_hermes._install_codex_auth_plugin_commands",
            return_value={"installed": True, "commands": ["codex_auth"]},
        ),
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
    assert result["messaging"]["ok"] is True
    assert result["messaging"]["changed"] is False
    assert result["multimodal_defaults"]["ok"] is True
    assert result["local_stt_model_prefetch"]["model"] == "small"
    assert result["local_stt_model_prefetch_warning"] is None
    assert result["codex_auth"]["quick_commands"]["installed"] is True
    assert result["codex_auth"]["plugin_commands"]["installed"] is True
    assert result["status"]["ok"] is True


def test_install_hermes_retries_browser_smoke_after_fallback() -> None:
    browser_probes = [
        {
            "ok": False,
            "smoke_status": "failed",
            "open": {
                "ok": False,
                "returncode": 1,
                "stdout": "",
                "stderr": "browser missing",
            },
        },
        {
            "ok": True,
            "smoke_status": "passed",
            "browser_bin": "/usr/bin/agent-browser",
        },
    ]
    fallback_calls = 0

    async def fake_status(*, timeout_seconds: int = 30) -> dict[str, object]:
        del timeout_seconds
        return _status()

    async def fake_messaging() -> dict[str, object]:
        return {"ok": True, "changed": False}

    async def fake_browser_probe() -> dict[str, object]:
        return browser_probes.pop(0)

    async def fake_browser_fallback() -> dict[str, object]:
        nonlocal fallback_calls
        fallback_calls += 1
        return {
            "attempted": True,
            "architecture": "x86_64",
            "reason": "managed_chrome_for_testing_missing",
            "result": {"ok": True},
        }

    with (
        patch(
            "hermes_runtime.commands.install_hermes.find_hermes_binary",
            return_value=Path("/usr/local/bin/hermes"),
        ),
        patch(
            "hermes_runtime.commands.install_hermes.probe_hermes_status",
            fake_status,
        ),
        patch(
            "hermes_runtime.commands.install_hermes._ensure_messaging_dependencies",
            fake_messaging,
        ),
        patch(
            "hermes_runtime.commands.install_hermes._prefetch_local_stt_model",
            _fake_local_stt_model_prefetch,
        ),
        patch(
            "hermes_runtime.commands.install_hermes._configure_day_one_capabilities",
            _fake_day_one_capabilities,
        ),
        patch(
            "hermes_runtime.commands.install_hermes._probe_browser_automation",
            fake_browser_probe,
        ),
        patch(
            "hermes_runtime.commands.install_hermes._install_browser_fallback",
            fake_browser_fallback,
        ),
        patch(
            "hermes_runtime.commands.install_hermes._install_codex_auth_quick_commands",
            return_value={"installed": True, "commands": ["codex_auth"]},
        ),
        patch(
            "hermes_runtime.commands.install_hermes._install_codex_auth_plugin_commands",
            return_value={"installed": True, "commands": ["codex_auth"]},
        ),
    ):
        result = asyncio.run(
            run_command(SimpleNamespace(), {"kind": "install_hermes"})
        )

    assert fallback_calls == 1
    assert browser_probes == []
    assert result["browser_smoke"]["smoke_status"] == "passed"
    assert result["browser_fallback"]["attempted"] is True


def test_install_hermes_repairs_messaging_when_cli_exists() -> None:
    async def fake_status(*, timeout_seconds: int = 30) -> dict[str, object]:
        return _status()

    async def fake_messaging() -> dict[str, object]:
        return {
            "ok": True,
            "changed": True,
            "before": {"ok": False},
            "after": {"ok": True},
        }

    with (
        patch(
            "hermes_runtime.commands.install_hermes.find_hermes_binary",
            return_value=Path("/usr/local/bin/hermes"),
        ),
        patch(
            "hermes_runtime.commands.install_hermes.probe_hermes_status",
            fake_status,
        ),
        patch(
            "hermes_runtime.commands.install_hermes._ensure_messaging_dependencies",
            fake_messaging,
        ),
        patch(
            "hermes_runtime.commands.install_hermes._prefetch_local_stt_model",
            _fake_local_stt_model_prefetch,
        ),
        patch(
            "hermes_runtime.commands.install_hermes._configure_day_one_capabilities",
            _fake_day_one_capabilities,
        ),
        patch(
            "hermes_runtime.commands.install_hermes._probe_browser_automation",
            _fake_browser_smoke,
        ),
        patch(
            "hermes_runtime.commands.install_hermes._install_codex_auth_quick_commands",
            return_value={"installed": True, "commands": ["codex_auth"]},
        ),
        patch(
            "hermes_runtime.commands.install_hermes._install_codex_auth_plugin_commands",
            return_value={"installed": True, "commands": ["codex_auth"]},
        ),
    ):
        result = asyncio.run(
            run_command(SimpleNamespace(), {"kind": "install_hermes"})
        )

    assert result["installed_before"] is True
    assert result["installed_now"] is False
    assert result["changed"] is False
    assert result["messaging"]["changed"] is True
    assert result["multimodal_defaults"]["ok"] is True
    assert result["local_stt_model_prefetch"]["model"] == "small"
    assert result["codex_auth"]["quick_commands"]["installed"] is True
    assert result["codex_auth"]["plugin_commands"]["installed"] is True


def test_install_hermes_runs_official_installer_when_missing() -> None:
    install_calls: list[tuple[str, dict[str, str] | None]] = []

    async def fake_status(*, timeout_seconds: int = 30) -> dict[str, object]:
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

    async def fake_messaging() -> dict[str, object]:
        return {"ok": True, "changed": True}

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
        patch(
            "hermes_runtime.commands.install_hermes._ensure_messaging_dependencies",
            fake_messaging,
        ),
        patch(
            "hermes_runtime.commands.install_hermes._prefetch_local_stt_model",
            _fake_local_stt_model_prefetch,
        ),
        patch(
            "hermes_runtime.commands.install_hermes._configure_day_one_capabilities",
            _fake_day_one_capabilities,
        ),
        patch(
            "hermes_runtime.commands.install_hermes._probe_browser_automation",
            _fake_browser_smoke,
        ),
        patch(
            "hermes_runtime.commands.install_hermes._install_codex_auth_quick_commands",
            return_value={"installed": True, "commands": ["codex_auth"]},
        ),
        patch(
            "hermes_runtime.commands.install_hermes._install_codex_auth_plugin_commands",
            return_value={"installed": True, "commands": ["codex_auth"]},
        ),
    ):
        result = asyncio.run(
            run_command(SimpleNamespace(), {"kind": "install_hermes"})
        )

    assert len(install_calls) == 1
    script, env = install_calls[0]
    assert (
        "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | "
        "bash -s -- --commit 40a53ca0317b0ddc1a79133fb70fc5eb75c3d74b"
        in script
    )
    assert "--skip-browser" not in script
    assert env == {"CI": "1"}
    assert result["installed_before"] is False
    assert result["installed_now"] is True
    assert result["installed_after"] is True
    assert result["already_installed"] is False
    assert result["changed"] is True
    assert result["messaging"]["changed"] is True
    assert result["multimodal_defaults"]["ok"] is True
    assert result["browser_smoke"]["smoke_status"] == "passed"
    assert result["day_one_capabilities"]["baseline_id"] == (
        "tinyhat-hermes-day-one-v1"
    )
    assert result["day_one_capabilities"]["capabilities"]["web_search"] == {
        "state": "ready",
        "provider": "ddgs",
        "dependency_ready": True,
        "smoke_status": "not_run",
    }
    assert result["day_one_capabilities"]["capabilities"][
        "telegram_rich_rendering"
    ] == {
        "state": "configured_waiting_for_assignment",
        "rich_messages": True,
        "rich_drafts": False,
        "smoke_status": "not_run",
    }
    assert result["local_stt_model_prefetch"]["model"] == "small"
    assert result["codex_auth"]["quick_commands"]["installed"] is True
    assert result["codex_auth"]["plugin_commands"]["installed"] is True
    assert result["prerequisites"]["attempted"] is True


def test_install_hermes_retries_transient_status_failure_after_install() -> None:
    install_calls: list[str] = []
    sleep_calls: list[float] = []
    status_timeouts: list[int] = []
    statuses = [_status(installed=True, ok=False), _status()]

    async def fake_prerequisites() -> dict[str, object]:
        return {"missing_before": [], "attempted": False}

    async def fake_run_shell(
        script: str,
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del timeout_seconds, env
        install_calls.append(script)
        return {"ok": True, "returncode": 0, "stdout": "installed", "stderr": ""}

    async def fake_status(*, timeout_seconds: int = 30) -> dict[str, object]:
        status_timeouts.append(timeout_seconds)
        return statuses.pop(0)

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    async def fake_messaging() -> dict[str, object]:
        return {"ok": True, "changed": True}

    with (
        patch.dict(
            os.environ,
            {
                "TINYHAT_HERMES_STATUS_PROBE_ATTEMPTS": "3",
                "TINYHAT_HERMES_STATUS_PROBE_RETRY_DELAY_SECONDS": "1",
                "TINYHAT_HERMES_STATUS_PROBE_TIMEOUT_SECONDS": "45",
            },
        ),
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
        patch("hermes_runtime.commands.install_hermes.asyncio.sleep", fake_sleep),
        patch(
            "hermes_runtime.commands.install_hermes._ensure_messaging_dependencies",
            fake_messaging,
        ),
        patch(
            "hermes_runtime.commands.install_hermes._prefetch_local_stt_model",
            _fake_local_stt_model_prefetch,
        ),
        patch(
            "hermes_runtime.commands.install_hermes._configure_day_one_capabilities",
            _fake_day_one_capabilities,
        ),
        patch(
            "hermes_runtime.commands.install_hermes._probe_browser_automation",
            _fake_browser_smoke,
        ),
        patch(
            "hermes_runtime.commands.install_hermes._install_codex_auth_quick_commands",
            return_value={"installed": True, "commands": ["codex_auth"]},
        ),
        patch(
            "hermes_runtime.commands.install_hermes._install_codex_auth_plugin_commands",
            return_value={"installed": True, "commands": ["codex_auth"]},
        ),
    ):
        result = asyncio.run(
            run_command(SimpleNamespace(), {"kind": "install_hermes"})
        )

    assert len(install_calls) == 1
    assert result["installed_now"] is True
    assert result["status"]["ok"] is True
    assert result["status"]["probe_attempt_count"] == 2
    assert status_timeouts == [45, 45]
    assert sleep_calls == [1]


def test_install_hermes_stops_retrying_status_timeouts_with_summary() -> None:
    sleep_calls: list[float] = []
    status_timeouts: list[int] = []
    timed_out_status = {
        **_status(installed=True, ok=False),
        "commands": {
            "version": {
                "ok": True,
                "returncode": 0,
                "stdout": "Hermes Agent 0.1.0",
                "stderr": "",
                "timed_out": False,
            },
            "status": {
                "ok": False,
                "returncode": None,
                "stdout": "",
                "stderr": "command timed out after 45s",
                "timed_out": True,
            },
            "status_all": {
                "ok": True,
                "returncode": 0,
                "stdout": "ok",
                "stderr": "",
                "timed_out": False,
            },
        },
    }

    async def fake_status(*, timeout_seconds: int = 30) -> dict[str, object]:
        status_timeouts.append(timeout_seconds)
        return timed_out_status

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    with (
        patch.dict(
            os.environ,
            {
                "TINYHAT_HERMES_STATUS_PROBE_ATTEMPTS": "3",
                "TINYHAT_HERMES_STATUS_PROBE_RETRY_DELAY_SECONDS": "1",
                "TINYHAT_HERMES_STATUS_PROBE_TIMEOUT_SECONDS": "45",
            },
        ),
        patch(
            "hermes_runtime.commands.install_hermes.find_hermes_binary",
            return_value=Path("/usr/local/bin/hermes"),
        ),
        patch(
            "hermes_runtime.commands.install_hermes.probe_hermes_status",
            fake_status,
        ),
        patch("hermes_runtime.commands.install_hermes.asyncio.sleep", fake_sleep),
    ):
        with _raises_runtime(
            "failed after 1 attempt\\(s\\): status: command timed out after 45s"
        ):
            asyncio.run(run_command(SimpleNamespace(), {"kind": "install_hermes"}))

    assert status_timeouts == [45]
    assert sleep_calls == []


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
        patch.dict(os.environ, {"TINYHAT_HERMES_STATUS_PROBE_ATTEMPTS": "1"}),
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

    async def fake_status(*, timeout_seconds: int = 30) -> dict[str, object]:
        return _status(installed=False, ok=False)

    async def fake_messaging() -> dict[str, object]:
        return {"ok": True, "changed": False}

    with (
        patch.dict(os.environ, {"TINYHAT_HERMES_STATUS_PROBE_ATTEMPTS": "1"}),
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
        patch(
            "hermes_runtime.commands.install_hermes._ensure_messaging_dependencies",
            fake_messaging,
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

    async def fake_status(*, timeout_seconds: int = 30) -> dict[str, object]:
        return _status(installed=True, ok=False)

    async def fake_messaging() -> dict[str, object]:
        return {"ok": True, "changed": False}

    with (
        patch.dict(os.environ, {"TINYHAT_HERMES_STATUS_PROBE_ATTEMPTS": "1"}),
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
        patch(
            "hermes_runtime.commands.install_hermes._ensure_messaging_dependencies",
            fake_messaging,
        ),
    ):
        with _raises_runtime("status checks failed"):
            asyncio.run(run_command(SimpleNamespace(), {"kind": "install_hermes"}))


def test_install_hermes_raises_when_day_one_dependencies_are_unavailable() -> None:
    async def fake_status(*, timeout_seconds: int = 30) -> dict[str, object]:
        return _status()

    async def fake_messaging() -> dict[str, object]:
        return {"ok": False, "changed": False, "message": "missing telegram"}

    with (
        patch(
            "hermes_runtime.commands.install_hermes.find_hermes_binary",
            return_value=Path("/usr/local/bin/hermes"),
        ),
        patch(
            "hermes_runtime.commands.install_hermes.probe_hermes_status",
            fake_status,
        ),
        patch(
            "hermes_runtime.commands.install_hermes._ensure_messaging_dependencies",
            fake_messaging,
        ),
    ):
        with _raises_runtime("day-one dependencies"):
            asyncio.run(run_command(SimpleNamespace(), {"kind": "install_hermes"}))


def test_prefetch_local_stt_model_warms_selected_model() -> None:
    calls: list[tuple[list[str], int, dict[str, str] | None]] = []

    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        calls.append((args, timeout_seconds, env))
        return {
            "args": args,
            "returncode": 0,
            "ok": True,
            "timed_out": False,
            "duration_ms": 1,
            "stdout": "cached:medium\n",
            "stderr": "",
        }

    with tempfile.TemporaryDirectory() as tmp:
        project_dir = Path(tmp) / "hermes-agent"
        python_bin = project_dir / "venv" / "bin" / "python"
        python_bin.parent.mkdir(parents=True)
        python_bin.write_text("", encoding="utf-8")
        (project_dir / "pyproject.toml").write_text(
            "[project]\nname='hermes-agent'\n",
            encoding="utf-8",
        )

        with (
            patch.dict(
                os.environ,
                {
                    "TINYHAT_HERMES_LOCAL_STT_MODEL": "medium",
                    "TINYHAT_HERMES_STT_MODEL_PREFETCH_TIMEOUT_SECONDS": "120",
                },
            ),
            patch(
                "hermes_runtime.commands.install_hermes._find_hermes_project_dir",
                return_value=project_dir,
            ),
            patch(
                "hermes_runtime.commands.install_hermes.run_process",
                fake_run_process,
            ),
        ):
            result = asyncio.run(install_hermes._prefetch_local_stt_model())

    assert result["ok"] is True
    assert result["changed"] is True
    assert result["model"] == "medium"
    assert len(calls) == 1
    args, timeout_seconds, env = calls[0]
    assert args[0] == str(python_bin)
    assert "WhisperModel(model, device='cpu', compute_type='int8')" in args[-1]
    assert timeout_seconds == 120
    assert env == {
        "TINYHAT_LOCAL_STT_MODEL": "medium",
        "HF_HUB_DISABLE_TELEMETRY": "1",
    }


def test_prefetch_local_stt_model_can_be_skipped() -> None:
    with (
        patch.dict(
            os.environ,
            {
                "TINYHAT_SKIP_LOCAL_STT_MODEL_PREFETCH": "1",
                "TINYHAT_HERMES_LOCAL_STT_MODEL": "tiny",
            },
        ),
        patch(
            "hermes_runtime.commands.install_hermes._find_hermes_project_dir",
            side_effect=AssertionError("project lookup should not run"),
        ),
    ):
        result = asyncio.run(install_hermes._prefetch_local_stt_model())

    assert result == {
        "ok": True,
        "changed": False,
        "skipped": True,
        "skip_env": "TINYHAT_SKIP_LOCAL_STT_MODEL_PREFETCH",
        "model": "tiny",
    }


def test_install_hermes_reports_prefetch_failure_without_blocking() -> None:
    async def fake_status(*, timeout_seconds: int = 30) -> dict[str, object]:
        return _status()

    async def fake_messaging() -> dict[str, object]:
        return {"ok": True, "changed": False}

    async def fake_prefetch() -> dict[str, object]:
        return {"ok": False, "changed": False, "model": "medium"}

    with (
        patch(
            "hermes_runtime.commands.install_hermes.find_hermes_binary",
            return_value=Path("/usr/local/bin/hermes"),
        ),
        patch(
            "hermes_runtime.commands.install_hermes.probe_hermes_status",
            fake_status,
        ),
        patch(
            "hermes_runtime.commands.install_hermes._ensure_messaging_dependencies",
            fake_messaging,
        ),
        patch(
            "hermes_runtime.commands.install_hermes._prefetch_local_stt_model",
            fake_prefetch,
        ),
        patch(
            "hermes_runtime.commands.install_hermes._configure_day_one_capabilities",
            _fake_day_one_capabilities,
        ),
        patch(
            "hermes_runtime.commands.install_hermes._probe_browser_automation",
            _fake_browser_smoke,
        ),
    ):
        result = asyncio.run(run_command(SimpleNamespace(), {"kind": "install_hermes"}))

    assert result["installed_after"] is True
    assert result["multimodal_defaults"]["ok"] is True
    assert result["local_stt_model_prefetch"]["ok"] is False
    assert result["local_stt_model_prefetch_warning"] == (
        "Hermes local STT model prefetch failed; provisioning "
        "continues because OpenRouter STT is the active provider."
    )
