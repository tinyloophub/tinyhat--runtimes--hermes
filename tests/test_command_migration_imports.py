"""Focused tests for OpenClaw -> Hermes import runtime commands."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hermes_runtime.commands import run_command  # noqa: E402
from hermes_runtime.commands import import_openclaw_state  # noqa: E402


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


class FakePlatform:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.gets: list[str] = []

    async def get_json(self, path: str) -> dict[str, Any]:
        self.gets.append(path)
        return self.payload


def test_import_legacy_tinyhat_secrets_writes_env_and_restarts() -> None:
    events: list[tuple[str, str]] = []
    platform = FakePlatform(
        {
            "revision": 9,
            "secrets": {
                "EXA_API_KEY": "exa-secret",
                "VOICE_TOOLS_OPENAI_KEY": "voice-secret",
            },
        }
    )

    async def fake_run_gateway(_hermes_bin: Path) -> dict[str, Any]:
        events.append(("gateway", os.environ.get("EXA_API_KEY") or ""))
        return {"healthy": True, "started": True}

    with tempfile.TemporaryDirectory() as tmp:
        env_file = Path(tmp) / ".hermes" / ".env"
        old_env = os.environ.copy()
        os.environ.clear()
        try:
            with (
                patch(
                    "hermes_runtime.commands.import_legacy_tinyhat_secrets._env_file_candidates",
                    return_value=[env_file],
                ),
                patch(
                    "hermes_runtime.commands.apply_config.find_hermes_binary",
                    return_value=Path("/usr/local/bin/hermes"),
                ),
                patch(
                    "hermes_runtime.commands.apply_config._run_gateway",
                    fake_run_gateway,
                ),
            ):
                result = asyncio.run(
                    run_command(
                        SimpleNamespace(
                            platform=platform,
                            platform_auth="gcloud",
                            computer_id="42",
                        ),
                        {
                            "kind": "import_legacy_tinyhat_secrets",
                            "spec": {},
                        },
                    )
                )
            env_text = env_file.read_text(encoding="utf-8")
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    assert platform.gets == ["/hapi/v1/computers/me/runtime-secrets"]
    assert result["schema"] == "tinyhat_hermes_import_legacy_tinyhat_secrets_v1"
    assert result["secret_names"] == ["EXA_API_KEY", "VOICE_TOOLS_OPENAI_KEY"]
    assert result["secret_count"] == 2
    assert result["values_masked"] is True
    assert result["gateway"]["healthy"] is True
    assert events == [("gateway", "exa-secret")]
    assert "exa-secret" not in str(result)
    assert 'EXA_API_KEY="exa-secret"' in env_text


def test_import_openclaw_state_runs_hermes_claw_migrate() -> None:
    calls: list[list[str]] = []

    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        del timeout_seconds, env
        calls.append(args)
        return {
            "ok": True,
            "returncode": 0,
            "timed_out": False,
            "duration_ms": 123,
            "stdout": "migrated 3 items",
            "stderr": "",
            "stdout_truncated": False,
            "stderr_truncated": False,
        }

    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "openclaw"
        source.mkdir()
        with (
            patch.object(
                import_openclaw_state,
                "find_hermes_binary",
                return_value=Path("/usr/local/bin/hermes"),
            ),
            patch.object(import_openclaw_state, "run_process", fake_run_process),
        ):
            result = asyncio.run(
                run_command(
                    SimpleNamespace(platform=None),
                    {
                        "kind": "import_openclaw_state",
                        "spec": {"source": str(source)},
                    },
                )
            )

    assert calls == [
        [
            "/usr/local/bin/hermes",
            "claw",
            "migrate",
            "--source",
            str(source),
            "--preset",
            "full",
            "--overwrite",
            "--yes",
        ]
    ]
    assert result["schema"] == "tinyhat_hermes_import_openclaw_state_v1"
    assert result["imported"] is True
    assert result["migrate_secrets"] is False
    assert result["hermes"]["stdout"] == "migrated 3 items"


def test_import_openclaw_state_private_value_flags_migrate_secrets() -> None:
    for private_flag in ("include_private_values", "migrate_secrets"):
        calls: list[list[str]] = []

        async def fake_run_process(
            args: list[str],
            *,
            timeout_seconds: int,
            env: dict[str, str] | None = None,
        ) -> dict[str, Any]:
            del timeout_seconds, env
            calls.append(args)
            return {
                "ok": True,
                "returncode": 0,
                "timed_out": False,
                "duration_ms": 123,
                "stdout": "migrated 3 items",
                "stderr": "",
                "stdout_truncated": False,
                "stderr_truncated": False,
            }

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "openclaw"
            source.mkdir()
            with (
                patch.object(
                    import_openclaw_state,
                    "find_hermes_binary",
                    return_value=Path("/usr/local/bin/hermes"),
                ),
                patch.object(import_openclaw_state, "run_process", fake_run_process),
            ):
                result = asyncio.run(
                    run_command(
                        SimpleNamespace(platform=None),
                        {
                            "kind": "import_openclaw_state",
                            "spec": {
                                "source": str(source),
                                private_flag: True,
                            },
                        },
                    )
                )

        assert calls == [
            [
                "/usr/local/bin/hermes",
                "claw",
                "migrate",
                "--source",
                str(source),
                "--preset",
                "full",
                "--overwrite",
                "--yes",
                "--migrate-secrets",
            ]
        ]
        assert result["migrate_secrets"] is True
        assert result["include_private_values"] is True
