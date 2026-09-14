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
            snapshot_channel=Mock(return_value={"SLACK_BOT_TOKEN": "previous"}),
            restore_channel=Mock(),
            record_applied=Mock(),
        )

        @contextmanager
        def adapter():
            yield self.adapter

        self.patches = [
            patch.object(
                command, "_telegram_delete_webhook", return_value={"ok": True}
            ),
            patch.object(
                command,
                "_configure_tinyhat_menu_button",
                AsyncMock(return_value={"configured": True}),
            ),
            patch.object(command, "_prepare_telegram"),
            patch.object(
                command,
                "_snapshot_webhook",
                return_value={"url": "https://example.com/private-hook"},
            ),
            patch.object(command, "_restore_webhook"),
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
            with self.assertRaises(RuntimeError):
                await command.run(self.ctx, self.input)
        self.adapter.record_applied.assert_called_once_with(
            self.config["assignment"], "slack", "rev1"
        )
        self.assertEqual(self.adapter.restore_channel.call_count, 1)
        command._restore_webhook.assert_called_once_with(
            "disposable", {"url": "https://example.com/private-hook"}
        )
        acknowledgements = [
            call.args[1]
            for call in self.platform.post_json.call_args_list
            if call.args[0].endswith("/applied")
        ]
        self.assertIn(
            {
                "assignment": self.config["assignment"],
                "provider": "slack",
                "revision": "rev1",
                "connected": True,
                "error": None,
            },
            acknowledgements,
        )

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
        with self.assertRaises(RuntimeError) as failure:
            await command.run(self.ctx, self.input)
        self.assertNotIn("private-value", str(failure.exception))
        self.assertNotIn("private-value", str(self.platform.post_json.call_args_list))
        self.adapter.restore_channel.assert_called_once()

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

    async def test_unhealthy_gateway_restores_old_values_and_is_failed(self):
        command._run_gateway_for_managed_setup.side_effect = [
            ({"healthy": False}, {}),
            ({"healthy": True}, {}),
        ]
        with self.assertRaises(RuntimeError):
            await command.run(self.ctx, self.input)
        self.adapter.restore_channel.assert_called_once_with(
            {"SLACK_BOT_TOKEN": "previous"}
        )
        self.adapter.record_applied.assert_not_called()
        self.assertEqual(
            self.platform.post_json.call_args.args[1]["error"], "gateway_unavailable"
        )

    async def test_transient_fence_failure_does_not_stop_existing_gateway(self):
        self.platform.get_json.side_effect = [self.config, self.config, TimeoutError()]
        with patch("hermes_runtime.commands.stop_hermes.run", AsyncMock()) as stop:
            with self.assertRaises(RuntimeError):
                await command.run(self.ctx, self.input)
            stop.assert_not_awaited()
        command._run_gateway_for_managed_setup.assert_not_awaited()

    async def test_unknown_readiness_keeps_healthy_gateway_without_rollback(self):
        command._connected.return_value = {"slack": None}
        with self.assertRaises(RuntimeError):
            await command.run(self.ctx, self.input)
        self.assertEqual(
            self.platform.post_json.call_args.args[1]["error"], "readiness_unknown"
        )
        self.adapter.restore_channel.assert_not_called()
        self.assertEqual(command._run_gateway_for_managed_setup.await_count, 1)

    async def test_unreadable_snapshot_never_changes_the_channel(self):
        self.adapter.snapshot_channel.side_effect = PermissionError("unreadable")
        with self.assertRaises(RuntimeError):
            await command.run(self.ctx, self.input)
        self.adapter.install_channel.assert_not_called()
        self.adapter.restore_channel.assert_not_called()
        command._run_gateway_for_managed_setup.assert_not_awaited()

    async def test_telegram_success_survives_slack_failure(self):
        telegram = {
            "provider": "telegram",
            "revision": "rev2",
            "status": "pending",
            "bot_token": "disposable",
            "owner_id": "12345",
            "settings_miniapp_url": "https://example.com/settings",
        }
        self.config["channels"] = [telegram, self.channel]
        command._connected.return_value = {"telegram": True, "slack": False}
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
            with self.assertRaises(RuntimeError):
                await command.run(self.ctx, self.input)
        self.adapter.record_applied.assert_called_once_with(
            self.config["assignment"], "telegram", "rev2"
        )
        self.adapter.restore_channel.assert_called_once()
        command._restore_webhook.assert_not_called()

    async def test_unknown_connected_provider_is_ignored(self):
        self.config["channels"] = [
            {"provider": "future", "revision": 1, "status": "connected"}
        ]
        self.assertFalse((await command.run(self.ctx, self.input))["changed"])
        self.adapter.applied_revision.assert_not_called()

    def add_telegram(self):
        self.config["channels"].append(
            {
                "provider": "telegram",
                "revision": "rev2",
                "status": "pending",
                "bot_token": "disposable",
                "owner_id": "12345",
                "settings_miniapp_url": "https://example.com/settings",
            }
        )

    def latest_ack(self, provider):
        return [
            c.args[1]
            for c in self.platform.post_json.call_args_list
            if c.args[0].endswith("/applied") and c.args[1]["provider"] == provider
        ][-1]

    async def test_failed_recovery_downgrades_previously_connected_sibling(self):
        self.add_telegram()
        command._run_gateway_for_managed_setup.side_effect = [
            ({"healthy": True}, {}),
            ({"healthy": True}, {}),
            ({"healthy": False}, {}),
        ]
        command._connected.side_effect = [
            {"slack": True},
            {"telegram": False},
            {"slack": True},
        ]
        with self.assertRaises(RuntimeError):
            await command.run(self.ctx, self.input)
        self.assertFalse(self.latest_ack("slack")["connected"])
        self.assertEqual(self.latest_ack("slack")["error"], "gateway_unavailable")
        self.assertFalse(self.latest_ack("telegram")["connected"])

    async def test_successful_restart_rechecks_an_already_applied_sibling(self):
        self.add_telegram()
        self.channel["status"] = "connected"
        self.adapter.applied_revision.return_value = "rev1"
        command._connected.side_effect = [{"telegram": True}, {"slack": False}]
        with self.assertRaises(RuntimeError):
            await command.run(self.ctx, self.input)
        self.assertFalse(self.latest_ack("slack")["connected"])
        self.assertTrue(self.latest_ack("telegram")["connected"])
        self.adapter.install_channel.assert_called_once()

    async def test_unchanged_sibling_keeps_success_when_readiness_is_unknown(self):
        self.add_telegram()
        self.channel["status"] = "connected"
        self.adapter.applied_revision.return_value = "rev1"
        command._connected.side_effect = [{"telegram": True}, {"slack": None}]
        result = await command.run(self.ctx, self.input)
        self.assertEqual(
            result["channels"][0], {"provider": "slack", "status": "connected"}
        )
        self.assertEqual(command._connected.call_args.args[0], ["slack"])
        self.assertGreater(command._connected.call_args.args[1], 0)
        self.assertTrue(command._connected.call_args.kwargs["survivors"])
        self.adapter.restore_channel.assert_not_called()
        self.assertTrue(self.latest_ack("telegram")["connected"])

    async def test_failure_report_transport_error_does_not_skip_local_recovery(self):
        self.add_telegram()
        self.config["channels"] = [self.config["channels"][-1]]
        command._connected.return_value = {"telegram": False}

        async def report(path, payload):
            if path.endswith("/applied"):
                raise OSError("transport unavailable")
            return {}

        self.platform.post_json.side_effect = report
        with self.assertRaises(RuntimeError):
            await command.run(self.ctx, self.input)
        self.adapter.restore_channel.assert_called_once()
        self.assertEqual(command._run_gateway_for_managed_setup.await_count, 2)
        command._restore_webhook.assert_called_once()

    async def test_unsupported_pending_provider_does_not_block_supported_sibling(self):
        self.config["channels"].insert(
            0,
            {"provider": "future_provider", "revision": "future1", "status": "pending"},
        )
        with self.assertRaises(RuntimeError):
            await command.run(self.ctx, self.input)
        self.assertEqual(self.latest_ack("future_provider")["error"], "setup_failed")
        self.assertTrue(self.latest_ack("slack")["connected"])
        self.adapter.record_applied.assert_called_once_with(
            self.config["assignment"], "slack", "rev1"
        )

    async def test_unsupported_row_without_revision_skips_ack_but_sets_up_sibling(self):
        self.config["channels"].insert(0, {"provider": "future", "status": "pending"})
        with self.assertLogs(command.logger, level="WARNING") as logs:
            with self.assertRaises(RuntimeError):
                await command.run(self.ctx, self.input)
        self.assertTrue(any("has no revision" in line for line in logs.output))
        self.assertFalse(
            any(
                c.args[1].get("provider") == "future"
                for c in self.platform.post_json.call_args_list
            )
        )
        self.assertTrue(self.latest_ack("slack")["connected"])

    async def test_unsupported_only_returns_per_channel_failure_without_hermes(self):
        self.config["channels"] = [
            {"provider": "future", "revision": "rev", "status": "pending"}
        ]
        command.find_hermes_binary.return_value = None
        result = await command.run(self.ctx, self.input)
        self.assertFalse(result["changed"])
        self.assertEqual(
            result["channels"],
            [{"provider": "future", "status": "failed", "error": "setup_failed"}],
        )
        command._run_gateway_for_managed_setup.assert_not_awaited()

    async def test_unsupported_provider_report_rejection_preserves_supported_setup(
        self,
    ):
        from hermes_runtime.client import PlatformError

        self.config["channels"].insert(
            0,
            {"provider": "future_provider", "revision": "future1", "status": "pending"},
        )

        async def report(path, payload):
            if payload.get("provider") == "future_provider":
                raise PlatformError("unsupported provider", status_code=422)
            return {}

        self.platform.post_json.side_effect = report
        with self.assertRaises(RuntimeError):
            await command.run(self.ctx, self.input)
        self.assertTrue(self.latest_ack("slack")["connected"])

    async def test_webhook_recovery_failure_still_acknowledges_and_rechecks(self):
        self.add_telegram()
        command._connected.side_effect = [
            {"slack": True},
            {"telegram": False},
            {"slack": True},
            {"slack": False},
        ]
        command._restore_webhook.side_effect = command.ChannelSetupError(
            "network_unavailable"
        )
        with self.assertRaises(RuntimeError):
            await command.run(self.ctx, self.input)
        self.assertFalse(self.latest_ack("slack")["connected"])
        self.assertEqual(self.latest_ack("telegram")["error"], "gateway_unavailable")

    async def test_restore_failure_cannot_swallow_failed_acknowledgement(self):
        command._connected.return_value = {"slack": False}
        self.adapter.restore_channel.side_effect = ValueError("private-detail")
        with self.assertRaises(RuntimeError):
            await command.run(self.ctx, self.input)
        self.assertEqual(self.latest_ack("slack")["error"], "gateway_unavailable")
        self.assertNotIn("private-detail", str(self.platform.post_json.call_args_list))

    async def test_confirmed_rejection_during_recovery_is_not_masked(self):
        from hermes_runtime.client import PlatformError

        command._connected.return_value = {"slack": False}
        self.adapter.restore_channel.side_effect = PlatformError(
            "stale", status_code=409
        )
        with patch("hermes_runtime.commands.stop_hermes.run", AsyncMock()) as stop:
            with self.assertRaises(RuntimeError):
                await command.run(self.ctx, self.input)
            stop.assert_awaited_once()

    async def test_http_settings_origin_reports_settings_error_before_writes(self):
        self.add_telegram()
        self.config["channels"] = [self.config["channels"][-1]]
        self.config["channels"][0]["settings_miniapp_url"] = (
            "http://localhost:3000/settings"
        )
        with self.assertRaises(RuntimeError):
            await command.run(self.ctx, self.input)
        self.assertEqual(self.latest_ack("telegram")["error"], "settings_unavailable")
        self.adapter.install_channel.assert_not_called()


class TelegramPreparationTests(unittest.TestCase):
    def test_missing_menu_url_never_prepares_or_installs(self):
        with patch.object(command, "ensure_telegram_network_fallback_env") as network:
            with self.assertRaises(command.ChannelSetupError) as failure:
                command._prepare_telegram({})
            self.assertEqual(failure.exception.code, "settings_unavailable")
            network.assert_not_called()

    def test_network_fallback_is_required_before_quick_commands(self):
        channel = {"settings_miniapp_url": "https://example.com/computer"}
        with (
            patch.object(
                command,
                "ensure_telegram_network_fallback_env",
                return_value={"ok": False},
            ),
            patch.object(command, "_install_codex_auth_quick_commands") as quick,
        ):
            with self.assertRaises(command.ChannelSetupError) as failure:
                command._prepare_telegram(channel)
            self.assertEqual(failure.exception.code, "network_unavailable")
            quick.assert_not_called()


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
            saved = state["platforms"].pop("slack")
            path.write_text(json.dumps(state))
            self.assertIsNone(
                readiness._runtime_state_telegram_evidence(
                    path, service_main_pid=123, since_unix=1, provider="slack"
                )
            )
            state["platforms"]["slack"] = saved
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

    def test_survivor_inherited_connected_row_is_unknown_until_refreshed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway_state.json"
            state = {
                "pid": 123,
                "gateway_state": "running",
                "updated_at": "1970-01-01T00:20:00+00:00",
                "platforms": {
                    "slack": {
                        "state": "connected",
                        "updated_at": "1970-01-01T00:15:00+00:00",
                    }
                },
            }
            path.write_text(json.dumps(state))
            args = dict(service_main_pid=123, since_unix=1000, provider="slack")
            self.assertFalse(readiness._runtime_state_telegram_evidence(path, **args))
            self.assertIsNone(
                readiness._runtime_state_telegram_evidence(
                    path, **args, stale_is_unknown=True
                )
            )
            state["platforms"]["slack"]["updated_at"] = state["updated_at"]
            path.write_text(json.dumps(state))
            self.assertTrue(
                readiness._runtime_state_telegram_evidence(
                    path, **args, stale_is_unknown=True
                )
            )
            state["platforms"]["slack"]["state"] = "disconnected"
            path.write_text(json.dumps(state))
            self.assertFalse(
                readiness._runtime_state_telegram_evidence(
                    path, **args, stale_is_unknown=True
                )
            )


class SurvivorPollingTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_survivor_does_not_wait_or_probe_again(self):
        with (
            patch.object(
                readiness, "connected_channel_states", return_value={"slack": None}
            ) as probe,
            patch.object(command.asyncio, "sleep", AsyncMock()) as sleep,
        ):
            self.assertEqual(
                await command._connected(["slack"], 1000, survivors=True),
                {"slack": None},
            )
            probe.assert_called_once_with(
                ["slack"], since_unix=1000, stale_is_unknown=True
            )
            sleep.assert_not_awaited()


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


class WebhookRecoveryTests(unittest.TestCase):
    def test_preserves_observed_webhook_and_pending_updates(self):
        with patch.object(
            command,
            "_telegram_transport",
            return_value={
                "url": "https://example.com/private-hook",
                "allowed_updates": ["message"],
                "max_connections": 30,
            },
        ) as transport:
            snapshot = command._snapshot_webhook("secret-token")
            command._restore_webhook("secret-token", snapshot)
            self.assertEqual(
                transport.call_args.args,
                (
                    "secret-token",
                    "setWebhook",
                    {
                        "url": "https://example.com/private-hook",
                        "allowed_updates": ["message"],
                        "max_connections": 30,
                        "drop_pending_updates": False,
                    },
                ),
            )

    def test_missing_update_subscription_is_not_narrowed(self):
        with patch.object(
            command,
            "_telegram_transport",
            return_value={"url": "https://example.com/hook"},
        ):
            self.assertNotIn(
                "allowed_updates", command._snapshot_webhook("secret-token")
            )

    def test_previous_polling_does_not_set_a_webhook(self):
        with patch.object(
            command, "_telegram_transport", return_value={"url": ""}
        ) as transport:
            snapshot = command._snapshot_webhook("secret-token")
            transport.reset_mock()
            command._restore_webhook("secret-token", snapshot)
            transport.assert_not_called()


class TelegramFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_journal_probe_is_bound_to_the_same_invocation(self):
        generation = {"main_pid": 42, "invocation_id": "test-invocation"}
        with (
            patch(
                "hermes_runtime.gateway_service.discover_gateway_service",
                AsyncMock(
                    return_value={
                        "ok": True,
                        "generation": generation,
                        "owner": {"manager": "system"},
                    }
                ),
            ),
            patch(
                "hermes_runtime.gateway_service.gateway_generation_active",
                return_value=True,
            ),
            patch(
                "hermes_runtime.gateway_service.snapshot_gateway_service",
                AsyncMock(return_value=generation),
            ),
            patch(
                "hermes_runtime.gateway_service.gateway_generation_same",
                return_value=True,
            ) as same,
            patch(
                "hermes_runtime.gateway_readiness.probe_functional_readiness",
                AsyncMock(
                    return_value={"status_healthy": True, "telegram_connected": True}
                ),
            ) as probe,
        ):
            self.assertTrue(
                await command._telegram_fallback(Path("/bin/hermes"), 100, 5)
            )
            self.assertEqual(
                probe.call_args.kwargs["service_invocation_id"], "test-invocation"
            )
            self.assertEqual(probe.call_args.kwargs["since_unix"], 100)
            probe.return_value = {"status_healthy": False, "telegram_connected": None}
            self.assertIsNone(
                await command._telegram_fallback(Path("/bin/hermes"), 100, 5)
            )
            same.return_value = False
            self.assertIsNone(
                await command._telegram_fallback(Path("/bin/hermes"), 100, 5)
            )

    async def test_fallback_does_not_use_an_unidentified_foreground_log(self):
        with (
            patch(
                "hermes_runtime.gateway_service.discover_gateway_service",
                AsyncMock(return_value={"ok": False}),
            ),
            patch(
                "hermes_runtime.commands.configure_telegram._active_gateway_foreground_generation",
                return_value=None,
            ),
            patch(
                "hermes_runtime.gateway_readiness.probe_functional_readiness",
                AsyncMock(),
            ) as probe,
        ):
            self.assertIsNone(
                await command._telegram_fallback(Path("/bin/hermes"), 100, 5)
            )
            probe.assert_not_awaited()
