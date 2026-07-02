"""Focused tests for the Tinyhat terminal env export module."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hermes_runtime import terminal_env_export  # noqa: E402
from hermes_runtime.runtime_env import (  # noqa: E402
    RUNTIME_SECRETS_END,
    RUNTIME_SECRETS_START,
    read_env_values,
)


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
        os.environ.update(
            {
                "HOME": str(home),
                # Keep the default project-dir candidate out of these tests.
                "HERMES_PROJECT_DIR": str(home / "no-such-project"),
            }
        )
        try:
            fn(home)
        finally:
            os.environ.clear()
            os.environ.update(old_env)


def _write_env(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_register_appends_validates_and_dedupes() -> None:
    def run(home: Path) -> None:
        first = terminal_env_export.register_name("EXA_API_KEY")
        second = terminal_env_export.register_name("EXA_API_KEY")
        third = terminal_env_export.register_name("SERPER_API_KEY")
        manifest = home / ".hermes" / "tinyhat" / "terminal-env-names"

        assert first["added"] is True
        assert second["added"] is False
        assert third["names"] == ["EXA_API_KEY", "SERPER_API_KEY"]
        assert manifest.read_text(encoding="utf-8") == "EXA_API_KEY\nSERPER_API_KEY\n"
        assert (manifest.stat().st_mode & 0o777) == 0o600

        try:
            terminal_env_export.register_name("not a name")
        except ValueError:
            pass
        else:  # pragma: no cover - defensive
            raise AssertionError("invalid names must be rejected")

    _with_home(run)


def test_register_refreshes_terminal_env_hook() -> None:
    def run(home: Path) -> None:
        profile_d = home / "profile.d"
        profile_d.mkdir()
        os.environ["TINYHAT_PROFILE_D_DIR"] = str(profile_d)

        result = terminal_env_export.register_name("EXA_API_KEY")

        hook = home / ".hermes" / "tinyhat" / "terminal-env.sh"
        config = home / ".hermes" / "config.yaml"
        profile = profile_d / "tinyhat-hermes-env.sh"
        assert result["terminal_env_hook"]["installed"] is True
        assert hook.exists()
        assert config.exists()
        assert profile.exists()
        assert "terminal_env_export" in hook.read_text(encoding="utf-8")

    _with_home(run)


def test_manifest_names_skip_comments_and_invalid_lines() -> None:
    def run(home: Path) -> None:
        manifest = home / ".hermes" / "tinyhat" / "terminal-env-names"
        _write_env(
            manifest,
            ["# comment", "", "EXA_API_KEY", "bad name", "EXA_API_KEY", "OTHER_KEY"],
        )

        assert terminal_env_export.read_manifest_names() == [
            "EXA_API_KEY",
            "OTHER_KEY",
        ]

    _with_home(run)


def test_exportable_names_union_managed_block_and_manifest() -> None:
    def run(home: Path) -> None:
        _write_env(
            home / ".hermes" / ".env",
            [
                'TELEGRAM_BOT_TOKEN="internal-value"',
                RUNTIME_SECRETS_START,
                'PLATFORM_SECRET="from-platform"',
                RUNTIME_SECRETS_END,
                "EXA_API_KEY='exa-value'",
            ],
        )
        terminal_env_export.register_name("EXA_API_KEY")

        names = terminal_env_export.exportable_names()

        assert names == ["PLATFORM_SECRET", "EXA_API_KEY"]
        assert "TELEGRAM_BOT_TOKEN" not in names

    _with_home(run)


def test_render_export_lines_quotes_values_and_skips_missing() -> None:
    def run(home: Path) -> None:
        _write_env(
            home / ".hermes" / ".env",
            ["EXA_API_KEY=\"it's a 'value' with spaces\""],
        )
        terminal_env_export.register_name("EXA_API_KEY")
        terminal_env_export.register_name("MISSING_KEY")

        output = terminal_env_export.render_export_lines()

        assert output.startswith("export EXA_API_KEY=")
        assert "MISSING_KEY" not in output
        probe = subprocess.run(
            ["bash", "-c", f'{output}\nprintf %s "$EXA_API_KEY"'],
            capture_output=True,
            text=True,
            check=True,
        )
        assert probe.stdout == "it's a 'value' with spaces"

    _with_home(run)


def test_home_env_file_wins_over_project_env_file() -> None:
    def run(home: Path) -> None:
        project = home / "project"
        project.mkdir()
        os.environ["HERMES_PROJECT_DIR"] = str(project)
        _write_env(home / ".hermes" / ".env", ["EXA_API_KEY=home-value"])
        _write_env(project / ".env", ["EXA_API_KEY=project-value"])
        terminal_env_export.register_name("EXA_API_KEY")

        output = terminal_env_export.render_export_lines()

        assert output == "export EXA_API_KEY=home-value"

    _with_home(run)


def test_read_env_values_last_line_wins_within_one_file() -> None:
    def run(home: Path) -> None:
        env_path = home / ".hermes" / ".env"
        _write_env(
            env_path,
            ["EXA_API_KEY=old-value", "export EXA_API_KEY='new-value'"],
        )

        values = read_env_values([env_path], names=["EXA_API_KEY"])

        assert values == {"EXA_API_KEY": "new-value"}

    _with_home(run)


def test_cli_print_exports_and_register_round_trip() -> None:
    def run(home: Path) -> None:
        _write_env(home / ".hermes" / ".env", ["EXA_API_KEY=cli-value"])
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)

        register = subprocess.run(
            [
                sys.executable,
                "-m",
                "hermes_runtime.terminal_env_export",
                "register",
                "EXA_API_KEY",
            ],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        assert '"added": true' in register.stdout

        exports = subprocess.run(
            [sys.executable, "-m", "hermes_runtime.terminal_env_export", "print-exports"],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        assert exports.stdout.strip() == "export EXA_API_KEY=cli-value"

        bad = subprocess.run(
            [sys.executable, "-m", "hermes_runtime.terminal_env_export", "register"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert bad.returncode == 2

    _with_home(run)


def test_print_exports_is_empty_without_tinyhat_managed_names() -> None:
    def run(home: Path) -> None:
        _write_env(
            home / ".hermes" / ".env",
            ['TELEGRAM_BOT_TOKEN="internal-value"', "OPENAI_API_KEY=internal"],
        )

        assert terminal_env_export.render_export_lines() == ""

    _with_home(run)


def test_generated_hook_script_exports_into_login_style_shell() -> None:
    """End-to-end: source the installed hook in a shell and see the secret."""

    def run(home: Path) -> None:
        from hermes_runtime.terminal_env_hook import install_terminal_env_reload_hook

        profile_d = home / "profile.d"
        profile_d.mkdir()
        os.environ["TINYHAT_PROFILE_D_DIR"] = str(profile_d)
        _write_env(home / ".hermes" / ".env", ["EXA_API_KEY='hook-value'"])
        terminal_env_export.register_name("EXA_API_KEY")
        result = install_terminal_env_reload_hook()
        assert result["profile"]["installed"] is True

        probe = subprocess.run(
            [
                "bash",
                "-c",
                f'. "{profile_d / "tinyhat-hermes-env.sh"}"; printf %s "$EXA_API_KEY"',
            ],
            capture_output=True,
            text=True,
            env={
                "HOME": str(home),
                "PATH": os.defpath + os.pathsep + str(Path(sys.executable).parent),
                "TINYHAT_RUNTIME_PREFIX": str(ROOT),
                "HERMES_PROJECT_DIR": str(home / "no-such-project"),
            },
            check=True,
        )
        assert probe.stdout == "hook-value"

    _with_home(run)


def test_hook_script_survives_posix_sh_without_python3() -> None:
    """The drop-in must not break logins when python3 is unavailable."""

    def run(home: Path) -> None:
        from hermes_runtime.terminal_env_hook import install_terminal_env_reload_hook

        profile_d = home / "profile.d"
        profile_d.mkdir()
        os.environ["TINYHAT_PROFILE_D_DIR"] = str(profile_d)
        install_terminal_env_reload_hook()

        probe = subprocess.run(
            [
                "/bin/sh",
                "-c",
                f'. "{profile_d / "tinyhat-hermes-env.sh"}" && echo ok',
            ],
            capture_output=True,
            text=True,
            env={"HOME": str(home), "PATH": "/nonexistent"},
            check=False,
        )
        assert probe.stdout.strip() == "ok"
        assert probe.returncode == 0

    _with_home(run)
