"""Smoke tests for the Hermes runtime command whitelist."""

from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hermes_runtime.commands import run_command  # noqa: E402
from hermes_runtime.local_ledger import append_entry, report  # noqa: E402
from hermes_runtime.main import (  # noqa: E402
    RuntimeContext,
    _heartbeat_metrics,
    _run_one_command,
    _scheduled_update_check,
)
from hermes_runtime.platform_paths import computer_api_path  # noqa: E402
from hermes_runtime.update_artifacts import staged_package_dir  # noqa: E402
from hermes_runtime.update_check import scheduled_check_due  # noqa: E402


class FakePlatform:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[str] = []
        self.fail_posts = False

    async def post_json(self, path: str, payload: dict) -> dict:
        if self.fail_posts:
            raise RuntimeError("post failed")
        self.posts.append((path, payload))
        return {"ok": True}

    async def get_json(self, path: str) -> dict:
        self.gets.append(path)
        return {"ok": True, "path": path}


class CommandTests(TestCase):
    def test_ping_returns_pong(self) -> None:
        result = asyncio.run(run_command(SimpleNamespace(), {"kind": "ping"}))
        self.assertEqual(result["message"], "pong")

    def test_platform_paths_use_local_dev_context(self) -> None:
        self.assertEqual(
            computer_api_path("computer 123", "heartbeat"),
            "/hapi/v1/computers/local-dev/heartbeat",
        )

    def test_whoami_uses_local_dev_attestation_path(self) -> None:
        platform = FakePlatform()
        ctx = SimpleNamespace(platform=platform, computer_id="computer 123")

        result = asyncio.run(run_command(ctx, {"kind": "whoami"}))

        self.assertEqual(
            platform.gets,
            ["/hapi/v1/computers/local-dev/whoami"],
        )
        self.assertEqual(
            result["attestation"]["path"],
            "/hapi/v1/computers/local-dev/whoami",
        )

    def test_update_is_staged_then_marked_for_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            source_root = state_dir / "source"
            (source_root / "hermes_runtime" / "commands").mkdir(parents=True)
            (source_root / "hermes_runtime" / "__init__.py").write_text(
                '__version__ = "0.20.0-dev"\n',
                encoding="utf-8",
            )
            (source_root / "hermes_runtime" / "commands" / "__init__.py").write_text(
                "COMMAND_MODULES = {}\n",
                encoding="utf-8",
            )
            ctx = SimpleNamespace(
                state_dir=state_dir,
                restart_requested=False,
                staged_version_file=state_dir / "staged" / "VERSION",
                activation_marker=state_dir / "ACTIVATE_ON_RESTART",
                staged_version=lambda: (
                    (state_dir / "staged" / "VERSION")
                    .read_text(encoding="utf-8")
                    .strip()
                    if (state_dir / "staged" / "VERSION").exists()
                    else None
                ),
            )
            with patch.dict(
                "os.environ",
                {"TINYHAT_RUNTIME_UPDATE_SOURCE_DIR": str(source_root)},
            ):
                staged = asyncio.run(
                    run_command(
                        ctx,
                        {
                            "kind": "stage_update",
                            "spec": {
                                "target_ref": "v0.20.0-dev.20260625T173000Z.smoke",
                                "channel": "custom",
                            },
                        },
                    )
                )
            self.assertEqual(
                staged["target_ref"], "v0.20.0-dev.20260625T173000Z.smoke"
            )
            self.assertEqual(staged["activation"], "requires_activate_update")
            self.assertTrue(staged["code_staged"])
            self.assertTrue((staged_package_dir(state_dir) / "__init__.py").is_file())
            self.assertEqual(
                ctx.staged_version(), "v0.20.0-dev.20260625T173000Z.smoke"
            )

            activated = asyncio.run(
                run_command(ctx, {"kind": "activate_update", "spec": {}})
            )
            self.assertEqual(
                activated["target_version"], "v0.20.0-dev.20260625T173000Z.smoke"
            )
            self.assertTrue(ctx.restart_requested)
            self.assertEqual(
                ctx.activation_marker.read_text().strip(),
                "v0.20.0-dev.20260625T173000Z.smoke",
            )

    def test_activation_swaps_staged_runtime_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            install_prefix = root / "install"
            source_root = root / "source"
            (install_prefix / "hermes_runtime").mkdir(parents=True)
            (install_prefix / "hermes_runtime" / "old_only.py").write_text(
                "OLD = True\n",
                encoding="utf-8",
            )
            (source_root / "hermes_runtime" / "commands").mkdir(parents=True)
            (source_root / "hermes_runtime" / "__init__.py").write_text(
                '__version__ = "0.0.3"\n',
                encoding="utf-8",
            )
            (source_root / "hermes_runtime" / "new_command.py").write_text(
                "NEW = True\n",
                encoding="utf-8",
            )
            (source_root / "hermes_runtime" / "commands" / "__init__.py").write_text(
                "COMMAND_MODULES = {}\n",
                encoding="utf-8",
            )
            ctx = RuntimeContext(
                platform=FakePlatform(),
                state_dir=state_dir,
                started_at=0,
            )
            with patch.dict(
                "os.environ",
                {
                    "TINYHAT_RUNTIME_PREFIX": str(install_prefix),
                    "TINYHAT_RUNTIME_UPDATE_SOURCE_DIR": str(source_root),
                },
            ):
                asyncio.run(
                    run_command(
                        ctx,
                        {
                            "kind": "stage_update",
                            "spec": {"target_ref": "v0.0.3", "channel": "custom"},
                        },
                    )
                )
                asyncio.run(run_command(ctx, {"kind": "activate_update"}))
                activated = ctx.activate_staged_on_startup()

            self.assertEqual(activated, {"version": "v0.0.3", "code_swapped": True})
            self.assertFalse((install_prefix / "hermes_runtime" / "old_only.py").exists())
            self.assertTrue(
                (install_prefix / "hermes_runtime" / "new_command.py").is_file()
            )
            self.assertEqual(ctx.current_version(), "v0.0.3")
            self.assertFalse(staged_package_dir(state_dir).exists())

    def test_restart_runtime_service_requests_restart(self) -> None:
        ctx = SimpleNamespace(restart_requested=False)

        result = asyncio.run(
            run_command(ctx, {"kind": "restart_runtime_service", "spec": {}})
        )

        self.assertTrue(ctx.restart_requested)
        self.assertEqual(result["message"], "runtime service restart requested")
        self.assertEqual(
            result["restart_target"], "tinyhat-hermes-runtime.service"
        )
        self.assertEqual(result["effect"], "after_command_result")

    def test_update_status_reports_current_and_staged_versions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            ctx = SimpleNamespace(
                state_dir=state_dir,
                staged_version_file=state_dir / "staged" / "VERSION",
                staged_metadata_file=state_dir / "staged" / "metadata.json",
                activation_marker=state_dir / "ACTIVATE_ON_RESTART",
                current_version=lambda: "v0.0.1",
                current_commit_sha=lambda: None,
                staged_version=lambda: "v0.20.0-dev.20260625T173000Z.next",
            )
            ctx.staged_metadata_file.parent.mkdir(parents=True)
            ctx.staged_metadata_file.write_text(
                '{"target_ref":"v0.20.0-dev.20260625T173000Z.next","channel":"custom"}\n',
                encoding="utf-8",
            )

            status = asyncio.run(run_command(ctx, {"kind": "update_status"}))

            self.assertEqual(status["current_version"], "v0.0.1")
            self.assertEqual(
                status["ready_updates"][0]["version"],
                "v0.20.0-dev.20260625T173000Z.next",
            )
            self.assertEqual(
                status["ready_updates"][0]["activation"],
                "requires_activate_update",
            )

            ctx.activation_marker.write_text(
                "v0.20.0-dev.20260625T173000Z.next\n",
                encoding="utf-8",
            )
            status = asyncio.run(run_command(ctx, {"kind": "update_status"}))
            self.assertEqual(
                status["ready_updates"][0]["activation"],
                "after_runtime_restart",
            )

    def test_recent_commands_returns_local_ledger_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            append_entry(
                state_dir=state_dir,
                command={
                    "command_id": "cmd-1",
                    "kind": "ping",
                    "spec": {},
                    "created_at": "2026-06-25T10:00:00Z",
                },
                status="applied",
                phase="ping",
                result={"message": "pong"},
                started_at="2026-06-25T10:00:01Z",
                completed_at="2026-06-25T10:00:02Z",
            )
            ctx = SimpleNamespace(state_dir=state_dir)

            result = asyncio.run(
                run_command(ctx, {"kind": "recent_commands", "spec": {"limit": 5}})
            )

            self.assertEqual(result["count"], 1)
            self.assertEqual(result["commands"][0]["command_id"], "cmd-1")
            self.assertEqual(report(state_dir=state_dir)["commands"][0]["kind"], "ping")

    def test_setup_snapshot_reports_install_and_service_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_root = root / "opt" / "tinyhat-hermes-runtime"
            state_dir = root / "var" / "lib" / "tinyhat-hermes-runtime"
            (install_root / "env").mkdir(parents=True)
            (state_dir / "current").mkdir(parents=True)
            (install_root / "INSTALL_REF").write_text("channels/lts\n", encoding="utf-8")
            (install_root / "env" / "runtime.env").write_text(
                "TINYHAT_LOCAL_DEV_TOKEN=secret\n",
                encoding="utf-8",
            )
            (state_dir / "current" / "VERSION").write_text("0.0.1\n", encoding="utf-8")
            (state_dir / "current" / "COMMIT_SHA").write_text(
                "a" * 40 + "\n",
                encoding="utf-8",
            )
            ctx = SimpleNamespace(state_dir=state_dir)

            def fake_systemctl(args: list[str]) -> dict:
                if args[0] == "show":
                    return {
                        "systemctl_available": True,
                        "ok": True,
                        "stdout": (
                            "ActiveState=active\n"
                            "Restart=always\n"
                            "Nice=-5\n"
                            "OOMScoreAdjust=-900\n"
                        ),
                        "stderr": "",
                    }
                return {
                    "systemctl_available": True,
                    "ok": True,
                    "stdout": "[Service]\nRestart=always\n",
                    "stderr": "",
                }

            with patch.dict(
                "os.environ",
                {"TINYHAT_RUNTIME_PREFIX": str(install_root)},
            ), patch(
                "hermes_runtime.commands.setup_snapshot._run_systemctl",
                side_effect=fake_systemctl,
            ):
                snapshot = asyncio.run(
                    run_command(ctx, {"kind": "setup_snapshot", "spec": {}})
                )

            self.assertEqual(snapshot["schema"], "tinyhat_hermes_setup_snapshot_v1")
            self.assertEqual(
                snapshot["install"]["install_ref"]["value"],
                "channels/lts",
            )
            self.assertEqual(
                snapshot["state"]["current_version"]["value"],
                "0.0.1",
            )
            self.assertEqual(
                snapshot["service"]["properties"]["OOMScoreAdjust"],
                "-900",
            )
            self.assertEqual(snapshot["warnings"], [])
            self.assertNotIn("secret", str(snapshot))

    def test_ledger_write_failure_still_reports_command_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            platform = FakePlatform()
            ctx = SimpleNamespace(
                platform=platform,
                state_dir=Path(tmp),
                computer_id="local-dev",
            )

            with patch(
                "hermes_runtime.main.append_entry",
                side_effect=OSError("ledger unavailable"),
            ):
                asyncio.run(
                    _run_one_command(
                        ctx,
                        {
                            "command_id": "cmd-ledger-fails",
                            "idempotency_key": "idem-ledger-fails",
                            "kind": "ping",
                            "spec": {},
                        },
                    )
                )

            self.assertEqual(
                platform.posts[0][0],
                "/hapi/v1/computers/local-dev/runtime-command/result",
            )
            reported = platform.posts[0][1]["result"]
            self.assertEqual(reported["status"], "applied")
            self.assertEqual(reported["result"], {"message": "pong"})

    def test_check_update_writes_last_result_without_staging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            platform = FakePlatform()
            ctx = SimpleNamespace(
                platform=platform,
                state_dir=state_dir,
                current_version=lambda: "v0.20.0-dev.20260625T173000Z.old",
                current_commit_sha=lambda: None,
            )

            with patch(
                "hermes_runtime.update_check._fetch_github_commit",
                return_value={
                    "ok": True,
                    "status": "ok",
                    "sha": "a" * 40,
                    "html_url": "https://github.com/tinyloophub/tinyhat--runtimes--hermes/commit/"
                    + "a" * 40,
                },
            ):
                checked = asyncio.run(
                    run_command(
                        ctx,
                        {
                            "kind": "check_update",
                            "spec": {
                                "channel": "custom",
                                "target_ref": "v0.20.0-dev.20260625T173000Z.next",
                            },
                        },
                    )
                )

            self.assertEqual(checked["status"], "ok")
            self.assertTrue(checked["update_available"])
            self.assertEqual(
                checked["target_ref"], "v0.20.0-dev.20260625T173000Z.next"
            )
            self.assertTrue((state_dir / "updates" / "last_check.json").is_file())
            self.assertFalse((state_dir / "staged" / "VERSION").exists())
            self.assertEqual(
                platform.posts[0][0],
                "/hapi/v1/computers/local-dev/update-check-results/v1",
            )
            self.assertEqual(
                platform.posts[0][1]["result"]["target_ref"],
                "v0.20.0-dev.20260625T173000Z.next",
            )

    def test_check_update_is_not_available_when_current_sha_matches_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            platform = FakePlatform()
            target_sha = "a" * 40
            ctx = SimpleNamespace(
                platform=platform,
                state_dir=state_dir,
                computer_id="987",
                current_version=lambda: "channels/lts",
                current_commit_sha=lambda: target_sha,
            )

            with patch(
                "hermes_runtime.update_check._fetch_github_commit",
                return_value={
                    "ok": True,
                    "status": "ok",
                    "sha": target_sha,
                    "html_url": "https://github.com/tinyloophub/tinyhat--runtimes--hermes/commit/"
                    + target_sha,
                },
            ):
                checked = asyncio.run(
                    run_command(
                        ctx,
                        {
                            "kind": "check_update",
                            "spec": {
                                "channel": "lts",
                                "target_ref": "channels/lts",
                            },
                        },
                    )
                )

            self.assertFalse(checked["update_available"])
            self.assertEqual(checked["current_sha"], target_sha)
            self.assertEqual(
                platform.posts[0][0],
                "/hapi/v1/computers/local-dev/update-check-results/v1",
            )

    def test_check_update_is_not_available_when_current_ref_matches_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            platform = FakePlatform()
            current_ref = "v0.20.0-dev.20260625T153259Z.local-dev-check"
            ctx = SimpleNamespace(
                platform=platform,
                state_dir=state_dir,
                current_version=lambda: current_ref,
                current_commit_sha=lambda: None,
            )

            with patch(
                "hermes_runtime.update_check._fetch_github_commit",
                return_value={
                    "ok": True,
                    "status": "ok",
                    "sha": "b" * 40,
                    "html_url": "https://github.com/tinyloophub/tinyhat--runtimes--hermes/commit/"
                    + "b" * 40,
                },
            ):
                checked = asyncio.run(
                    run_command(
                        ctx,
                        {
                            "kind": "check_update",
                            "spec": {
                                "channel": "custom",
                                "target_ref": current_ref,
                            },
                        },
                    )
                )

            self.assertFalse(checked["update_available"])
            self.assertEqual(checked["target_ref"], current_ref)

    def test_check_update_uses_dev_ref_check_for_local_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            platform = FakePlatform()
            ctx = SimpleNamespace(
                platform=platform,
                state_dir=state_dir,
                current_version=lambda: "v0.0.1",
                current_commit_sha=lambda: None,
            )

            with patch.dict("os.environ", {"TINYHAT_LOCAL_DEV_TOKEN": "dev-token"}):
                with patch(
                    "hermes_runtime.update_check._fetch_github_commit",
                    side_effect=AssertionError("local dev must not call GitHub"),
                ):
                    checked = asyncio.run(
                        run_command(
                            ctx,
                            {
                                "kind": "check_update",
                                "spec": {
                                    "channel": "lts",
                                    "target_ref": "v0.0.1",
                                },
                            },
                        )
                    )

            self.assertEqual(checked["status"], "dev_ref_check")
            self.assertFalse(checked["update_available"])
            self.assertEqual(checked["target_sha"], None)
            self.assertIn("Local dev", checked["detail"])

    def test_lts_check_does_not_treat_dev_tag_as_available_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            platform = FakePlatform()
            ctx = SimpleNamespace(
                platform=platform,
                state_dir=state_dir,
                current_version=lambda: "v0.0.1",
                current_commit_sha=lambda: None,
            )

            with patch.dict("os.environ", {"TINYHAT_LOCAL_DEV_TOKEN": "dev-token"}):
                checked = asyncio.run(
                    run_command(
                        ctx,
                        {
                            "kind": "check_update",
                            "spec": {
                                "channel": "lts",
                                "target_ref": "v0.0.2-dev.20260625T173000Z.smoke",
                            },
                        },
                    )
                )

            self.assertFalse(checked["channel_eligible"])
            self.assertFalse(checked["update_available"])
            self.assertEqual(
                checked["target_ref"], "v0.0.2-dev.20260625T173000Z.smoke"
            )

    def test_check_update_rejects_older_final_tag_for_bare_current_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            platform = FakePlatform()
            ctx = SimpleNamespace(
                platform=platform,
                state_dir=state_dir,
                current_version=lambda: "0.0.2",
                current_commit_sha=lambda: None,
            )

            with patch.dict("os.environ", {"TINYHAT_LOCAL_DEV_TOKEN": "dev-token"}):
                checked = asyncio.run(
                    run_command(
                        ctx,
                        {
                            "kind": "check_update",
                            "spec": {
                                "channel": "latest",
                                "target_ref": "v0.0.1",
                            },
                        },
                    )
                )

            self.assertTrue(checked["channel_eligible"])
            self.assertFalse(checked["target_final_version_is_newer"])
            self.assertFalse(checked["update_available"])

    def test_check_update_platform_report_failure_is_nonfatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            platform = FakePlatform()
            platform.fail_posts = True
            ctx = SimpleNamespace(
                platform=platform,
                state_dir=state_dir,
                current_version=lambda: "v0.0.1",
                current_commit_sha=lambda: None,
            )

            with patch.dict("os.environ", {"TINYHAT_LOCAL_DEV_TOKEN": "dev-token"}):
                checked = asyncio.run(
                    run_command(
                        ctx,
                        {
                            "kind": "check_update",
                            "spec": {
                                "channel": "lts",
                                "target_ref": "v0.0.2",
                            },
                        },
                    )
                )

            self.assertEqual(checked["message"], "update check complete")
            self.assertTrue(checked["update_available"])
            self.assertFalse(checked["report_delivered"])
            self.assertEqual(checked["report_error"], "post failed")
            self.assertTrue((state_dir / "updates" / "last_check.json").is_file())

    def test_heartbeat_metrics_do_not_embed_update_check_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "updates").mkdir()
            (state_dir / "updates" / "last_check.json").write_text(
                '{"target_ref":"v0.20.0-dev.20260625T173000Z.next"}',
                encoding="utf-8",
            )
            ctx = SimpleNamespace(
                state_dir=state_dir,
                started_at=0.0,
                current_version=lambda: "v0.20.0-dev.20260625T173000Z.old",
                staged_version=lambda: None,
            )

            metrics = _heartbeat_metrics(ctx, status="running")

            self.assertNotIn("update_check", metrics["hermes_runtime"])

    def test_scheduled_update_check_due_once_after_configured_local_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            config_dir = state_dir / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "update_check_time").write_text("02:35\n")
            (config_dir / "update_check_timezone").write_text("America/Los_Angeles\n")
            now = datetime(2026, 6, 25, 10, 0, tzinfo=timezone.utc)

            due, config, date_key = scheduled_check_due(
                state_dir=state_dir,
                now_utc=now,
            )

            self.assertTrue(due)
            self.assertEqual(config.local_time, "02:35")
            self.assertEqual(config.timezone, "America/Los_Angeles")
            self.assertEqual(date_key, "2026-06-25")
            (state_dir / "updates").mkdir()
            (state_dir / "updates" / "last_scheduled_check_date").write_text(
                date_key + "\n"
            )
            due_again, _config, _date_key = scheduled_check_due(
                state_dir=state_dir,
                now_utc=now,
            )
            self.assertFalse(due_again)

    def test_scheduled_update_check_posts_result_then_marks_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            config_dir = state_dir / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "update_check_time").write_text("00:00\n")
            (config_dir / "update_check_timezone").write_text("UTC\n")
            platform = FakePlatform()
            ctx = SimpleNamespace(
                platform=platform,
                state_dir=state_dir,
                computer_id="scheduled",
                current_version=lambda: "channels/lts",
                current_commit_sha=lambda: "b" * 40,
            )

            with patch(
                "hermes_runtime.update_check._fetch_github_commit",
                return_value={
                    "ok": True,
                    "status": "ok",
                    "sha": "c" * 40,
                    "html_url": "https://github.com/tinyloophub/tinyhat--runtimes--hermes/commit/"
                    + "c" * 40,
                },
            ):
                result = asyncio.run(_scheduled_update_check(ctx))

            self.assertEqual(result["reason"], "scheduled")
            self.assertEqual(
                platform.posts[0][0],
                "/hapi/v1/computers/local-dev/update-check-results/v1",
            )
            self.assertTrue(
                (state_dir / "updates" / "last_scheduled_check_date").is_file()
            )

    def test_failed_scheduled_update_check_does_not_mark_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            config_dir = state_dir / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "update_check_time").write_text("00:00\n")
            (config_dir / "update_check_timezone").write_text("UTC\n")
            platform = FakePlatform()
            platform.fail_posts = True
            ctx = SimpleNamespace(
                platform=platform,
                state_dir=state_dir,
                current_version=lambda: "channels/lts",
                current_commit_sha=lambda: None,
            )

            with patch(
                "hermes_runtime.update_check._fetch_github_commit",
                return_value={
                    "ok": True,
                    "status": "ok",
                    "sha": "c" * 40,
                    "html_url": "https://github.com/tinyloophub/tinyhat--runtimes--hermes/commit/"
                    + "c" * 40,
                },
            ):
                with self.assertRaises(RuntimeError):
                    asyncio.run(_scheduled_update_check(ctx))

            self.assertFalse(
                (state_dir / "updates" / "last_scheduled_check_date").exists()
            )

    def test_unknown_command_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            asyncio.run(run_command(SimpleNamespace(), {"kind": "shell"}))


if __name__ == "__main__":
    import unittest

    unittest.main()
