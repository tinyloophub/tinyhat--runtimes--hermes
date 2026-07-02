"""Focused tests for Tinyhat's Hermes terminal env passthrough helper."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hermes_runtime import terminal_env_passthrough  # noqa: E402


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


def _with_home(fn) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        old_env = os.environ.copy()
        os.environ.clear()
        os.environ.update({"HOME": str(home)})
        try:
            fn(home)
        finally:
            os.environ.clear()
            os.environ.update(old_env)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_register_uses_hermes_passthrough_for_non_protected_names() -> None:
    def run(home: Path) -> None:
        result = terminal_env_passthrough.register_name("CUSTOM_SERVICE_TOKEN")
        config_path = home / ".hermes" / "config.yaml"
        text = config_path.read_text(encoding="utf-8")

        assert result["registered_names"] == ["CUSTOM_SERVICE_TOKEN"]
        assert result["skipped_names"] == []
        assert "terminal:\n  env_passthrough:\n" in text
        assert "    - CUSTOM_SERVICE_TOKEN\n" in text

    _with_home(run)


def test_register_skips_hermes_provider_credentials() -> None:
    def run(home: Path) -> None:
        result = terminal_env_passthrough.register_name("EXA_API_KEY")
        config_path = home / ".hermes" / "config.yaml"

        assert result["registered_names"] == []
        assert result["skipped_names"] == [
            {
                "name": "EXA_API_KEY",
                "reason": "hermes_protected_credential",
                "message": (
                    "Hermes keeps this provider/tool credential in the main "
                    "agent process and refuses terminal env passthrough."
                ),
            }
        ]
        assert not config_path.exists()

    _with_home(run)


def test_passthrough_update_dedupes_existing_inline_list() -> None:
    def run(home: Path) -> None:
        config_path = home / ".hermes" / "config.yaml"
        _write(
            config_path,
            "terminal:\n  env_passthrough: [CUSTOM_SERVICE_TOKEN]\n  backend: local\n",
        )

        result = terminal_env_passthrough.sync_terminal_env_passthrough(
            ["CUSTOM_SERVICE_TOKEN", "SECOND_SERVICE_TOKEN"],
        )
        text = config_path.read_text(encoding="utf-8")

        assert result["config"]["updated"] is True
        assert text.count("CUSTOM_SERVICE_TOKEN") == 1
        assert "    - SECOND_SERVICE_TOKEN\n" in text
        assert "  backend: local\n" in text

    _with_home(run)


def test_passthrough_removes_deleted_names() -> None:
    def run(home: Path) -> None:
        config_path = home / ".hermes" / "config.yaml"
        _write(
            config_path,
            "\n".join(
                [
                    "terminal:",
                    "  env_passthrough:",
                    "    - CUSTOM_SERVICE_TOKEN",
                    "    - OLD_SERVICE_TOKEN",
                ]
            )
            + "\n",
        )

        result = terminal_env_passthrough.sync_terminal_env_passthrough(
            ["CUSTOM_SERVICE_TOKEN"],
            remove_names=["OLD_SERVICE_TOKEN"],
        )
        text = config_path.read_text(encoding="utf-8")

        assert result["removed_names"] == ["OLD_SERVICE_TOKEN"]
        assert "CUSTOM_SERVICE_TOKEN" in text
        assert "OLD_SERVICE_TOKEN" not in text

    _with_home(run)


def test_register_rejects_invalid_names() -> None:
    def run(_home: Path) -> None:
        try:
            terminal_env_passthrough.register_name("not a name")
        except ValueError:
            pass
        else:  # pragma: no cover - defensive
            raise AssertionError("invalid names must be rejected")

    _with_home(run)


def test_cli_register() -> None:
    def run(_home: Path) -> None:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)

        register = subprocess.run(
            [
                sys.executable,
                "-m",
                "hermes_runtime.terminal_env_passthrough",
                "register",
                "CUSTOM_SERVICE_TOKEN",
            ],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        assert '"registered_names": ["CUSTOM_SERVICE_TOKEN"]' in register.stdout

        bad = subprocess.run(
            [
                sys.executable,
                "-m",
                "hermes_runtime.terminal_env_passthrough",
                "register",
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert bad.returncode == 2

    _with_home(run)
