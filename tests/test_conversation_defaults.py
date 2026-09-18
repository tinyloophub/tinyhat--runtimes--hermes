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
