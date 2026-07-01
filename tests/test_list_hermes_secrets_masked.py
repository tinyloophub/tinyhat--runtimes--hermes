"""Focused tests for the masked Hermes secret listing command."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

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


def test_list_hermes_secrets_masked_masks_values_from_managed_env_blocks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        project_dir = Path(tmp) / "project"
        project_dir.mkdir(parents=True)
        home_env = home / ".hermes" / ".env"
        home_env.parent.mkdir(parents=True)
        home_env.write_text(
            "\n".join(
                [
                    "PLAIN_NON_SECRET=visible",
                    "# tinyhat runtime secrets start",
                    'EXA_API_KEY="alpha-raw-token-999999"',
                    'SHORT_SECRET="tiny-short-raw-value-777777"',
                    "# tinyhat runtime secrets end",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        project_env = project_dir / ".env"
        project_env.write_text(
            "\n".join(
                [
                    "# tinyhat runtime secrets start",
                    'EXA_API_KEY="rotated-raw-token-222222"',
                    'SECOND_SECRET="beta-raw-token-888888"',
                    "# tinyhat runtime secrets end",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        old_env = os.environ.copy()
        os.environ.clear()
        os.environ.update(
            {
                "HOME": str(home),
                "HERMES_PROJECT_DIR": str(project_dir),
                "EXA_API_KEY": "rotated-raw-token-222222",
            }
        )
        try:
            result = asyncio.run(
                run_command(SimpleNamespace(), {"kind": "list_hermes_secrets_masked"})
            )
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    assert result["schema"] == "tinyhat_hermes_secrets_masked_v1"
    assert result["secret_count"] == 4
    assert result["values_masked"] is True
    by_name = {item["name"]: item for item in result["secrets"]}
    assert sorted(by_name) == [
        "EXA_API_KEY",
        "PLAIN_NON_SECRET",
        "SECOND_SECRET",
        "SHORT_SECRET",
    ]
    assert by_name["EXA_API_KEY"]["masked_value"] == "********"
    assert by_name["EXA_API_KEY"]["source_conflict"] is True
    assert by_name["EXA_API_KEY"]["source_count"] == 2
    assert by_name["EXA_API_KEY"]["managed_by_tinyhat"] is True
    assert by_name["EXA_API_KEY"]["available_in_process"] is True
    assert by_name["EXA_API_KEY"]["process_value_matches_managed"] is True
    assert by_name["SHORT_SECRET"]["masked_value"] == "********"
    assert by_name["PLAIN_NON_SECRET"]["managed_by_tinyhat"] is False
    assert by_name["SHORT_SECRET"]["available_in_process"] is False
    assert by_name["SHORT_SECRET"]["process_value_matches_managed"] is None
    assert any(item["path"] == str(home_env) for item in result["env_files"])
    assert any(item["path"] == str(project_env) for item in result["env_files"])
    assert any(
        item["managed_keys"] == ["EXA_API_KEY", "SHORT_SECRET"]
        for item in result["env_files"]
    )

    serialized = json.dumps(result, sort_keys=True)
    assert "alpha-raw-token-999999" not in serialized
    assert "rotated-raw-token-222222" not in serialized
    assert "beta-raw-token-888888" not in serialized
    assert "tiny-short-raw-value-777777" not in serialized


def test_list_hermes_secrets_masked_uses_hermes_env_path() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        hermes_env = Path(tmp) / "official-hermes.env"
        hermes_env.write_text(
            "\n".join(
                [
                    'EXA_API_KEY="official-env-secret-123456"',
                    'CUSTOM_RUNTIME_NAME="custom-runtime-secret-654321"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        hermes_bin = Path(tmp) / "hermes"
        hermes_bin.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"config\" ] && [ \"$2\" = \"env-path\" ]; then\n"
            "  printf '%s\\n' \"$HERMES_TEST_ENV_PATH\"\n"
            "  exit 0\n"
            "fi\n"
            "exit 64\n",
            encoding="utf-8",
        )
        hermes_bin.chmod(0o755)

        old_env = os.environ.copy()
        os.environ.clear()
        os.environ.update(
            {
                "HOME": str(Path(tmp) / "home"),
                "HERMES_BIN": str(hermes_bin),
                "HERMES_TEST_ENV_PATH": str(hermes_env),
                "HERMES_PROJECT_DIR": str(Path(tmp) / "missing-project"),
            }
        )
        try:
            result = asyncio.run(
                run_command(SimpleNamespace(), {"kind": "list_hermes_secrets_masked"})
            )
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    assert result["hermes_env_path"] == {
        "available": True,
        "ok": True,
        "path": str(hermes_env),
        "returncode": 0,
        "duration_ms": result["hermes_env_path"]["duration_ms"],
    }
    by_name = {item["name"]: item for item in result["secrets"]}
    assert sorted(by_name) == ["CUSTOM_RUNTIME_NAME", "EXA_API_KEY"]
    assert by_name["EXA_API_KEY"]["managed_by_tinyhat"] is False
    assert by_name["EXA_API_KEY"]["source_files"] == [str(hermes_env)]
    serialized = json.dumps(result, sort_keys=True)
    assert "official-env-secret-123456" not in serialized
    assert "custom-runtime-secret-654321" not in serialized


def test_list_hermes_secrets_masked_reports_empty_missing_env_files() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        missing_explicit = Path(tmp) / "missing.env"
        old_env = os.environ.copy()
        os.environ.clear()
        os.environ.update(
            {
                "HOME": str(home),
                "HERMES_ENV_FILE": str(missing_explicit),
                "HERMES_PROJECT_DIR": str(Path(tmp) / "missing-project"),
            }
        )
        try:
            result = asyncio.run(
                run_command(SimpleNamespace(), {"kind": "list_hermes_secrets_masked"})
            )
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    assert result["secret_count"] == 0
    assert result["secrets"] == []
    assert result["values_masked"] is True
    assert all(item["exists"] is False for item in result["env_files"])
