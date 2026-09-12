"""Email setup through public config without overwriting owner choices."""

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

import yaml
from hermes_runtime import email_onboarding

VALUES = {
    "TINYHAT_EMAIL_CHANNEL_ENABLED": "1",
    "TINYHAT_EMAIL_OWNER": "owner@example.test",
    "TINYHAT_MAILBOX_ADDRESS": "agent@example.test",
    "TINYHAT_MAILBOX_PASSWORD": "fixture",
    "TINYHAT_MAILBOX_JMAP_URL": "https://mail.example.test/.well-known/jmap",
    "OPENROUTER_API_KEY": "fixture",
    "TINYHAT_EMAIL_INITIAL_MODEL": "example/small",
}


class ConfigTests(TestCase):
    def test_initial_config_is_private_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            self.assertTrue(email_onboarding.configure(path, VALUES))
            config = yaml.safe_load((path / "config.yaml").read_text())
            self.assertEqual(config["plugins"]["enabled"], ["tinyhat"])
            self.assertFalse(
                config["display"]["platforms"]["tinyhat_email"]["streaming"]
            )
            self.assertEqual(config["model"]["provider"], "openrouter")
            self.assertFalse(email_onboarding.configure(path, VALUES))
            self.assertEqual((path / "config.yaml").stat().st_mode & 0o777, 0o600)
            self.assertNotIn("fixture", (path / "config.yaml").read_text())

    def test_existing_model_and_other_channels_survive(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            existing = {
                "model": {"provider": "openai-codex", "default": "owner/model"},
                "plugins": {"enabled": ["other"]},
                "platforms": {"telegram": {"enabled": True}},
            }
            (path / "config.yaml").write_text(yaml.safe_dump(existing))
            email_onboarding.configure(path, VALUES)
            result = yaml.safe_load((path / "config.yaml").read_text())
            self.assertEqual(result["model"], existing["model"])
            self.assertEqual(
                result["platforms"]["telegram"], existing["platforms"]["telegram"]
            )
            self.assertEqual(result["plugins"]["enabled"], ["other", "tinyhat"])


class ScheduleTests(IsolatedAsyncioTestCase):
    async def test_slow_mail_setup_never_blocks_heartbeat_or_duplicates_task(self):
        gate = asyncio.Event()

        async def slow(ctx):
            await gate.wait()

        ctx = SimpleNamespace(agent_api_context_ready=True)
        with patch.object(email_onboarding, "reconcile", side_effect=slow) as reconcile:
            email_onboarding.schedule(ctx)
            first = ctx.email_setup_task
            await asyncio.sleep(0)
            for _ in range(10):
                email_onboarding.schedule(ctx)
            self.assertIs(ctx.email_setup_task, first)
            self.assertEqual(reconcile.call_count, 1)
            gate.set()
            await first

    async def test_unassigned_computers_do_not_get_email(self):
        with patch.object(email_onboarding, "reconcile", new_callable=AsyncMock) as run:
            email_onboarding.schedule(SimpleNamespace(agent_api_context_ready=False))
            run.assert_not_called()
