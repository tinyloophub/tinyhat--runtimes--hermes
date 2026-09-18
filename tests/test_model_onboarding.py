"""Fresh chat setup must not depend on email or replace owner model choices."""
import io
import json
import tempfile
import importlib.util
from pathlib import Path
from unittest import TestCase, skipUnless
from unittest.mock import patch

from hermes_runtime import model_onboarding as model


@skipUnless(importlib.util.find_spec("yaml"), "YAML runs in the Hermes interpreter")
class ModelOnboardingTests(TestCase):
    def run_setup(self, home, setup):
        output = io.StringIO()
        with patch.object(model.sys, "stdin", io.StringIO(json.dumps({
            "home": str(home), "setup": setup,
        }))), patch.object(model.sys, "stdout", output):
            model._configure_file()
        return json.loads(output.getvalue())

    def test_fresh_model_without_email_is_private_and_idempotent(self):
        import yaml
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = home / "config.yaml"
            config.write_text("model:\n  provider: auto\nstreaming:\n  enabled: true\n")
            setup = {"openrouter_api_key": "fixture-key", "openrouter_default_model": "example/model"}
            self.assertTrue(self.run_setup(home, setup)["changed"])
            parsed = yaml.safe_load(config.read_text())
            self.assertEqual(parsed["model"]["default"], "example/model")
            self.assertTrue(parsed["streaming"]["enabled"])
            self.assertNotIn("platforms", parsed)
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertEqual((home / ".env").stat().st_mode & 0o777, 0o600)
            self.assertFalse(self.run_setup(home, setup)["changed"])

    def test_owner_model_or_provider_is_preserved_without_platform_secrets(self):
        import yaml
        for selection in ({"provider": "openai-codex"}, {"default": "owner/model"}, "owner/model"):
            with self.subTest(selection=selection), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                original = yaml.safe_dump({"model": selection})
                (home / "config.yaml").write_text(original)
                (home / ".env").write_text("OWNER_KEY=unchanged\n")
                self.assertFalse(self.run_setup(home, None)["changed"])
                self.assertEqual((home / "config.yaml").read_text(), original)
                self.assertEqual((home / ".env").read_text(), "OWNER_KEY=unchanged\n")

    def test_missing_access_does_not_write_a_broken_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with self.assertRaises(ValueError):
                self.run_setup(home, None)
            self.assertFalse((home / "config.yaml").exists())
            self.assertFalse((home / ".env").exists())
