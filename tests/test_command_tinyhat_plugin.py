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
from hermes_runtime.plugin_manager import (  # noqa: E402
    _prepare_checkout,
    plugin_target_selection,
)


TARGET_COMMIT = "a" * 40
NEW_TARGET_COMMIT = "b" * 40


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
    *,
    fallback_ref: str | None = None,
) -> tuple[Path, str, FakeTmp]:
    del fallback_ref
    checkout = Path(tempfile.mkdtemp(prefix="tinyhat-plugin-test-"))
    (checkout / "plugin.yaml").write_text("name: tinyhat\nversion: 0.20.0\n")
    return checkout, TARGET_COMMIT, FakeTmp()


async def _fake_checkout_0201(
    repo_url: str,
    ref: str,
    *,
    fallback_ref: str | None = None,
) -> tuple[Path, str, FakeTmp]:
    del fallback_ref
    checkout = Path(tempfile.mkdtemp(prefix="tinyhat-plugin-test-"))
    (checkout / "plugin.yaml").write_text("name: tinyhat\nversion: 0.20.1\n")
    return checkout, NEW_TARGET_COMMIT, FakeTmp()


async def _fake_checkout_same(
    _repo_url: str,
    _ref: str,
    *,
    fallback_ref: str | None = None,
) -> tuple[Path, str, FakeTmp]:
    del fallback_ref
    checkout = Path(tempfile.mkdtemp(prefix="tinyhat-plugin-test-"))
    (checkout / "plugin.yaml").write_text("name: tinyhat\nversion: 0.20.0\n")
    return checkout, "same", FakeTmp()


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


def test_plugin_target_precedence_and_malformed_installed_metadata() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "hermes-home"
        _write_plugin(
            home,
            source={
                "repo_url": "https://github.com/installed/plugin.git",
                "ref": "channels/latest",
                "commit": "old",
            },
        )
        with (
            patch.dict(
                os.environ,
                {
                    "TINYHAT_HERMES_HOME": str(home),
                    "TINYHAT_PLUGIN_REPO_URL": "https://github.com/environment/plugin.git",
                    "TINYHAT_PLUGIN_REF": "environment-ref",
                },
                clear=True,
            )
        ):
            explicit = plugin_target_selection(
                {
                    "spec": {
                        "plugin_repo_url": "https://github.com/explicit/plugin.git",
                        "plugin_ref": "explicit-ref",
                    }
                }
            )
            explicit_ref_only = plugin_target_selection(
                {"spec": {"plugin_ref": "explicit-ref-only"}}
            )
            explicit_repo_only = plugin_target_selection(
                {
                    "spec": {
                        "plugin_repo_url": "https://github.com/explicit-only/plugin.git"
                    }
                }
            )
            environment = plugin_target_selection({})

        with (
            patch.dict(
                os.environ,
                {"TINYHAT_HERMES_HOME": str(home)},
                clear=True,
            )
        ):
            installed = plugin_target_selection({})
            explicit_without_environment = plugin_target_selection(
                {"spec": {"plugin_ref": "explicit-without-environment"}}
            )
        preserved_refs = []
        for installed_ref in ("v0.21.3", TARGET_COMMIT):
            _write_plugin(
                home,
                source={
                    "repo_url": "https://github.com/tinyhat-ai/tinyhat.git",
                    "ref": installed_ref,
                    "commit": TARGET_COMMIT,
                },
            )
            with patch.dict(
                os.environ,
                {"TINYHAT_HERMES_HOME": str(home)},
                clear=True,
            ):
                preserved_refs.append(plugin_target_selection({})["ref"])
        _write_plugin(
            home,
            source={
                "repo_url": "https://github.com/malformed/plugin.git",
                "ref": ["not", "a", "string"],
                "commit": "old",
            },
        )
        with (
            patch.dict(
                os.environ,
                {"TINYHAT_HERMES_HOME": str(home)},
                clear=True,
            )
        ):
            fallback = plugin_target_selection({})

    assert explicit == {
        "source": "spec",
        "repo_url": "https://github.com/explicit/plugin.git",
        "ref": "explicit-ref",
    }
    assert environment["source"] == "environment"
    assert environment["ref"] == "environment-ref"
    assert explicit_ref_only == {
        "source": "spec",
        "repo_url": "https://github.com/environment/plugin.git",
        "ref": "explicit-ref-only",
    }
    assert explicit_repo_only == {
        "source": "spec",
        "repo_url": "https://github.com/explicit-only/plugin.git",
        "ref": "environment-ref",
    }
    assert installed["source"] == "installed_metadata"
    assert installed["ref"] == "channels/latest"
    assert explicit_without_environment == {
        "source": "spec",
        "repo_url": "https://github.com/tinyhat-ai/tinyhat.git",
        "ref": "explicit-without-environment",
    }
    assert preserved_refs == ["v0.21.3", TARGET_COMMIT]
    assert fallback == {
        "source": "default",
        "repo_url": "https://github.com/tinyhat-ai/tinyhat.git",
        "ref": "channels/lts",
    }


def test_plugin_target_treats_whitespace_env_as_unset_and_rejects_option_refs(
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "hermes-home"
        _write_plugin(
            home,
            source={
                "repo_url": "https://github.com/installed/plugin.git",
                "ref": "channels/latest",
                "commit": TARGET_COMMIT,
            },
        )
        with patch.dict(
            os.environ,
            {
                "TINYHAT_HERMES_HOME": str(home),
                "TINYHAT_PLUGIN_REPO_URL": "   ",
                "TINYHAT_PLUGIN_REF": "\t",
            },
            clear=True,
        ):
            whitespace_fallback = plugin_target_selection({})

        with (
            patch.dict(
                os.environ,
                {
                    "TINYHAT_HERMES_HOME": str(home),
                    "TINYHAT_PLUGIN_REF": "--upload-pack=malicious",
                },
                clear=True,
            ),
            unittest.TestCase().assertRaisesRegex(ValueError, "malformed"),
        ):
            plugin_target_selection({})

        with unittest.TestCase().assertRaisesRegex(ValueError, "malformed"):
            plugin_target_selection({"spec": {"plugin_ref": "--help"}})

    assert whitespace_fallback == {
        "source": "installed_metadata",
        "repo_url": "https://github.com/installed/plugin.git",
        "ref": "channels/latest",
    }


def test_exact_commit_checkout_skips_branch_clone() -> None:
    calls: list[list[str]] = []

    async def fake_git(
        args: list[str],
        *,
        timeout_seconds: int = 120,
    ) -> dict[str, object]:
        del timeout_seconds
        calls.append(args)
        result = _ok(args)
        if args[-2:] == ["rev-parse", "HEAD"]:
            result["stdout"] = TARGET_COMMIT + "\n"
        return result

    with patch("hermes_runtime.plugin_manager._git", fake_git):
        _checkout, commit, temporary = asyncio.run(
            _prepare_checkout(
                "https://github.com/tinyhat-ai/tinyhat.git",
                TARGET_COMMIT,
                fallback_ref="channels/lts",
            )
        )
        temporary.cleanup()

    assert commit == TARGET_COMMIT
    assert not any("clone" in call for call in calls)
    fetches = [call for call in calls if "fetch" in call]
    assert [call[-1] for call in fetches] == [TARGET_COMMIT]


def test_exact_commit_checkout_falls_back_to_logical_ref_without_drift() -> None:
    calls: list[list[str]] = []

    async def fake_git(
        args: list[str],
        *,
        timeout_seconds: int = 120,
    ) -> dict[str, object]:
        del timeout_seconds
        calls.append(args)
        result = _ok(args)
        if "fetch" in args and args[-1] == TARGET_COMMIT:
            result["ok"] = False
            result["returncode"] = 128
            result["stderr"] = "server does not allow request for unadvertised object"
        if args[-2:] == ["rev-parse", "HEAD"]:
            result["stdout"] = TARGET_COMMIT + "\n"
        return result

    with patch("hermes_runtime.plugin_manager._git", fake_git):
        _checkout, commit, temporary = asyncio.run(
            _prepare_checkout(
                "https://github.com/tinyhat-ai/tinyhat.git",
                TARGET_COMMIT,
                fallback_ref="channels/lts",
            )
        )
        temporary.cleanup()

    assert commit == TARGET_COMMIT
    fetches = [call for call in calls if "fetch" in call]
    assert [call[-1] for call in fetches] == [TARGET_COMMIT, "channels/lts"]


def test_exact_commit_checkout_rejects_logical_ref_drift() -> None:
    async def fake_git(
        args: list[str],
        *,
        timeout_seconds: int = 120,
    ) -> dict[str, object]:
        del timeout_seconds
        result = _ok(args)
        if "fetch" in args and args[-1] == TARGET_COMMIT:
            result["ok"] = False
            result["returncode"] = 128
        if args[-2:] == ["rev-parse", "HEAD"]:
            result["stdout"] = NEW_TARGET_COMMIT + "\n"
        return result

    with (
        patch("hermes_runtime.plugin_manager._git", fake_git),
        unittest.TestCase().assertRaisesRegex(
            RuntimeError,
            "did not match the exact commit",
        ),
    ):
        asyncio.run(
            _prepare_checkout(
                "https://github.com/tinyhat-ai/tinyhat.git",
                TARGET_COMMIT,
                fallback_ref="channels/lts",
            )
        )


def test_plugin_status_uses_one_coherent_target_selection() -> None:
    lts_source = {
        "repo_url": "https://github.com/tinyhat-ai/tinyhat.git",
        "ref": "channels/lts",
        "commit": "old",
    }
    latest_source = {**lts_source, "ref": "channels/latest", "commit": "new"}
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "hermes-home"
        _write_plugin(home, source=lts_source)
        with (
            patch.dict(
                os.environ,
                {"TINYHAT_HERMES_HOME": str(home)},
                clear=True,
            ),
            patch(
                "hermes_runtime.plugin_manager._read_source_metadata",
                side_effect=[lts_source, latest_source, latest_source],
            ) as read_source,
            patch(
                "hermes_runtime.plugin_manager._prepare_checkout",
                _fake_checkout_0201,
            ),
        ):
            result = asyncio.run(
                run_command(SimpleNamespace(), {"kind": "tinyhat_plugin_status"})
            )

    assert read_source.call_count == 1
    assert result["installed"]["source"] == lts_source
    assert result["plugin_ref"] == "channels/lts"
    assert result["target_selection"]["plugin_ref"] == "channels/lts"
    assert result["target"]["ref"] == "channels/lts"


def test_update_tinyhat_plugin_installs_exact_commit_and_keeps_logical_ref() -> None:
    calls: list[list[str]] = []
    checkout_refs: list[str] = []

    async def fake_checkout(
        repo_url: str,
        ref: str,
        *,
        fallback_ref: str | None = None,
    ) -> tuple[Path, str, FakeTmp]:
        del repo_url, fallback_ref
        checkout_refs.append(ref)
        checkout = Path(tempfile.mkdtemp(prefix="tinyhat-plugin-test-"))
        (checkout / "plugin.yaml").write_text(
            "name: tinyhat\nversion: 0.20.1\n",
            encoding="utf-8",
        )
        return checkout, NEW_TARGET_COMMIT, FakeTmp()

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
            source={
                "repo_url": "https://github.com/tinyhat-ai/tinyhat.git",
                "ref": "channels/latest",
                "commit": "old",
            },
        )
        command = {
            "kind": "update_tinyhat_plugin",
            "spec": {
                "plugin_repo_url": "https://github.com/tinyhat-ai/tinyhat.git",
                "plugin_ref": "channels/latest",
                "target_sha": NEW_TARGET_COMMIT,
            },
        }
        with (
            patch.dict(
                os.environ,
                {"TINYHAT_HERMES_HOME": str(home)},
                clear=True,
            ),
            patch(
                "hermes_runtime.plugin_manager.find_hermes_binary",
                return_value=Path("/usr/local/bin/hermes"),
            ),
            patch("hermes_runtime.plugin_manager._prepare_checkout", fake_checkout),
            patch("hermes_runtime.plugin_manager.run_process", fake_run_process),
        ):
            result = asyncio.run(run_command(SimpleNamespace(), command))
            retry = asyncio.run(run_command(SimpleNamespace(), command))

    assert result["updated_now"] is True
    assert result["target_commit"] == NEW_TARGET_COMMIT
    assert result["after"]["source"] == {
        "repo_url": "https://github.com/tinyhat-ai/tinyhat.git",
        "ref": "channels/latest",
        "commit": NEW_TARGET_COMMIT,
    }
    assert checkout_refs == [NEW_TARGET_COMMIT] * 5
    install_calls = [call for call in calls if call[1:3] == ["plugins", "install"]]
    assert len(install_calls) == 1
    assert retry["updated_now"] is False
    assert retry["changed"] is False


def test_update_tinyhat_plugin_installs_commit_checked_before_channel_moves() -> None:
    checked_commit = "c" * 40
    moved_commit = "d" * 40
    checkout_refs: list[str] = []
    channel_checks = 0

    async def fake_checkout(
        _repo_url: str,
        ref: str,
        *,
        fallback_ref: str | None = None,
    ) -> tuple[Path, str, FakeTmp]:
        del fallback_ref
        nonlocal channel_checks
        checkout_refs.append(ref)
        checkout = Path(tempfile.mkdtemp(prefix="tinyhat-plugin-test-"))
        if ref == "channels/lts":
            channel_checks += 1
            commit = checked_commit if channel_checks == 1 else moved_commit
            version = "0.20.1" if channel_checks == 1 else "0.20.2"
        else:
            commit = checked_commit
            version = "0.20.1"
        (checkout / "plugin.yaml").write_text(
            f"name: tinyhat\nversion: {version}\n",
            encoding="utf-8",
        )
        return checkout, commit, FakeTmp()

    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del timeout_seconds, env
        if args[1:3] == ["plugins", "install"]:
            _write_plugin(home, version="0.20.1")
        return _ok(args)

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "hermes-home"
        _write_plugin(
            home,
            source={
                "repo_url": "https://github.com/tinyhat-ai/tinyhat.git",
                "ref": "channels/lts",
                "commit": "old",
            },
        )
        with (
            patch.dict(
                os.environ,
                {"TINYHAT_HERMES_HOME": str(home)},
                clear=True,
            ),
            patch(
                "hermes_runtime.plugin_manager.find_hermes_binary",
                return_value=Path("/usr/local/bin/hermes"),
            ),
            patch("hermes_runtime.plugin_manager._prepare_checkout", fake_checkout),
            patch("hermes_runtime.plugin_manager.run_process", fake_run_process),
        ):
            result = asyncio.run(
                run_command(SimpleNamespace(), {"kind": "update_tinyhat_plugin"})
            )

    assert checkout_refs == ["channels/lts", checked_commit, "channels/lts"]
    assert result["after"]["source"]["ref"] == "channels/lts"
    assert result["after"]["source"]["commit"] == checked_commit
    assert result["after_status"]["target_commit"] == moved_commit
    assert result["update_available_after"] is True


def test_check_tinyhat_plugin_update_reports_no_update_when_current() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "hermes-home"
        source = {
            "repo_url": "https://github.com/tinyhat-ai/tinyhat.git",
            "ref": "channels/lts",
            "commit": "same",
        }
        _write_plugin(home, version="0.20.0", source=source)
        with (
            patch.dict(os.environ, {"TINYHAT_HERMES_HOME": str(home)}),
            patch("hermes_runtime.plugin_manager._prepare_checkout", _fake_checkout_same),
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


def test_install_tinyhat_plugin_reports_cli_success_without_manifest() -> None:
    async def fake_run_process(
        args: list[str],
        *,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        del timeout_seconds, env
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
            unittest.TestCase().assertRaisesRegex(
                RuntimeError,
                "reported plugin install success",
            ),
        ):
            asyncio.run(
                run_command(SimpleNamespace(), {"kind": "install_tinyhat_plugin"})
            )


def test_plugin_commands_fail_clearly_when_hermes_missing() -> None:
    with patch("hermes_runtime.plugin_manager.find_hermes_binary", return_value=None):
        with unittest.TestCase().assertRaisesRegex(RuntimeError, "install_hermes"):
            asyncio.run(
                run_command(SimpleNamespace(), {"kind": "install_tinyhat_plugin"})
            )
