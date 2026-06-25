"""Smoke tests for the Hermes runtime command whitelist."""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hermes_runtime.commands import run_command  # noqa: E402


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

    def test_unknown_command_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            asyncio.run(run_command(SimpleNamespace(), {"kind": "shell"}))


if __name__ == "__main__":
    import unittest

    unittest.main()
