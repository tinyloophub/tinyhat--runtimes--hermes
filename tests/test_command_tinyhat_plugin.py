"""Focused tests for Tinyhat Hermes plugin runtime commands."""

from __future__ import annotations

import asyncio
import json
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


class FakeTmp:
    def cleanup(self) -> None:
        return None


def _ok(args: list[str]) -> dict[str, object]:
    return {
        "args": args,
        "returncode": 0,
        "ok": True,
        "timed_out": False,
        "duration_ms": 12,
        "stdout": "ok\n",
        "stderr": "",
        "stdout_truncated": False,
        "stderr_truncated": False,
    }


def _write_plugin(
    home: Path,
    *,
    version: str = "0.20.0",
    source: dict[str, object] | None = None,
) -> None:
    plugin_dir = home / "plugins" / "tinyhat"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.yaml").write_text(
        f"name: tinyhat\nversion: {version}\n",
        encoding="utf-8",
    )
    if source:
        (plugin_dir / ".tinyhat-plugin-source.json").write_text(
            json.dumps(source, sort_keys=True),
            encoding="utf-8",
        )


async def _fake_checkout(
    repo_url: str,
    ref: str,
) -> tuple[Path, str, FakeTmp]:
    checkout = Path(tempfile.mkdtemp(prefix="tinyhat-plugin-test-"))
    (checkout / "plugin.yaml").write_text("name: tinyhat\nversion: 0.20.0\n")
    return checkout, f"sha-for-{repo_url}-{ref}", FakeTmp()


async def _fake_checkout_0201(
    repo_url: str,
    ref: str,
) -> tuple[Path, str, FakeTmp]:
    checkout = Path(tempfile.mkdtemp(prefix="tinyhat-plugin-test-"))
    (checkout / "plugin.yaml").write_text("name: tinyhat\nversion: 0.20.1\n")
    return checkout, f"sha-for-{repo_url}-{ref}", FakeTmp()


async def _fake_checkout_same(
    _repo_url: str,
    _ref: str,
) -> tuple[Path, str, FakeTmp]:
    checkout = Path(tempfile.mkdtemp(prefix="tinyhat-plugin-test-"))
    (checkout / "plugin.yaml").write_text("name: tinyhat\nversion: 0.20.0\n")
    return checkout, "same", FakeTmp()


async def _new_ref(_repo_url: str, _ref: str) -> str:
    return "new"


async def _same_ref(_repo_url: str, _ref: str) -> str:
    return "same"


def test_install_tinyhat_plugin_installs_lts_when_missing() -> None:
    calls: list[list[str]] = []

    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del timeout_seconds, env
        calls.append(args)
        if args[1:3] == ["plugins", "install"]:
            _write_plugin(home)
        return _ok(args)

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "hermes-home"
        with (
            patch.dict(os.environ, {"TINYHAT_HERMES_HOME": str(home)}),
            patch(
                "hermes_runtime.plugin_manager.find_hermes_binary",
                return_value=Path("/usr/local/bin/hermes"),
            ),
            patch("hermes_runtime.plugin_manager._prepare_checkout", _fake_checkout),
            patch("hermes_runtime.plugin_manager.run_process", fake_run_process),
        ):
            result = asyncio.run(
                run_command(SimpleNamespace(), {"kind": "install_tinyhat_plugin"})
            )

    install_call = calls[1]
    assert calls[0] == ["/usr/local/bin/hermes", "plugins", "list"]
    assert install_call[:3] == ["/usr/local/bin/hermes", "plugins", "install"]
    assert install_call[3].startswith("file://")
    assert install_call[4:] == ["--enable"]
    assert calls[-2:] == [
        ["/usr/local/bin/hermes", "plugins", "enable", "tinyhat"],
        ["/usr/local/bin/hermes", "plugins", "list"],
    ]
    assert result["schema"] == "tinyhat_hermes_plugin_install_v1"
    assert result["plugin_ref"] == "channels/lts"
    assert result["plugin_repo_url"] == "https://github.com/tinyhat-ai/tinyhat.git"
    assert result["installed_before"] is False
    assert result["installed_now"] is True
    assert result["installed_after"] is True
    assert result["after"]["version"] == "0.20.0"
    assert result["after"]["source"]["ref"] == "channels/lts"


def test_install_tinyhat_plugin_noops_when_present_but_enables() -> None:
    calls: list[list[str]] = []

    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del timeout_seconds, env
        calls.append(args)
        return _ok(args)

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "hermes-home"
        _write_plugin(
            home,
            version="0.20.0",
            source={
                "repo_url": "https://github.com/tinyhat-ai/tinyhat.git",
                "ref": "channels/lts",
                "commit": "abc123",
            },
        )
        with (
            patch.dict(os.environ, {"TINYHAT_HERMES_HOME": str(home)}),
            patch(
                "hermes_runtime.plugin_manager.find_hermes_binary",
                return_value=Path("/usr/local/bin/hermes"),
            ),
            patch("hermes_runtime.plugin_manager.run_process", fake_run_process),
        ):
            result = asyncio.run(
                run_command(SimpleNamespace(), {"kind": "install_tinyhat_plugin"})
            )

    assert calls == [
        ["/usr/local/bin/hermes", "plugins", "list"],
        ["/usr/local/bin/hermes", "plugins", "enable", "tinyhat"],
        ["/usr/local/bin/hermes", "plugins", "list"],
    ]
    assert result["installed_before"] is True
    assert result["installed_now"] is False
    assert result["changed"] is False
    assert result["after"]["version"] == "0.20.0"


def test_update_tinyhat_plugin_reinstalls_when_lts_commit_changes() -> None:
    calls: list[list[str]] = []

    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del timeout_seconds, env
        calls.append(args)
        if args[1:3] == ["plugins", "install"]:
            _write_plugin(home, version="0.20.1")
        return _ok(args)

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "hermes-home"
        _write_plugin(
            home,
            version="0.20.0",
            source={
                "repo_url": "https://github.com/tinyhat-ai/tinyhat.git",
                "ref": "channels/lts",
                "commit": "old",
            },
        )
        with (
            patch.dict(os.environ, {"TINYHAT_HERMES_HOME": str(home)}),
            patch(
                "hermes_runtime.plugin_manager.find_hermes_binary",
                return_value=Path("/usr/local/bin/hermes"),
            ),
            patch("hermes_runtime.plugin_manager._resolve_ref", _new_ref),
            patch("hermes_runtime.plugin_manager._prepare_checkout", _fake_checkout_0201),
            patch("hermes_runtime.plugin_manager.run_process", fake_run_process),
        ):
            result = asyncio.run(
                run_command(SimpleNamespace(), {"kind": "update_tinyhat_plugin"})
            )

    install_call = calls[1]
    assert install_call[:3] == ["/usr/local/bin/hermes", "plugins", "install"]
    assert install_call[3].startswith("file://")
    assert install_call[4:] == ["--enable", "--force"]
    assert result["schema"] == "tinyhat_hermes_plugin_update_v1"
    assert result["updated_now"] is True
    assert result["target_version"] == "0.20.1"
    assert result["after"]["version"] == "0.20.1"
    assert result["after_status"]["update_available"] is False


def test_update_tinyhat_plugin_skips_when_lts_commit_is_current() -> None:
    calls: list[list[str]] = []

    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del timeout_seconds, env
        calls.append(args)
        return _ok(args)

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "hermes-home"
        _write_plugin(
            home,
            version="0.20.0",
            source={
                "repo_url": "https://github.com/tinyhat-ai/tinyhat.git",
                "ref": "channels/lts",
                "commit": "same",
            },
        )
        with (
            patch.dict(os.environ, {"TINYHAT_HERMES_HOME": str(home)}),
            patch(
                "hermes_runtime.plugin_manager.find_hermes_binary",
                return_value=Path("/usr/local/bin/hermes"),
            ),
            patch("hermes_runtime.plugin_manager._resolve_ref", _same_ref),
            patch("hermes_runtime.plugin_manager._prepare_checkout", _fake_checkout_same),
            patch("hermes_runtime.plugin_manager.run_process", fake_run_process),
        ):
            result = asyncio.run(
                run_command(SimpleNamespace(), {"kind": "update_tinyhat_plugin"})
            )

    assert calls == [
        ["/usr/local/bin/hermes", "plugins", "list"],
        ["/usr/local/bin/hermes", "plugins", "enable", "tinyhat"],
        ["/usr/local/bin/hermes", "plugins", "list"],
    ]
    assert result["updated_now"] is False
    assert result["changed"] is False
    assert result["target_commit"] == "same"


def test_update_tinyhat_plugin_installs_when_missing() -> None:
    calls: list[list[str]] = []

    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del timeout_seconds, env
        calls.append(args)
        if args[1:3] == ["plugins", "install"]:
            _write_plugin(home, version="0.20.0")
        return _ok(args)

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "hermes-home"
        with (
            patch.dict(os.environ, {"TINYHAT_HERMES_HOME": str(home)}),
            patch(
                "hermes_runtime.plugin_manager.find_hermes_binary",
                return_value=Path("/usr/local/bin/hermes"),
            ),
            patch("hermes_runtime.plugin_manager._resolve_ref", _new_ref),
            patch("hermes_runtime.plugin_manager._prepare_checkout", _fake_checkout),
            patch("hermes_runtime.plugin_manager.run_process", fake_run_process),
        ):
            result = asyncio.run(
                run_command(SimpleNamespace(), {"kind": "update_tinyhat_plugin"})
            )

    assert any(call[:3] == ["/usr/local/bin/hermes", "plugins", "install"] for call in calls)
    assert result["installed_before"] is False
    assert result["installed_now"] is True
    assert result["updated_now"] is True


def test_tinyhat_plugin_status_reports_current_and_target() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "hermes-home"
        _write_plugin(
            home,
            version="0.20.0",
            source={
                "repo_url": "https://github.com/tinyhat-ai/tinyhat.git",
                "ref": "channels/lts",
                "commit": "old",
            },
        )
        with (
            patch.dict(os.environ, {"TINYHAT_HERMES_HOME": str(home)}),
            patch("hermes_runtime.plugin_manager._prepare_checkout", _fake_checkout_0201),
        ):
            result = asyncio.run(
                run_command(SimpleNamespace(), {"kind": "tinyhat_plugin_status"})
            )

    assert result["schema"] == "tinyhat_hermes_plugin_status_v1"
    assert result["installed_version"] == "0.20.0"
    assert result["target_version"] == "0.20.1"
    assert result["update_available"] is True
    assert result["decision"] == "target_ref_changed"


def test_check_tinyhat_plugin_update_reports_no_update_when_current() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "hermes-home"
        source = {
            "repo_url": "https://github.com/tinyhat-ai/tinyhat.git",
            "ref": "channels/lts",
            "commit": "sha-for-https://github.com/tinyhat-ai/tinyhat.git-channels/lts",
        }
        _write_plugin(home, version="0.20.0", source=source)
        with (
            patch.dict(os.environ, {"TINYHAT_HERMES_HOME": str(home)}),
            patch("hermes_runtime.plugin_manager._prepare_checkout", _fake_checkout),
        ):
            result = asyncio.run(
                run_command(
                    SimpleNamespace(),
                    {"kind": "check_tinyhat_plugin_update"},
                )
            )

    assert result["schema"] == "tinyhat_hermes_plugin_update_check_v1"
    assert result["installed_version"] == "0.20.0"
    assert result["target_version"] == "0.20.0"
    assert result["update_available"] is False
    assert result["decision"] == "installed_matches_target"


def test_plugin_commands_fail_clearly_when_hermes_missing() -> None:
    with patch("hermes_runtime.plugin_manager.find_hermes_binary", return_value=None):
        with unittest.TestCase().assertRaisesRegex(RuntimeError, "install_hermes"):
            asyncio.run(
                run_command(SimpleNamespace(), {"kind": "install_tinyhat_plugin"})
            )
