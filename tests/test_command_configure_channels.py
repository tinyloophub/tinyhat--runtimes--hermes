"""Channel command retries, assignment fences and provider-specific readiness."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from contextlib import contextmanager

from hermes_runtime.commands import configure_channels as command
from hermes_runtime import gateway_readiness as readiness


class ConfigureChannelsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.channel = {
            "provider": "slack",
            "revision": "rev1",
            "status": "pending",
            "ciphertext": {},
        }
        self.config = {
            "assignment": "account:user:agent:date",
            "channels": [self.channel],
        }
        self.platform = SimpleNamespace(
            get_json=AsyncMock(side_effect=lambda _: self.config),
            post_json=AsyncMock(return_value={}),
        )
        self.ctx = SimpleNamespace(platform=self.platform, platform_auth="gcloud")
        self.adapter = SimpleNamespace(
            prepare_key=Mock(return_value={"public_key_pem": "public"}),
            slack_manifest=Mock(return_value={}),
            applied_revision=Mock(return_value=None),
            install_channel=Mock(),
            record_applied=Mock(),
        )

        @contextmanager
        def adapter():
            yield self.adapter

        self.patches = [
            patch.object(command, "channel_adapter", adapter),
            patch.object(
                command, "find_hermes_binary", return_value=Path("/bin/hermes")
            ),
            patch.object(
                command,
                "_run_gateway_for_managed_setup",
                AsyncMock(return_value=({"healthy": True}, {})),
            ),
            patch.object(
                command,
                "_connected",
                AsyncMock(return_value={"slack": True, "telegram": False}),
            ),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.input = {"spec": {"assignment": self.config["assignment"]}}

    async def test_slack_only_does_not_require_or_change_telegram(self):
        result = await command.run(self.ctx, self.input)
        self.assertEqual(
            result["channels"], [{"provider": "slack", "status": "connected"}]
        )
        self.adapter.record_applied.assert_called_once_with(
            self.config["assignment"], "slack", "rev1"
        )
        self.assertIn("?assignment=", self.platform.get_json.call_args.args[0])

    async def test_global_health_does_not_imply_telegram_connected(self):
        self.config["channels"].append(
            {
                "provider": "telegram",
                "revision": "rev2",
                "status": "pending",
                "bot_token": "disposable",
                "owner_id": "12345",
                "settings_miniapp_url": "https://app.example.com/tinyhat/miniapp/computers/1",
            }
        )
        with (
            patch.object(
                command, "_telegram_delete_webhook", return_value={"ok": True}
            ),
            patch.object(
                command,
                "_configure_tinyhat_menu_button",
                AsyncMock(return_value={"configured": True}),
            ),
        ):
            result = await command.run(self.ctx, self.input)
        self.assertIn({"provider": "telegram", "status": "failed"}, result["channels"])
        self.assertEqual(self.adapter.record_applied.call_count, 1)

    async def test_connected_revision_is_noop_and_empty_setup_only_prepares_key(self):
        self.channel["status"] = "connected"
        self.adapter.applied_revision.return_value = "rev1"
        result = await command.run(self.ctx, self.input)
        self.assertFalse(result["changed"])
        self.adapter.install_channel.assert_not_called()
        command._run_gateway_for_managed_setup.assert_not_awaited()
        self.config["channels"] = []
        self.assertFalse((await command.run(self.ctx, self.input))["changed"])

    async def test_stale_command_never_receives_or_installs_new_assignment(self):
        self.config["assignment"] = "new-owner"
        with self.assertRaises(RuntimeError):
            await command.run(self.ctx, self.input)
        self.adapter.prepare_key.assert_not_called()
        self.adapter.install_channel.assert_not_called()

    async def test_assignment_change_after_write_stops_gateway_before_return(self):
        self.platform.get_json.side_effect = [
            self.config,
            self.config,
            {"assignment": "new-owner"},
        ]
        with patch("hermes_runtime.commands.stop_hermes.run", AsyncMock()) as stop:
            with self.assertRaises(RuntimeError):
                await command.run(self.ctx, self.input)
            stop.assert_awaited_once()
        command._run_gateway_for_managed_setup.assert_not_awaited()
        self.adapter.record_applied.assert_not_called()

    async def test_provider_error_never_leaks_credentials(self):
        self.adapter.install_channel.side_effect = ValueError("xoxb-private-value")
        result = await command.run(self.ctx, self.input)
        self.assertNotIn("private-value", json.dumps(result))
        self.assertNotIn("private-value", str(self.platform.post_json.call_args_list))
        self.assertEqual(
            result["channels"], [{"provider": "slack", "status": "failed"}]
        )

    async def test_uncertain_acknowledgement_preserves_healthy_gateway(self):
        self.platform.post_json.side_effect = [{}, TimeoutError()]
        with patch("hermes_runtime.commands.stop_hermes.run", AsyncMock()) as stop:
            with self.assertRaises(RuntimeError):
                await command.run(self.ctx, self.input)
            stop.assert_not_awaited()
        self.adapter.record_applied.assert_not_called()

    async def test_rejected_assignment_acknowledgement_stops_gateway(self):
        from hermes_runtime.client import PlatformError

        self.platform.post_json.side_effect = [
            {},
            PlatformError("stale", status_code=409),
        ]
        with patch("hermes_runtime.commands.stop_hermes.run", AsyncMock()) as stop:
            with self.assertRaises(RuntimeError):
                await command.run(self.ctx, self.input)
            stop.assert_awaited_once()


class ProviderReadinessTests(unittest.TestCase):
    def test_slack_requires_its_own_fresh_state_and_matching_process(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway_state.json"
            state = {
                "pid": 123,
                "gateway_state": "running",
                "updated_at": "2026-09-13T00:00:10+00:00",
                "platforms": {
                    "telegram": {
                        "state": "connected",
                        "updated_at": "2026-09-13T00:00:10+00:00",
                    },
                    "slack": {
                        "state": "disconnected",
                        "updated_at": "2026-09-13T00:00:10+00:00",
                    },
                },
            }
            path.write_text(json.dumps(state))
            self.assertFalse(
                readiness._runtime_state_telegram_evidence(
                    path, service_main_pid=123, since_unix=1, provider="slack"
                )
            )
            state["platforms"]["slack"]["state"] = "connected"
            path.write_text(json.dumps(state))
            self.assertTrue(
                readiness._runtime_state_telegram_evidence(
                    path, service_main_pid=123, since_unix=1, provider="slack"
                )
            )
            self.assertIsNone(
                readiness._runtime_state_telegram_evidence(
                    path, service_main_pid=456, since_unix=1, provider="slack"
                )
            )


class AdapterLoadingTests(unittest.TestCase):
    def test_supervisor_loads_adapter_without_hermes_entrypoint_dependencies(self):
        import sys

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "__init__.py").write_text(
                'raise RuntimeError("Hermes tool dependencies must not load")'
            )
            adapter = root / "capabilities/channels/runtime.py"
            adapter.parent.mkdir(parents=True)
            adapter.write_text('CONTRACT = "standard-library-only"')
            with patch.object(command, "plugin_dir", return_value=root):
                with command.channel_adapter() as loaded:
                    self.assertEqual(loaded.CONTRACT, "standard-library-only")
            self.assertFalse(
                any(name.startswith(command._PACKAGE) for name in sys.modules)
            )
