"""Smoke tests for the Hermes runtime command whitelist."""

from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hermes_runtime.commands import run_command  # noqa: E402
from hermes_runtime.main import _heartbeat_metrics  # noqa: E402
from hermes_runtime.update_check import scheduled_check_due  # noqa: E402


class FakePlatform:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []

    async def post_json(self, path: str, payload: dict) -> dict:
        self.posts.append((path, payload))
        return {"ok": True}


class CommandTests(TestCase):
    def test_ping_returns_pong(self) -> None:
        result = asyncio.run(run_command(SimpleNamespace(), {"kind": "ping"}))
        self.assertEqual(result["message"], "pong")

    def test_update_is_staged_then_marked_for_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
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

    def test_check_update_writes_last_result_without_staging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            platform = FakePlatform()
            ctx = SimpleNamespace(
                platform=platform,
                state_dir=state_dir,
                current_version=lambda: "v0.20.0-dev.20260625T173000Z.old",
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
            now = datetime(2026, 6, 25, 10, 0, tzinfo=UTC)

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

    def test_unknown_command_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            asyncio.run(run_command(SimpleNamespace(), {"kind": "shell"}))


if __name__ == "__main__":
    import unittest

    unittest.main()
