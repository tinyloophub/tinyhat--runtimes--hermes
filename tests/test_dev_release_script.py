"""Tests for the dev-release publishing helper."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "publish_dev_release.py"
SPEC = importlib.util.spec_from_file_location("publish_dev_release", SCRIPT)
assert SPEC is not None
publish_dev_release = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(publish_dev_release)


class DevReleaseScriptTests(TestCase):
    def test_make_tag_uses_utc_stamp_and_clean_suffix(self) -> None:
        tag = publish_dev_release.make_tag(
            base="v0.20.0",
            suffix="PR #2 smoke!",
            now=datetime(2026, 6, 25, 18, 30, 45, tzinfo=UTC),
        )

        self.assertEqual(tag, "v0.20.0-dev.20260625T183045Z.PR-2-smoke")

    def test_installer_command_uses_exact_dev_tag(self) -> None:
        tag = "v0.20.0-dev.20260625T183045Z.smoke"

        command = publish_dev_release.installer_command(tag)

        self.assertEqual(
            command,
            "curl -fsSL https://raw.githubusercontent.com/"
            "tinyloophub/tinyhat--runtimes--hermes/"
            "v0.20.0-dev.20260625T183045Z.smoke/install.sh "
            "| bash -s -- --ref v0.20.0-dev.20260625T183045Z.smoke",
        )

    def test_default_notes_say_dev_release_does_not_move_channels(self) -> None:
        notes = publish_dev_release.default_notes(
            tag="v0.20.0-dev.20260625T183045Z.smoke",
            target="HEAD",
        )

        self.assertIn("before the PR branch is merged", notes)
        self.assertIn("channels/latest", notes)
        self.assertIn("channels/lts", notes)
        self.assertIn("v0.20.0-dev.20260625T183045Z.smoke/install.sh", notes)


if __name__ == "__main__":
    import unittest

    unittest.main()
