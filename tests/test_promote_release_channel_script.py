"""Tests for the maintainer-only channel promotion helper."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "promote_release_channel.py"
SPEC = importlib.util.spec_from_file_location("promote_release_channel", SCRIPT)
assert SPEC is not None
promote_release_channel = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(promote_release_channel)


class PromoteReleaseChannelScriptTests(TestCase):
    def test_normalize_final_tag_accepts_plain_version(self) -> None:
        self.assertEqual(promote_release_channel.normalize_final_tag("0.0.10"), "v0.0.10")

    def test_normalize_final_tag_rejects_dev_or_rc_tags(self) -> None:
        with self.assertRaises(SystemExit):
            promote_release_channel.normalize_final_tag("v0.0.10-dev.20260626T120000Z.smoke")
        with self.assertRaises(SystemExit):
            promote_release_channel.normalize_final_tag("v0.0.10-rc.1")

    def test_parse_channels_dedupes_repeated_and_comma_separated_values(self) -> None:
        self.assertEqual(
            promote_release_channel.parse_channels(["latest,lts", "latest", "beta"]),
            ["latest", "lts", "beta"],
        )

    def test_channel_ref_uses_protected_channel_branch_namespace(self) -> None:
        self.assertEqual(promote_release_channel.channel_ref("latest"), "heads/channels/latest")

    def test_channel_ref_rejects_path_traversal_or_slashes(self) -> None:
        with self.assertRaises(SystemExit):
            promote_release_channel.channel_ref("../main")
        with self.assertRaises(SystemExit):
            promote_release_channel.channel_ref("team/latest")


if __name__ == "__main__":
    import unittest

    unittest.main()
