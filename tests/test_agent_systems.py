"""Bounded public CLI probes and coding-agent context without model login."""

import asyncio
import copy
import json
import os
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from hermes_runtime import agent_systems
from hermes_runtime.main import RuntimeContext, _heartbeat_metrics, _heartbeat_once, _maybe_start_gateway_reconcile

CONTEXT = {"schema": agent_systems.CONTEXT_SCHEMA, "computer_id": "cmp_" + "c" * 32,
           "agent_id": "agt_" + "a" * 22, "system": "codex"}
INVENTORY = {"schema": agent_systems.INVENTORY_SCHEMA, "desktop_ready": True,
             **{key: {"ready": True, "version": "1.0.0"} for key in agent_systems.SYSTEM_COMMANDS}}


class AgentSystemsTests(TestCase):
    def setUp(self):
        flag = patch.dict(os.environ, {"TINYHAT_AGENT_SYSTEMS_PREINSTALLED": "1"})
        flag.start()
        self.addCleanup(flag.stop)

    def test_public_cli_inventory_never_calls_auth_or_gateway(self):
        async def process(args, **kwargs):
            self.assertEqual(args[1:], ["--version"])
            self.assertEqual(kwargs["timeout_seconds"], 5)
            return {"ok": not args[0].endswith("openclaw"), "stdout": "example 1.0\n", "stderr": ""}
        with patch.object(agent_systems.shutil, "which", side_effect=lambda name: "/bin/" + name), patch.object(agent_systems, "find_hermes_binary", return_value=Path("/bin/hermes")), patch.object(agent_systems, "run_process", side_effect=process):
            inventory = asyncio.run(agent_systems.inventory())
        self.assertTrue(inventory["desktop_ready"])
        self.assertTrue(inventory["codex"]["ready"])
        self.assertFalse(inventory["openclaw"]["ready"])
        self.assertFalse(agent_systems.ready(inventory))

    def test_context_writes_private_metadata_and_preserves_user_launcher(self):
        ctx = SimpleNamespace(agent_systems_inventory=INVENTORY)
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            agent_systems.apply_context(ctx, CONTEXT, home=home)
            saved = home / ".config/tinyhat/computer.json"
            self.assertEqual(json.loads(saved.read_text()), CONTEXT)
            self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)
            launcher = home / ".local/bin/tinyhat-agent"
            self.assertIn('exec codex "$@"', launcher.read_text())
            self.assertIn("Terminal=true", (home / "Desktop/Tinyhat Agent.desktop").read_text())
            launcher.write_text("# user customization\n")
            agent_systems.apply_context(ctx, CONTEXT, home=home)
            self.assertEqual(launcher.read_text(), "# user customization\n")
            self.assertTrue(agent_systems.acknowledgement(ctx)["ready"])

    def test_unready_inventory_recovers_on_identical_context(self):
        ctx = SimpleNamespace(agent_systems_inventory=None)
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            agent_systems.apply_context(ctx, CONTEXT, home=home)
            self.assertFalse(agent_systems.acknowledgement(ctx)["ready"])
            self.assertFalse((home / ".config/tinyhat/computer.json").exists())
            ctx.agent_systems_inventory = INVENTORY
            agent_systems.apply_context(ctx, CONTEXT, home=home)
            self.assertTrue(agent_systems.acknowledgement(ctx)["ready"])

    def test_unknown_context_cannot_inject_a_command(self):
        ctx = SimpleNamespace(agent_systems_inventory=INVENTORY)
        for key, value in (("system", "codex; touch /tmp/injected"), ("agent_id", "agt_../bad"), ("schema", "unknown")):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ValueError):
                    agent_systems.apply_context(ctx, {**CONTEXT, key: value}, home=Path(directory))
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_disabled_images_do_not_probe_installed_apps(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(agent_systems, "inventory", new_callable=AsyncMock) as probe:
            asyncio.run(agent_systems.refresh_inventory(SimpleNamespace()))
            probe.assert_not_awaited()

    def test_agent_api_context_skips_telegram_gateway(self):
        ctx = SimpleNamespace(agent_api_context=CONTEXT, gateway_reconcile_task=None)
        with patch("hermes_runtime.main._telegram_env_configured", side_effect=AssertionError("Telegram must not be consulted")):
            _maybe_start_gateway_reconcile(ctx)

    def test_disabled_image_ignores_assignment_and_keeps_telegram_reconcile(self):
        with tempfile.TemporaryDirectory() as directory:
            platform = SimpleNamespace(post_json=AsyncMock(return_value={"state": "assigned", "agent_api_context": CONTEXT}))
            ctx = RuntimeContext(platform=platform, state_dir=Path(directory) / "state", started_at=0)
            with patch.dict(os.environ, {"TINYHAT_AGENT_SYSTEMS_PREINSTALLED": "0"}), patch("hermes_runtime.main._telegram_env_configured", return_value=False) as telegram, patch.object(agent_systems, "inventory", new_callable=AsyncMock) as probe:
                asyncio.run(_heartbeat_once(ctx))
                probe.assert_not_awaited()
                telegram.assert_called()
                self.assertIsNone(ctx.agent_api_context)
                self.assertIsNone(agent_systems.acknowledgement(ctx))
                self.assertFalse(ctx.agent_api_context_ready)
                self.assertNotIn("agent_api", platform.post_json.call_args.args[1]["metrics"])

    def test_unexecutable_cli_does_not_interrupt_heartbeat(self):
        async def process(args, **kwargs):
            if args[0].endswith("codex"):
                raise OSError("invalid executable format")
            return {"ok": True, "stdout": "example 1.0\n"}
        with tempfile.TemporaryDirectory() as directory:
            platform = SimpleNamespace(post_json=AsyncMock(return_value={"state": "ready"}))
            ctx = RuntimeContext(platform=platform, state_dir=Path(directory) / "state", started_at=0)
            with patch.object(agent_systems.shutil, "which", side_effect=lambda name: "/bin/" + name), patch.object(agent_systems, "find_hermes_binary", return_value=Path("/bin/hermes")), patch.object(agent_systems, "run_process", side_effect=process):
                asyncio.run(_heartbeat_once(ctx))
            platform.post_json.assert_awaited_once()
            metrics = platform.post_json.call_args.args[1]["metrics"]
            self.assertEqual(metrics["agent_systems"]["codex"], {"ready": False, "version": None})
            self.assertTrue(metrics["agent_systems"]["claude_code"]["ready"])

    def test_heartbeat_acknowledges_context_and_bounds_inventory_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            platform = SimpleNamespace(post_json=AsyncMock(return_value={"state": "assigned", "agent_api_context": CONTEXT}))
            ctx = RuntimeContext(platform=platform, state_dir=Path(directory) / "state", started_at=0)
            with patch.dict(os.environ, {"TINYHAT_AGENT_SYSTEMS_PREINSTALLED": "1"}), patch.object(agent_systems, "inventory", new_callable=AsyncMock, return_value=copy.deepcopy(INVENTORY)) as probe, patch.object(agent_systems.Path, "home", return_value=Path(directory)):
                async def walk():
                    await _heartbeat_once(ctx)
                    await _heartbeat_once(ctx)
                asyncio.run(walk())
                probe.assert_awaited_once()
                payload = platform.post_json.call_args.args[1]["metrics"]
                self.assertEqual(payload["agent_api"], {**CONTEXT, "ready": True})
                self.assertTrue(payload["hermes_runtime"]["capabilities"]["agent_api"])
                self.assertTrue(payload["agent_systems"]["desktop_ready"])
