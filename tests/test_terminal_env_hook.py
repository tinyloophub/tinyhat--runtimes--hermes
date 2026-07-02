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
        assert "hermes_runtime.terminal_env_export" in hook_text
        assert "print-exports" in hook_text
        assert "TINYHAT_RUNTIME_PREFIX" in hook_text
        assert 'eval "$__tinyhat_exports"' in hook_text
        config_text = config_path.read_text(encoding="utf-8")
        assert "terminal:" in config_text
        assert "shell_init_files:" in config_text
        assert f"    - {hook_path}" in config_text

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

    _with_home(run)


def test_terminal_env_hook_installs_profile_dropin_when_dir_exists() -> None:
    def run(home: Path) -> None:
        profile_d = home / "profile.d"
        profile_d.mkdir()
        os.environ["TINYHAT_PROFILE_D_DIR"] = str(profile_d)

        first = install_terminal_env_reload_hook()
        second = install_terminal_env_reload_hook()
        dropin = profile_d / "tinyhat-hermes-env.sh"

        assert first["profile"]["installed"] is True
        assert first["profile"]["path"] == str(dropin)
        assert first["profile"]["updated"] is True
        assert second["profile"]["updated"] is False
        dropin_text = dropin.read_text(encoding="utf-8")
        assert dropin_text == (
            home / ".hermes" / "tinyhat" / "terminal-env.sh"
        ).read_text(encoding="utf-8")
        assert (dropin.stat().st_mode & 0o777) == 0o644

    _with_home(run)


def test_terminal_env_hook_skips_profile_dropin_when_dir_missing() -> None:
    def run(home: Path) -> None:
        os.environ["TINYHAT_PROFILE_D_DIR"] = str(home / "no-such-profile.d")

        result = install_terminal_env_reload_hook()

        assert result["installed"] is True
        assert result["profile"]["installed"] is False
        assert result["profile"]["skipped_reason"] == "profile_d_dir_missing"

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
