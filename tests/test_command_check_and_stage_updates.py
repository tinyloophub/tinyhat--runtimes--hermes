"""Focused tests for the combined Tinyhat update command."""

from __future__ import annotations

import asyncio
import http.client
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hermes_runtime import update_check  # noqa: E402
from hermes_runtime.commands import run_command  # noqa: E402
from hermes_runtime.main import _run_one_command  # noqa: E402
from hermes_runtime.update_orchestrator import (  # noqa: E402
    acknowledge_check_and_stage_result,
)
from hermes_runtime.update_check import run_update_check  # noqa: E402


RUNTIME_SHA = "a" * 40
PLUGIN_COMMIT = "b" * 40
OLD_PLUGIN_COMMIT = "c" * 40


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


class FakeContext:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.restart_requested = False
        self.staged_version_file = state_dir / "staged" / "VERSION"
        self.staged_metadata_file = state_dir / "staged" / "metadata.json"
        self.activation_marker = state_dir / "ACTIVATE_ON_RESTART"

    def current_version(self) -> str:
        return "v0.0.44"

    def current_commit_sha(self) -> str:
        return "d" * 40

    def staged_version(self) -> str | None:
        try:
            return self.staged_version_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None


class FakePlatform:
    def __init__(self) -> None:
        self.fail_results = False
        self.ignore_results = False
        self.posts: list[tuple[str, dict[str, Any]]] = []

    async def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.fail_results:
            raise RuntimeError("result delivery failed")
        self.posts.append((path, payload))
        return {
            "computer_id": 1,
            "ignored": self.ignore_results,
            "reason": "stale_command" if self.ignore_results else None,
            "ledger": {},
        }


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def read(self, limit: int = -1) -> bytes:
        return self.body if limit < 0 else self.body[:limit]


def _spec() -> dict[str, Any]:
    return {
        "reason": "admin_check_and_stage_updates",
        "channel": "lts",
        "target_ref": "v0.0.45",
        "target_sha": RUNTIME_SHA,
        "plugin_repo_url": "https://github.com/tinyhat-ai/tinyhat.git",
        "plugin_ref": "channels/lts",
        "target_commit": PLUGIN_COMMIT,
    }


def _discovery(*, runtime: bool, plugin: bool) -> dict[str, Any]:
    return {
        "schema": "tinyhat_hermes_update_check_v1",
        "current_version": "v0.0.44",
        "current_code_version": "0.0.44",
        "current_sha": "d" * 40,
        "target_ref": "v0.0.45",
        "target_sha": RUNTIME_SHA,
        "update_available": runtime,
        "plugin_update_check": {
            "installed_version": "0.21.5",
            "installed_commit": OLD_PLUGIN_COMMIT,
            "target_version": "0.21.6",
            "target_commit": PLUGIN_COMMIT,
            "update_available": plugin,
            "installed": {
                "installed": True,
                "version": "0.21.5",
                "source": {
                    "repo_url": "https://github.com/tinyhat-ai/tinyhat.git",
                    "ref": "channels/lts",
                    "commit": OLD_PLUGIN_COMMIT,
                },
            },
        },
    }


async def _fake_stage(ctx: FakeContext, command: dict[str, Any]) -> dict[str, Any]:
    spec = command["spec"]
    ctx.staged_version_file.parent.mkdir(parents=True, exist_ok=True)
    ctx.staged_version_file.write_text(spec["target_ref"] + "\n", encoding="utf-8")
    ctx.staged_metadata_file.write_text(
        json.dumps(
            {
                "target_ref": spec["target_ref"],
                "target_sha": spec["target_sha"],
            }
        ),
        encoding="utf-8",
    )
    return {
        "code_staged": True,
        "target_ref": spec["target_ref"],
        "package_dir": "/private/staged/runtime",
    }


def _plugin_result(*, changed: bool) -> dict[str, Any]:
    return {
        "changed": changed,
        "updated_now": changed,
        "after": {
            "installed": True,
            "version": "0.21.6" if changed else "0.21.5",
            "plugin_dir": "/private/hermes/plugin",
            "source": {
                "repo_url": "https://github.com/tinyhat-ai/tinyhat.git",
                "ref": "channels/lts",
                "commit": PLUGIN_COMMIT if changed else OLD_PLUGIN_COMMIT,
            },
        },
        "commands": {"install": {"stdout": "private diagnostic"}},
    }


def _run(
    ctx: FakeContext,
    *,
    runtime: bool,
    plugin: bool,
    plugin_result: dict[str, Any] | None = None,
    discovery_result: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], AsyncMock, AsyncMock, list[str]]:
    stage = AsyncMock(side_effect=_fake_stage)
    update_plugin = AsyncMock(
        return_value=plugin_result or _plugin_result(changed=plugin)
    )
    messages: list[str] = []

    def fake_send(text: str) -> dict[str, Any]:
        messages.append(text)
        return {"ok": True, "http_status": 200}

    with (
        patch(
            "hermes_runtime.update_orchestrator.run_update_check",
            AsyncMock(
                return_value=(
                    discovery_result
                    if discovery_result is not None
                    else _discovery(runtime=runtime, plugin=plugin)
                )
            ),
        ),
        patch("hermes_runtime.update_orchestrator.stage_update.run", stage),
        patch(
            "hermes_runtime.update_orchestrator.update_tinyhat_plugin",
            update_plugin,
        ),
        patch("hermes_runtime.update_orchestrator._telegram_send", fake_send),
    ):
        result = asyncio.run(
            run_command(
                ctx,
                {"kind": "check_and_stage_updates", "spec": _spec()},
            )
        )
    return result, stage, update_plugin, messages


def test_runtime_only_spec_stages_without_plugin_discovery() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctx = FakeContext(Path(tmp))
        discovery = _discovery(runtime=True, plugin=False)
        discovery.pop("plugin_update_check")
        run_check = AsyncMock(return_value=discovery)
        stage = AsyncMock(side_effect=_fake_stage)
        update_plugin = AsyncMock()
        messages: list[str] = []

        def fake_send(text: str) -> dict[str, Any]:
            messages.append(text)
            return {"ok": True, "http_status": 200}

        runtime_only_spec = {
            "reason": "scheduled_update",
            "auto_update_run_id": "scheduled:runtime-only",
            "channel": "lts",
            "target_ref": "v0.0.45",
            "target_sha": RUNTIME_SHA,
        }
        with (
            patch(
                "hermes_runtime.update_orchestrator.run_update_check",
                run_check,
            ),
            patch("hermes_runtime.update_orchestrator.stage_update.run", stage),
            patch(
                "hermes_runtime.update_orchestrator.update_tinyhat_plugin",
                update_plugin,
            ),
            patch("hermes_runtime.update_orchestrator._telegram_send", fake_send),
        ):
            result = asyncio.run(
                run_command(
                    ctx,
                    {"kind": "check_and_stage_updates", "spec": runtime_only_spec},
                )
            )

    assert result["status"] == "updated"
    assert result["runtime"]["status"] == "staged"
    assert result["plugin"]["status"] == "not_requested"
    assert result["plugin"]["changed"] is False
    assert result["hermes_restart_required"] is False
    assert result["runtime_restart_requested"] is True
    assert messages == [
        "Tinyhat runtime update staged to version v0.0.45.\n\n"
        "It will be activated automatically."
    ]
    update_plugin.assert_not_awaited()
    assert run_check.await_args.kwargs["include_plugin_check"] is False


def test_partial_plugin_target_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctx = FakeContext(Path(tmp))
        partial = {
            "reason": "scheduled_update",
            "channel": "lts",
            "target_ref": "v0.0.45",
            "target_sha": RUNTIME_SHA,
            "plugin_repo_url": "https://github.com/tinyhat-ai/tinyhat.git",
        }
        with unittest.TestCase().assertRaisesRegex(
            ValueError,
            "must be provided together",
        ):
            asyncio.run(
                run_command(
                    ctx,
                    {"kind": "check_and_stage_updates", "spec": partial},
                )
            )


def test_no_updates_is_a_true_noop() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctx = FakeContext(Path(tmp))
        result, stage, update_plugin, messages = _run(
            ctx,
            runtime=False,
            plugin=False,
        )

    assert result["status"] == "current"
    assert result["changed"] is False
    assert result["runtime_restart_requested"] is False
    assert result["hermes_restart_required"] is False
    assert result["notification"]["attempted"] is False
    assert ctx.restart_requested is False
    stage.assert_not_awaited()
    update_plugin.assert_not_awaited()
    assert messages == []


def test_plugin_only_update_requests_runtime_restart_and_notifies() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctx = FakeContext(Path(tmp))
        result, stage, update_plugin, messages = _run(
            ctx,
            runtime=False,
            plugin=True,
        )

    stage.assert_not_awaited()
    update_plugin.assert_awaited_once()
    command = update_plugin.await_args.args[0]
    assert command["spec"]["target_commit"] == PLUGIN_COMMIT
    assert result["plugin"]["changed"] is True
    assert result["plugin"]["installed_version"] == "0.21.6"
    assert result["plugin"]["installed_commit"] == PLUGIN_COMMIT
    assert result["plugin"]["installed"] == {
        "installed": True,
        "version": "0.21.6",
        "source": {
            "repo_url": "https://github.com/tinyhat-ai/tinyhat.git",
            "ref": "channels/lts",
            "commit": PLUGIN_COMMIT,
        },
    }
    assert result["runtime"]["changed"] is False
    assert result["runtime_restart_requested"] is True
    assert result["hermes_restart_required"] is True
    assert ctx.restart_requested is True
    assert messages == [
        "Tinyhat capabilities updated to version 0.21.6.\n\n"
        "The new capabilities will be picked up after the next Hermes /restart.\n\n"
        "To use them now, run /restart."
    ]
    serialized = json.dumps(result, sort_keys=True)
    assert "/private/" not in serialized
    assert "private diagnostic" not in serialized


def test_runtime_only_update_stages_exact_sha_and_marks_activation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctx = FakeContext(Path(tmp))
        result, stage, update_plugin, messages = _run(
            ctx,
            runtime=True,
            plugin=False,
        )
        marker = ctx.activation_marker.read_text(encoding="utf-8").strip()

    stage.assert_awaited_once()
    stage_spec = stage.await_args.args[1]["spec"]
    assert stage_spec["target_ref"] == "v0.0.45"
    assert stage_spec["target_sha"] == RUNTIME_SHA
    update_plugin.assert_not_awaited()
    assert marker == "v0.0.45"
    assert result["runtime"]["changed"] is True
    assert result["runtime"]["activation_requested"] is True
    assert result["plugin"]["changed"] is False
    assert result["runtime_restart_requested"] is True
    assert result["hermes_restart_required"] is False
    assert result["notification"]["sent"] is True
    assert messages == [
        "Tinyhat runtime update staged to version v0.0.45.\n\n"
        "It will be activated automatically."
    ]


def test_runtime_notice_waits_for_activation_marker_and_retries() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctx = FakeContext(Path(tmp))
        blocker = ctx.state_dir / "activation-parent"
        blocker.write_text("not a directory", encoding="utf-8")
        ctx.activation_marker = blocker / "ACTIVATE_ON_RESTART"

        failed, first_stage, _, first_messages = _run(
            ctx,
            runtime=True,
            plugin=False,
        )
        blocker.unlink()
        recovered, retry_stage, _, retry_messages = _run(
            ctx,
            runtime=True,
            plugin=False,
        )

    first_stage.assert_awaited_once()
    assert failed["status"] == "failed"
    assert failed["runtime"]["error"] is not None
    assert failed["runtime"]["activation_requested"] is False
    assert first_messages == []
    retry_stage.assert_not_awaited()
    assert recovered["status"] == "updated"
    assert recovered["runtime"]["status"] == "staged"
    assert recovered["runtime"]["activation_requested"] is True
    assert retry_messages == [
        "Tinyhat runtime update staged to version v0.0.45.\n\n"
        "It will be activated automatically."
    ]


def test_partial_activation_keeps_requesting_runtime_restart() -> None:
    """A surviving activation marker wins over a now-current state VERSION."""

    discovery = _discovery(runtime=False, plugin=False)
    discovery.update(
        {
            "current_version": "v0.0.45",
            "current_code_version": "0.0.44",
            "current_sha": "d" * 40,
            "decision": "current_matches_target",
        }
    )
    with tempfile.TemporaryDirectory() as tmp:
        ctx = FakeContext(Path(tmp))
        ctx.activation_marker.write_text("v0.0.45\n", encoding="utf-8")
        result, stage, update_plugin, messages = _run(
            ctx,
            runtime=False,
            plugin=False,
            discovery_result=discovery,
        )

    stage.assert_not_awaited()
    update_plugin.assert_not_awaited()
    assert result["status"] == "updated"
    assert result["runtime"]["status"] == "staged"
    assert result["runtime"]["update_available"] is False
    assert result["runtime"]["activation_requested"] is True
    assert result["runtime_restart_requested"] is True
    assert result["hermes_restart_required"] is False
    assert ctx.restart_requested is True
    assert messages == [
        "Tinyhat runtime update staged to version v0.0.45.\n\n"
        "It will be activated automatically."
    ]


def test_runtime_and_plugin_updates_both_apply() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctx = FakeContext(Path(tmp))
        result, stage, update_plugin, messages = _run(
            ctx,
            runtime=True,
            plugin=True,
        )

    stage.assert_awaited_once()
    update_plugin.assert_awaited_once()
    assert result["status"] == "updated"
    assert result["runtime"]["changed"] is True
    assert result["plugin"]["changed"] is True
    assert result["notification"]["sent"] is True
    assert len(messages) == 1


def test_plugin_update_without_version_keeps_exact_installed_commit() -> None:
    plugin_result = {
        "changed": True,
        "after": {
            "installed": True,
            "source": {
                "repo_url": "https://github.com/tinyhat-ai/tinyhat.git",
                "ref": "channels/lts",
                "commit": PLUGIN_COMMIT,
            },
        },
    }
    with tempfile.TemporaryDirectory() as tmp:
        result, _stage, _update_plugin, _messages = _run(
            FakeContext(Path(tmp)),
            runtime=False,
            plugin=True,
            plugin_result=plugin_result,
        )

    assert result["plugin"]["installed_commit"] == PLUGIN_COMMIT


def test_plugin_source_metadata_without_manifest_is_not_reported_as_installed() -> None:
    incomplete = _plugin_result(changed=True)
    incomplete["after"].update({"installed": False, "version": None})
    with tempfile.TemporaryDirectory() as tmp:
        ctx = FakeContext(Path(tmp))
        result, _, update_plugin, messages = _run(
            ctx,
            runtime=False,
            plugin=True,
            plugin_result=incomplete,
        )

    update_plugin.assert_awaited_once()
    assert result["status"] == "failed"
    assert result["plugin"]["status"] == "failed"
    assert result["plugin"]["changed"] is False
    assert result["plugin"]["installed_version"] == "0.21.5"
    assert result["plugin"]["installed_commit"] == OLD_PLUGIN_COMMIT
    assert result["hermes_restart_required"] is False
    assert result["notification"]["attempted"] is False
    assert messages == []


def test_notification_failure_does_not_undo_update_or_restart() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctx = FakeContext(Path(tmp))
        with (
            patch(
                "hermes_runtime.update_orchestrator.run_update_check",
                AsyncMock(return_value=_discovery(runtime=False, plugin=True)),
            ),
            patch(
                "hermes_runtime.update_orchestrator.update_tinyhat_plugin",
                AsyncMock(return_value=_plugin_result(changed=True)),
            ),
            patch(
                "hermes_runtime.update_orchestrator._telegram_send",
                return_value={"ok": False, "http_status": 503},
            ),
        ):
            result = asyncio.run(
                run_command(
                    ctx,
                    {"kind": "check_and_stage_updates", "spec": _spec()},
                )
            )
        marker_path = ctx.state_dir / "updates" / "pending_plugin_repair.json"
        marker_state = json.loads(marker_path.read_text(encoding="utf-8"))["state"]
        acknowledged = acknowledge_check_and_stage_result(
            ctx,
            {"kind": "check_and_stage_updates", "spec": _spec()},
            result,
        )
        marker_remained = marker_path.is_file()

    assert result["status"] == "updated"
    assert result["notification"] == {
        "attempted": True,
        "sent": False,
        "http_status": 503,
        "error": {
            "code": "telegram_delivery_failed",
            "message": "Telegram notification failed",
        },
    }
    assert ctx.restart_requested is True
    assert marker_state == "installed_unacknowledged"
    assert acknowledged is True
    assert marker_remained is False


def test_result_delivery_failure_redispatches_notice_and_staged_plugin_result() -> None:
    first_discovery = _discovery(runtime=False, plugin=True)
    retry_discovery = _discovery(runtime=False, plugin=False)
    retry_discovery["plugin_update_check"].update(
        {
            "installed": _plugin_result(changed=True)["after"],
            "installed_version": "0.21.6",
            "installed_commit": PLUGIN_COMMIT,
        }
    )
    update_plugin = AsyncMock(return_value=_plugin_result(changed=True))
    messages: list[str] = []

    def fake_send(text: str) -> dict[str, Any]:
        messages.append(text)
        return {"ok": True, "http_status": 200}

    with tempfile.TemporaryDirectory() as tmp:
        ctx = FakeContext(Path(tmp))
        platform = FakePlatform()
        ctx.platform = platform
        ctx.computer_id = "local-dev"
        command = {
            "command_id": "cmd-update",
            "idempotency_key": "idem-update",
            "kind": "check_and_stage_updates",
            "spec": _spec(),
        }
        marker_path = Path(tmp) / "updates" / "pending_plugin_repair.json"

        with (
            patch(
                "hermes_runtime.update_orchestrator.run_update_check",
                AsyncMock(side_effect=[first_discovery, retry_discovery]),
            ),
            patch(
                "hermes_runtime.update_orchestrator.update_tinyhat_plugin",
                update_plugin,
            ),
            patch("hermes_runtime.update_orchestrator._telegram_send", fake_send),
        ):
            platform.fail_results = True
            with unittest.TestCase().assertRaisesRegex(
                RuntimeError,
                "result delivery failed",
            ):
                asyncio.run(_run_one_command(ctx, command))

            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            assert marker["state"] == "installed_unacknowledged"
            assert marker["target_commit"] == PLUGIN_COMMIT

            platform.fail_results = False
            ctx.restart_requested = False
            asyncio.run(_run_one_command(ctx, command))

        update_plugin.assert_awaited_once()
        assert len(messages) == 2
        assert ctx.restart_requested is True
        assert marker_path.exists() is False
        assert len(platform.posts) == 1
        reported = platform.posts[0][1]["result"]
        assert reported["status"] == "applied"
        assert reported["result"]["status"] == "updated"
        assert reported["result"]["plugin"]["changed"] is True
        assert reported["result"]["plugin"]["installed_commit"] == PLUGIN_COMMIT
        assert reported["result"]["hermes_restart_required"] is True
        assert reported["result"]["notification"]["sent"] is True


def test_ignored_result_keeps_plugin_recovery_marker_for_redispatch() -> None:
    first_discovery = _discovery(runtime=False, plugin=True)
    retry_discovery = _discovery(runtime=False, plugin=False)
    retry_discovery["plugin_update_check"].update(
        {
            "installed": _plugin_result(changed=True)["after"],
            "installed_version": "0.21.6",
            "installed_commit": PLUGIN_COMMIT,
        }
    )
    update_plugin = AsyncMock(return_value=_plugin_result(changed=True))

    with tempfile.TemporaryDirectory() as tmp:
        ctx = FakeContext(Path(tmp))
        platform = FakePlatform()
        ctx.platform = platform
        ctx.computer_id = "local-dev"
        command = {
            "command_id": "cmd-update",
            "idempotency_key": "idem-update",
            "kind": "check_and_stage_updates",
            "spec": _spec(),
        }
        marker_path = Path(tmp) / "updates" / "pending_plugin_repair.json"

        with (
            patch(
                "hermes_runtime.update_orchestrator.run_update_check",
                AsyncMock(side_effect=[first_discovery, retry_discovery]),
            ),
            patch(
                "hermes_runtime.update_orchestrator.update_tinyhat_plugin",
                update_plugin,
            ),
            patch(
                "hermes_runtime.update_orchestrator._telegram_send",
                return_value={"ok": True, "http_status": 200},
            ),
        ):
            platform.ignore_results = True
            asyncio.run(_run_one_command(ctx, command))

            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            assert marker["state"] == "installed_unacknowledged"

            platform.ignore_results = False
            asyncio.run(_run_one_command(ctx, command))

        update_plugin.assert_awaited_once()
        assert marker_path.exists() is False
        assert len(platform.posts) == 2
        assert platform.posts[1][1]["result"]["result"]["plugin"]["changed"] is True


def test_plugin_update_continues_when_runtime_staging_fails() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctx = FakeContext(Path(tmp))
        update_plugin = AsyncMock(return_value=_plugin_result(changed=True))
        with (
            patch(
                "hermes_runtime.update_orchestrator.run_update_check",
                AsyncMock(return_value=_discovery(runtime=True, plugin=True)),
            ),
            patch(
                "hermes_runtime.update_orchestrator.stage_update.run",
                AsyncMock(side_effect=RuntimeError("private staging failure")),
            ),
            patch(
                "hermes_runtime.update_orchestrator.update_tinyhat_plugin",
                update_plugin,
            ),
            patch(
                "hermes_runtime.update_orchestrator._telegram_send",
                return_value={"ok": True},
            ),
        ):
            result = asyncio.run(
                run_command(
                    ctx,
                    {"kind": "check_and_stage_updates", "spec": _spec()},
                )
            )

    update_plugin.assert_awaited_once()
    assert result["status"] == "partial"
    assert result["runtime"]["changed"] is False
    assert result["runtime"]["error"] == {
        "code": "RuntimeError",
        "message": "Runtime update failed",
    }
    assert result["plugin"]["changed"] is True
    assert "private staging failure" not in json.dumps(result)
    assert ctx.restart_requested is True


def test_runtime_update_continues_when_plugin_update_fails() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctx = FakeContext(Path(tmp))
        stage = AsyncMock(side_effect=_fake_stage)
        with (
            patch(
                "hermes_runtime.update_orchestrator.run_update_check",
                AsyncMock(return_value=_discovery(runtime=True, plugin=True)),
            ),
            patch("hermes_runtime.update_orchestrator.stage_update.run", stage),
            patch(
                "hermes_runtime.update_orchestrator.update_tinyhat_plugin",
                AsyncMock(side_effect=RuntimeError("private plugin failure")),
            ),
            patch(
                "hermes_runtime.update_orchestrator._telegram_send",
                return_value={"ok": True},
            ),
        ):
            result = asyncio.run(
                run_command(
                    ctx,
                    {"kind": "check_and_stage_updates", "spec": _spec()},
                )
            )

    stage.assert_awaited_once()
    assert result["status"] == "partial"
    assert result["runtime"]["changed"] is True
    assert result["plugin"]["changed"] is False
    assert result["plugin"]["error"] == {
        "code": "RuntimeError",
        "message": "Plugin update failed",
    }
    assert "private plugin failure" not in json.dumps(result)
    assert ctx.restart_requested is True


def test_plugin_discovery_failure_is_not_reported_as_current() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctx = FakeContext(Path(tmp))
        discovery = _discovery(runtime=False, plugin=False)
        discovery["plugin_update_check"].update(
            {
                "update_available": None,
                "decision": "target_unavailable",
                "target_error": "private target failure",
            }
        )
        update_plugin = AsyncMock()
        with (
            patch(
                "hermes_runtime.update_orchestrator.run_update_check",
                AsyncMock(return_value=discovery),
            ),
            patch(
                "hermes_runtime.update_orchestrator.update_tinyhat_plugin",
                update_plugin,
            ),
        ):
            result = asyncio.run(
                run_command(
                    ctx,
                    {"kind": "check_and_stage_updates", "spec": _spec()},
                )
            )

    update_plugin.assert_not_awaited()
    assert result["status"] == "failed"
    assert result["changed"] is False
    assert result["plugin"]["status"] == "failed"
    assert result["plugin"]["error"] == {
        "code": "plugin_target_unavailable",
        "message": "Plugin update check failed",
    }
    assert "private target failure" not in json.dumps(result)
    assert result["notification"]["attempted"] is False
    assert ctx.restart_requested is False


def test_landed_plugin_failure_notifies_and_retry_repair_restarts() -> None:
    target_snapshot = _plugin_result(changed=True)["after"]
    first_discovery = _discovery(runtime=False, plugin=True)
    retry_discovery = _discovery(runtime=False, plugin=False)
    retry_discovery["plugin_update_check"]["installed"] = target_snapshot
    retry_discovery["plugin_update_check"]["installed_version"] = "0.21.6"
    retry_discovery["plugin_update_check"]["installed_commit"] = PLUGIN_COMMIT
    messages: list[str] = []

    def fake_send(text: str) -> dict[str, Any]:
        messages.append(text)
        return {"ok": True}

    with tempfile.TemporaryDirectory() as tmp:
        ctx = FakeContext(Path(tmp))
        failed_update = AsyncMock(side_effect=RuntimeError("enable failed"))
        with (
            patch(
                "hermes_runtime.update_orchestrator.run_update_check",
                AsyncMock(return_value=first_discovery),
            ),
            patch(
                "hermes_runtime.update_orchestrator.update_tinyhat_plugin",
                failed_update,
            ),
            patch(
                "hermes_runtime.update_orchestrator.plugin_snapshot",
                return_value=target_snapshot,
            ),
            patch("hermes_runtime.update_orchestrator._telegram_send", fake_send),
        ):
            first = asyncio.run(
                run_command(
                    ctx,
                    {"kind": "check_and_stage_updates", "spec": _spec()},
                )
            )

        repair_path = Path(tmp) / "updates" / "pending_plugin_repair.json"
        assert repair_path.is_file()
        assert first["status"] == "partial"
        assert first["plugin"]["changed"] is True
        assert first["plugin"]["installed_commit"] == PLUGIN_COMMIT
        assert first["plugin"]["installed"]["source"] == {
            "repo_url": "https://github.com/tinyhat-ai/tinyhat.git",
            "ref": "channels/lts",
            "commit": PLUGIN_COMMIT,
        }
        assert first["runtime_restart_requested"] is True
        assert first["notification"]["sent"] is True
        assert len(messages) == 1

        ctx.restart_requested = False
        repaired_update = AsyncMock(
            return_value={"changed": False, "after": target_snapshot}
        )
        with (
            patch(
                "hermes_runtime.update_orchestrator.run_update_check",
                AsyncMock(return_value=retry_discovery),
            ),
            patch(
                "hermes_runtime.update_orchestrator.update_tinyhat_plugin",
                repaired_update,
            ),
            patch("hermes_runtime.update_orchestrator._telegram_send", fake_send),
        ):
            retry = asyncio.run(
                run_command(
                    ctx,
                    {"kind": "check_and_stage_updates", "spec": _spec()},
                )
            )

        repaired_update.assert_awaited_once()
        marker = json.loads(repair_path.read_text(encoding="utf-8"))
        assert marker["state"] == "installed_unacknowledged"
        assert retry["status"] == "updated"
        assert retry["changed"] is True
        assert retry["plugin"]["repair_performed"] is True
        assert retry["plugin"]["installed_commit"] == PLUGIN_COMMIT
        assert retry["notification"]["sent"] is True
        assert ctx.restart_requested is True
        assert len(messages) == 2
        assert acknowledge_check_and_stage_result(
            ctx,
            {"kind": "check_and_stage_updates", "spec": _spec()},
            retry,
        )
        assert repair_path.exists() is False


def test_retry_is_idempotent_for_exact_runtime_and_plugin_targets() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctx = FakeContext(Path(tmp))
        stage = AsyncMock(side_effect=_fake_stage)
        update_plugin = AsyncMock(return_value=_plugin_result(changed=True))
        messages: list[str] = []
        current_discovery = _discovery(runtime=True, plugin=False)
        current_discovery["plugin_update_check"].update(
            {
                "installed": _plugin_result(changed=True)["after"],
                "installed_version": "0.21.6",
                "installed_commit": PLUGIN_COMMIT,
            }
        )

        def fake_send(text: str) -> dict[str, Any]:
            messages.append(text)
            return {"ok": True}

        with (
            patch(
                "hermes_runtime.update_orchestrator.run_update_check",
                AsyncMock(
                    side_effect=[
                        _discovery(runtime=True, plugin=True),
                        current_discovery,
                    ]
                ),
            ),
            patch("hermes_runtime.update_orchestrator.stage_update.run", stage),
            patch(
                "hermes_runtime.update_orchestrator.update_tinyhat_plugin",
                update_plugin,
            ),
            patch("hermes_runtime.update_orchestrator._telegram_send", fake_send),
        ):
            first = asyncio.run(
                run_command(
                    ctx,
                    {"kind": "check_and_stage_updates", "spec": _spec()},
                )
            )
            assert acknowledge_check_and_stage_result(
                ctx,
                {"kind": "check_and_stage_updates", "spec": _spec()},
                first,
            )
            ctx.restart_requested = False
            second = asyncio.run(
                run_command(
                    ctx,
                    {"kind": "check_and_stage_updates", "spec": _spec()},
                )
            )

    assert first["changed"] is True
    assert second["changed"] is True
    assert second["runtime"]["status"] == "staged"
    assert second["runtime"]["activation_requested"] is True
    assert second["runtime_restart_requested"] is True
    assert second["hermes_restart_required"] is False
    assert ctx.restart_requested is True
    assert stage.await_count == 1
    assert update_plugin.await_count == 1
    # The command was driven a second time without a platform settlement in
    # between, so the still-pending runtime activation notice is retried.
    assert len(messages) == 2


def test_update_discovery_honors_supplied_exact_runtime_sha() -> None:
    async def current_plugin(command: dict[str, Any]) -> dict[str, Any]:
        assert command["spec"]["target_commit"] == PLUGIN_COMMIT
        assert "target_sha" not in command["spec"]
        return {"update_available": False, "decision": "installed_matches_target"}

    with tempfile.TemporaryDirectory() as tmp:
        with (
            patch(
                "hermes_runtime.update_check._fetch_github_commit",
                side_effect=AssertionError("exact sha must not be resolved again"),
            ),
            patch(
                "hermes_runtime.update_check.tinyhat_plugin_status",
                current_plugin,
            ),
        ):
            result = asyncio.run(
                run_update_check(
                    state_dir=Path(tmp),
                    current_version="v0.0.44",
                    current_code_version="0.0.44",
                    current_sha="d" * 40,
                    spec={
                        **_spec(),
                        "target_ref": "v0.0.45",
                        "target_sha": RUNTIME_SHA.upper(),
                    },
                    reason="check_and_stage_updates",
                )
            )

    assert result["status"] == "provided_target_sha"
    assert result["target_sha"] == RUNTIME_SHA
    assert result["requested_target_ref"] == "v0.0.45"
    assert result["target_ref"] == "v0.0.45"
    assert result["update_available"] is True


def test_runtime_sha_is_never_projected_as_plugin_target() -> None:
    plugin_spec = update_check._plugin_check_spec(
        {
            "target_ref": "v0.0.45",
            "target_sha": RUNTIME_SHA,
            "plugin_repo_url": "https://github.com/tinyhat-ai/tinyhat.git",
            "plugin_ref": "channels/lts",
        }
    )

    assert plugin_spec == {
        "plugin_repo_url": "https://github.com/tinyhat-ai/tinyhat.git",
        "plugin_ref": "channels/lts",
    }


def test_channel_resolution_reads_final_version_and_verifies_tag_commit() -> None:
    with (
        patch(
            "hermes_runtime.update_check.request.urlopen",
            return_value=FakeResponse(b"0.0.44\n"),
        ),
        patch(
            "hermes_runtime.update_check._fetch_github_commit",
            return_value={
                "ok": True,
                "status": "ok",
                "sha": RUNTIME_SHA,
                "html_url": "https://github.com/example/commit/" + RUNTIME_SHA,
            },
        ) as fetch_commit,
    ):
        result = update_check._resolve_channel_final_target("channels/lts")

    fetch_commit.assert_called_once_with("v0.0.44")
    assert result["ok"] is True
    assert result["target_ref"] == "v0.0.44"
    assert result["sha"] == RUNTIME_SHA


def test_channel_resolution_rejects_non_final_version() -> None:
    with (
        patch(
            "hermes_runtime.update_check.request.urlopen",
            return_value=FakeResponse(b"0.0.45-rc.1\n"),
        ),
        patch("hermes_runtime.update_check._fetch_github_commit") as fetch_commit,
    ):
        result = update_check._resolve_channel_final_target("channels/latest")

    fetch_commit.assert_not_called()
    assert result == {
        "ok": False,
        "status": "channel_version_invalid",
        "message": "Channel VERSION is not a final release",
    }


def test_commit_resolution_contains_timeout_and_malformed_json() -> None:
    with patch(
        "hermes_runtime.update_check.request.urlopen",
        side_effect=TimeoutError("timed out"),
    ):
        timed_out = update_check._fetch_github_commit("v0.0.45")
    assert timed_out["status"] == "unavailable"

    for transport_error in (
        ConnectionResetError("connection reset"),
        http.client.RemoteDisconnected("remote disconnected"),
    ):
        with patch(
            "hermes_runtime.update_check.request.urlopen",
            side_effect=transport_error,
        ):
            unavailable = update_check._fetch_github_commit("v0.0.45")
        assert unavailable["status"] == "unavailable"

    with patch(
        "hermes_runtime.update_check.request.urlopen",
        return_value=FakeResponse(b"not-json"),
    ):
        malformed = update_check._fetch_github_commit("v0.0.45")
    assert malformed["status"] == "malformed"

    with patch(
        "hermes_runtime.update_check.request.urlopen",
        return_value=FakeResponse(b'{"sha":"short"}'),
    ):
        malformed_sha = update_check._fetch_github_commit("v0.0.45")
    assert malformed_sha["status"] == "malformed"


def test_command_rejects_non_full_target_commits() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctx = FakeContext(Path(tmp))
        spec = {**_spec(), "target_commit": "abc1234"}
        with unittest.TestCase().assertRaisesRegex(
            ValueError,
            "target_commit must be a full git commit sha",
        ):
            asyncio.run(
                run_command(
                    ctx,
                    {"kind": "check_and_stage_updates", "spec": spec},
                )
            )

        overlong_spec = {**_spec(), "target_sha": "a" * 41}
        with unittest.TestCase().assertRaisesRegex(
            ValueError,
            "valid target_sha",
        ):
            asyncio.run(
                run_command(
                    ctx,
                    {"kind": "check_and_stage_updates", "spec": overlong_spec},
                )
            )


def test_command_requires_a_bounded_reason() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ctx = FakeContext(Path(tmp))
        spec = _spec()
        spec.pop("reason")
        with unittest.TestCase().assertRaisesRegex(
            ValueError,
            "check_and_stage_updates requires reason",
        ):
            asyncio.run(
                run_command(
                    ctx,
                    {"kind": "check_and_stage_updates", "spec": spec},
                )
            )
