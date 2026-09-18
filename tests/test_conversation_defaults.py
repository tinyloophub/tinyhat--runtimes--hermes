"""Fresh installs have an editable voice and streaming, preserving owner files."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_runtime.commands.install_hermes import (
    _seed_conversation_soul,
    _seed_unconfigured_model,
)
from hermes_runtime.conversation_defaults import DEFAULT_CONFIG, DEFAULT_SOUL


class ConversationDefaultsTests(unittest.TestCase):
    def test_image_builder_entrypoint_seeds_identity_before_upstream_install(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"HERMES_HOME": home}):
            # Image builders seed this sentinel, run the upstream installer,
            # then call run() with an already-installed Hermes binary.
            _seed_unconfigured_model()
            self.assertEqual((Path(home) / "SOUL.md").read_text(), DEFAULT_SOUL)
            self.assertEqual((Path(home) / "config.yaml").read_text(), DEFAULT_CONFIG)

    def test_fresh_install_and_retries_preserve_owner_preferences(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"HERMES_HOME": home}):
            _seed_conversation_soul()
            _seed_unconfigured_model()
            soul, config = Path(home) / "SOUL.md", Path(home) / "config.yaml"
            self.assertEqual(soul.read_text(), DEFAULT_SOUL)
            self.assertEqual(config.read_text(), DEFAULT_CONFIG)
            soul.write_text("Speak in French. Keep my chosen personality.\n")
            config.write_text("model: owner/chosen\nstreaming:\n  enabled: false\n")
            _seed_conversation_soul()
            _seed_unconfigured_model()
            self.assertEqual(soul.read_text(), "Speak in French. Keep my chosen personality.\n")
            self.assertEqual(config.read_text(), "model: owner/chosen\nstreaming:\n  enabled: false\n")
