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


async def _fake_day_one_multimedia(_hermes_bin: Path) -> dict[str, object]:
    return {
        "ok": True,
        "commands": [
            {
                "key": "stt.provider",
                "value": "openrouter",
                "ok": True,
            },
            {
                "key": "auxiliary.vision.provider",
                "value": "openrouter",
                "ok": True,
            },
        ],
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
    assert "--python" not in script
    assert env == {"PIP_DISABLE_PIP_VERSION_CHECK": "1"}


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
            "hermes_runtime.commands.install_hermes._configure_day_one_multimedia",
            _fake_day_one_multimedia,
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


def test_install_hermes_repairs_messaging_when_cli_exists() -> None:
    async def fake_status() -> dict[str, object]:
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
            "hermes_runtime.commands.install_hermes._configure_day_one_multimedia",
            _fake_day_one_multimedia,
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
            "hermes_runtime.commands.install_hermes._configure_day_one_multimedia",
            _fake_day_one_multimedia,
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
        "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- --skip-browser"
        in script
    )
    assert env == {"CI": "1"}
    assert result["installed_before"] is False
    assert result["installed_now"] is True
    assert result["installed_after"] is True
    assert result["already_installed"] is False
    assert result["changed"] is True
    assert result["messaging"]["changed"] is True
    assert result["multimodal_defaults"]["ok"] is True
    assert result["local_stt_model_prefetch"]["model"] == "small"
    assert result["codex_auth"]["quick_commands"]["installed"] is True
    assert result["codex_auth"]["plugin_commands"]["installed"] is True
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

    async def fake_messaging() -> dict[str, object]:
        return {"ok": True, "changed": False}

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

    async def fake_messaging() -> dict[str, object]:
        return {"ok": True, "changed": False}

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
    ):
        with _raises_runtime("status checks failed"):
            asyncio.run(run_command(SimpleNamespace(), {"kind": "install_hermes"}))


def test_install_hermes_raises_when_messaging_is_unavailable() -> None:
    async def fake_status() -> dict[str, object]:
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
        with _raises_runtime("messaging dependencies"):
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
    async def fake_status() -> dict[str, object]:
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
            "hermes_runtime.commands.install_hermes._configure_day_one_multimedia",
            _fake_day_one_multimedia,
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
