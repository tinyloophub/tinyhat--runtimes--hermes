"""Focused tests for Tinyhat's Hermes terminal env hook."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hermes_runtime.terminal_env_hook import install_terminal_env_reload_hook  # noqa: E402


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


def test_terminal_env_hook_creates_config_and_export_script() -> None:
    def run(home: Path) -> None:
        result = install_terminal_env_reload_hook()
        hook_path = home / ".hermes" / "tinyhat" / "terminal-env.sh"
        config_path = home / ".hermes" / "config.yaml"

        assert result["installed"] is True
        assert result["hook"]["path"] == str(hook_path)
        assert result["config"]["config_file"] == str(config_path)
        hook_text = hook_path.read_text(encoding="utf-8")
        assert "set -a" in hook_text
        assert "$__tinyhat_hermes_home/.env" in hook_text
        assert "/usr/local/lib/hermes-agent" in hook_text
        config_text = config_path.read_text(encoding="utf-8")
        assert "terminal:" in config_text
        assert "shell_init_files:" in config_text
        assert f"    - {hook_path}" in config_text

    _with_home(run)


def test_terminal_env_hook_adds_secret_terminal_passthrough() -> None:
    def run(home: Path) -> None:
        result = install_terminal_env_reload_hook(
            secret_names=["SECOND_SECRET", "EXA_API_KEY", "EXA_API_KEY"],
        )
        config_path = home / ".hermes" / "config.yaml"
        text = config_path.read_text(encoding="utf-8")

        assert result["terminal_secret_env_names"] == ["EXA_API_KEY", "SECOND_SECRET"]
        assert result["config"]["paths"] == [
            "terminal.shell_init_files",
            "terminal.env_passthrough",
            "terminal.docker_forward_env",
        ]
        assert result["config"]["env_passthrough"]["added"] == [
            "EXA_API_KEY",
            "SECOND_SECRET",
        ]
        assert result["config"]["docker_forward_env"]["added"] == [
            "EXA_API_KEY",
            "SECOND_SECRET",
        ]
        assert "  env_passthrough:\n" in text
        assert "    - EXA_API_KEY\n" in text
        assert "    - SECOND_SECRET\n" in text
        assert "  docker_forward_env:\n" in text
        assert "exa-secret" not in text

    _with_home(run)


def test_terminal_env_hook_honors_tinyhat_hermes_home() -> None:
    def run(home: Path) -> None:
        hermes_home = home / "custom-hermes-home"
        os.environ["TINYHAT_HERMES_HOME"] = str(hermes_home)

        result = install_terminal_env_reload_hook()
        hook_path = hermes_home / "tinyhat" / "terminal-env.sh"
        config_path = hermes_home / "config.yaml"

        assert result["hook"]["path"] == str(hook_path)
        assert result["config"]["config_file"] == str(config_path)
        assert hook_path.exists()
        assert config_path.exists()
        assert "TINYHAT_HERMES_HOME" in hook_path.read_text(encoding="utf-8")

    _with_home(run)


def test_terminal_env_hook_preserves_existing_passthrough_entries() -> None:
    def run(home: Path) -> None:
        config_path = home / ".hermes" / "config.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            "\n".join(
                [
                    "terminal:",
                    "  env_passthrough: [KEEP_ME]",
                    "  docker_forward_env:",
                    "    - KEEP_DOCKER",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        first = install_terminal_env_reload_hook(secret_names=["EXA_API_KEY"])
        second = install_terminal_env_reload_hook(secret_names=["EXA_API_KEY"])
        text = config_path.read_text(encoding="utf-8")

        assert first["config"]["updated"] is True
        assert first["config"]["env_passthrough"]["added"] == ["EXA_API_KEY"]
        assert first["config"]["docker_forward_env"]["added"] == ["EXA_API_KEY"]
        assert second["config"]["updated"] is False
        assert "    - KEEP_ME\n" in text
        assert "    - KEEP_DOCKER\n" in text
        assert text.count("EXA_API_KEY") == 2

    _with_home(run)


def test_terminal_env_hook_adds_to_existing_terminal_block() -> None:
    def run(home: Path) -> None:
        config_path = home / ".hermes" / "config.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            "\n".join(
                [
                    "model:",
                    "  provider: auto",
                    "terminal:",
                    "  backend: local",
                    "  timeout: 180",
                    "browser:",
                    "  inactivity_timeout: 120",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        result = install_terminal_env_reload_hook()
        text = config_path.read_text(encoding="utf-8")

        assert result["config"]["updated"] is True
        assert "terminal:\n  shell_init_files:\n" in text
        assert "  backend: local" in text
        assert "browser:" in text

    _with_home(run)


def test_terminal_env_hook_adds_to_existing_block_list() -> None:
    def run(home: Path) -> None:
        config_path = home / ".hermes" / "config.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            "\n".join(
                [
                    "terminal:",
                    "  shell_init_files:",
                    "    - /opt/custom-one.sh",
                    "  backend: local",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        result = install_terminal_env_reload_hook()
        text = config_path.read_text(encoding="utf-8")
        hook_path = home / ".hermes" / "tinyhat" / "terminal-env.sh"

        assert result["config"]["updated"] is True
        assert "    - /opt/custom-one.sh\n" in text
        assert f"    - {hook_path}\n" in text
        assert text.index(str(hook_path)) < text.index("  backend: local")

    _with_home(run)


def test_terminal_env_hook_is_idempotent_for_inline_list() -> None:
    def run(home: Path) -> None:
        config_path = home / ".hermes" / "config.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            "terminal:\n  shell_init_files: [/opt/custom.sh]\n",
            encoding="utf-8",
        )

        first = install_terminal_env_reload_hook()
        second = install_terminal_env_reload_hook()
        text = config_path.read_text(encoding="utf-8")
        hook_path = home / ".hermes" / "tinyhat" / "terminal-env.sh"

        assert first["config"]["updated"] is True
        assert second["config"]["updated"] is False
        assert "    - /opt/custom.sh\n" in text
        assert text.count(str(hook_path)) == 1

    _with_home(run)
