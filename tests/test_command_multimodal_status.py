"""Focused tests for the ``multimodal_status`` runtime command."""

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


def test_multimodal_status_reports_models_without_values() -> None:
    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: int,
    ) -> dict[str, object]:
        assert args == ["/usr/local/bin/hermes", "config", "show"]
        assert timeout_seconds == 30
        return {
            "ok": True,
            "returncode": 0,
            "stdout": "Auxiliary Models (overrides)\n  Vision provider=openrouter\n",
            "stderr": "",
            "duration_ms": 7,
        }

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        project = Path(tmp) / "project"
        hermes_home = home / ".hermes"
        hermes_home.mkdir(parents=True)
        project.mkdir()
        (hermes_home / "config.yaml").write_text(
            "\n".join(
                [
                    "stt:",
                    "  enabled: true",
                    "  provider: openrouter",
                    "  local:",
                    "    model: medium",
                    "  providers:",
                    "    openrouter:",
                    "      type: command",
                    "      command: python3 -m hermes_runtime.openrouter_stt --input {input_path}",
                    "      model: openai/whisper-large-v3-turbo",
                    "      language: auto",
                    "      timeout: 120",
                    "      output_format: txt",
                    "auxiliary:",
                    "  vision:",
                    "    provider: openrouter",
                    "    model: google/gemini-2.5-flash-lite",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (hermes_home / ".env").write_text(
            'OPENROUTER_API_KEY="sk-or-v1-secret"\n',
            encoding="utf-8",
        )
        old_env = os.environ.copy()
        os.environ.clear()
        os.environ.update(
            {
                "HOME": str(home),
                "HERMES_PROJECT_DIR": str(project),
            }
        )
        try:
            with (
                patch(
                    "hermes_runtime.commands.multimodal_status.find_hermes_binary",
                    return_value=Path("/usr/local/bin/hermes"),
                ),
                patch(
                    "hermes_runtime.commands.multimodal_status.run_process",
                    fake_run_process,
                ),
            ):
                result = asyncio.run(
                    run_command(SimpleNamespace(), {"kind": "multimodal_status"})
                )
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    assert result["schema"] == "tinyhat_hermes_multimodal_status_v1"
    assert result["stt"]["provider"] == "openrouter"
    assert result["stt"]["active_model"] == "openai/whisper-large-v3-turbo"
    assert result["stt"]["openrouter"]["command_provider_configured"] is True
    assert result["stt"]["openrouter"]["api_key_present"] is True
    assert result["stt"]["local_model"] == {
        "provider": "local",
        "model": "medium",
        "prepared_for_provider": "local",
        "automatic_fallback_from_openrouter": False,
    }
    assert result["stt"]["local_fallback"]["model"] == "medium"
    assert result["stt"]["local_fallback"]["automatic"] is False
    assert result["vision"] == {
        "provider": "openrouter",
        "model": "google/gemini-2.5-flash-lite",
        "uses_codex_auth": False,
    }
    assert result["secrets"]["values_masked"] is True
    assert "sk-or-v1-secret" not in str(result)
