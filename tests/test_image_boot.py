"""First boot validates image provenance before writing machine configuration."""

import hashlib
import json
import stat
import tempfile
from pathlib import Path
from unittest import TestCase

from hermes_runtime import image_boot


class ImageBootTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.sha = "a" * 40
        manifest = json.dumps({"schema": "tinyhat.agent-image.v1", "runtime_sha": self.sha}).encode()
        for path, content in ((image_boot.MANIFEST, manifest),
                              (image_boot.STATE / "current/COMMIT_SHA", self.sha.encode()),
                              (image_boot.PREFIX / "bin/tinyhat-hermes-runtime", b"#!/bin/sh\n")):
            local = self.root / path.relative_to("/")
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(content)
        self.arguments = dict(platform_url="https://api.example.com", audience="https://api.example.com",
                              computer_id="123", runtime_sha=self.sha,
                              manifest_sha256=hashlib.sha256(manifest).hexdigest(), root=self.root)

    def test_fresh_configuration_has_only_machine_context(self):
        image_boot.configure(**self.arguments)
        path = self.root / "etc/tinyhat/hermes-runtime.env"
        value = path.read_text()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertIn("TINYHAT_COMPUTER_ID=123\n", value)
        self.assertIn("TINYHAT_AGENT_SYSTEMS_PREINSTALLED=1\n", value)
        self.assertNotIn("LOCAL_DEV_TOKEN", value)
        self.assertNotIn("TELEGRAM", value)
        self.assertNotIn("API_KEY", value)
        image_boot.configure(**self.arguments)
        self.assertEqual(path.read_text(), value)

    def test_mismatched_provenance_writes_nothing(self):
        with self.assertRaises(ValueError):
            image_boot.configure(**{**self.arguments, "manifest_sha256": "b" * 64})
        self.assertFalse((self.root / "etc").exists())
        with self.assertRaises(ValueError):
            image_boot.configure(**{**self.arguments, "runtime_sha": "b" * 40})
        self.assertFalse((self.root / "etc").exists())

    def test_bad_url_or_identity_cannot_inject_environment(self):
        for key, value in (("platform_url", "https://api.example.com\nEVIL=1"), ("audience", "https://user:password@example.com"), ("computer_id", "1\nEVIL=1")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                image_boot.configure(**{**self.arguments, key: value})
            self.assertFalse((self.root / "etc").exists())
