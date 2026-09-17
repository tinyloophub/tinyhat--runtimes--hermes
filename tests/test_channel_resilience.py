"""Regression checks for desktop ownership and independent channel recovery."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hermes_runtime.channel_agent.native import Codex, CodexRequestError
from hermes_runtime.channel_agent.service import Service
from hermes_runtime.channel_agent.state import State
from hermes_runtime.channel_agent.transports import Transports


class CodexWriterTests(unittest.IsolatedAsyncioTestCase):
    def test_workspace_skill_tracks_plugin_updates_and_ignores_workspace_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "plugin/skills/tinyhat-respond"
            source.mkdir(parents=True)
            (source / "SKILL.md").write_text("first policy")
            service = Service.__new__(Service)
            with patch(
                "hermes_runtime.channel_agent.service.plugin_dir",
                return_value=root / "plugin",
            ):
                path = service.codex_skill(root / "work", "tinyhat-respond")
                self.assertEqual(path, (source / "SKILL.md").resolve())
                (source / "SKILL.md").write_text("updated policy")
                self.assertEqual(path.read_text(), "updated policy")
                link = root / "work/.agents/skills/tinyhat-respond"
                link.unlink()
                link.mkdir()
                (link / "SKILL.md").write_text("owner override")
                with self.assertLogs(level="WARNING") as logs:
                    self.assertIsNone(
                        service.codex_skill(root / "work", "tinyhat-respond")
                    )
                self.assertIn("codex_skill_override_ignored", logs.output[0])
                self.assertEqual((link / "SKILL.md").read_text(), "owner override")

    async def test_owned_desktop_thread_forks_history_before_one_new_turn(self):
        agent = Codex(cwd=Path("/tmp"), approve=AsyncMock())
        calls, sessions = [], []

        async def request(method, params):
            calls.append((method, params))
            if method == "thread/resume":
                raise CodexRequestError(
                    {
                        "code": -32600,
                        "message": "thread 11111111-1111-1111-1111-111111111111 already has an active writer",
                    }
                )
            if method == "thread/fork":
                self.assertEqual(params["threadId"], "original")
                return {"thread": {"id": "continuation"}, "model": "test-model"}
            self.assertEqual(method, "turn/start")
            self.assertEqual(sessions, ["continuation"])
            agent.text = "answer"
            agent.done.set_result({"status": "completed"})
            return {"turn": {"id": "turn"}}

        agent.request = request
        result = await agent.turn(
            "new message",
            native_id="original",
            instructions="skill",
            on_session=sessions.append,
            skill_path=Path("/plugin/skills/tinyhat-respond/SKILL.md"),
        )
        self.assertEqual(result, ("continuation", "answer"))
        self.assertEqual(
            [c[0] for c in calls], ["thread/resume", "thread/fork", "turn/start"]
        )
        self.assertEqual(
            calls[-1][1]["input"][0],
            {
                "type": "skill",
                "name": "tinyhat-respond",
                "path": "/plugin/skills/tinyhat-respond/SKILL.md",
            },
        )

    async def test_other_errors_and_uncertain_turn_start_are_not_replayed(self):
        for error in [
            CodexRequestError({"code": -1, "message": "secret-value"}),
            TimeoutError(),
        ]:
            agent = Codex(cwd=Path("/tmp"), approve=AsyncMock())
            agent.request = AsyncMock(side_effect=error)
            with self.assertRaises(type(error)):
                await agent.turn("message", native_id="original", instructions="skill")
            self.assertEqual(agent.request.await_count, 1)
            self.assertNotIn("secret-value", str(error))
        agent = Codex(cwd=Path("/tmp"), approve=AsyncMock())
        agent.request = AsyncMock(
            side_effect=[{"thread": {"id": "original"}}, TimeoutError()]
        )
        with self.assertRaises(TimeoutError):
            await agent.turn("message", native_id="original", instructions="skill")
        self.assertEqual(
            [c.args[0] for c in agent.request.await_args_list],
            ["thread/resume", "turn/start"],
        )


class ResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp")
        self.root = Path(self.temp.name)
        (self.root / "control").write_text("local-control")
        (self.root / "capabilities/channels").mkdir(parents=True)
        (self.root / "capabilities/channels/methods.json").write_text('{"telegram": {}, "slack": {}}')
        self.state = State(self.root)
        with (
            patch(
                "hermes_runtime.channel_agent.transports.plugin_dir",
                return_value=self.root,
            ),
            patch(
                "hermes_runtime.channel_agent.transports.read_env_values",
                return_value={},
            ),
            patch.dict("os.environ", {}, clear=True),
        ):
            self.transport = Transports(self.state, self.state.ingest, AsyncMock())
        self.transport.session = SimpleNamespace(closed=False, close=AsyncMock())
        self.transport.request = AsyncMock(return_value={})

    async def asyncTearDown(self):
        await self.transport.close()
        self.state.close()
        self.temp.cleanup()

    async def test_failed_provider_startup_does_not_block_healthy_channel(self):
        self.transport.connected = {"telegram": False, "slack": False}
        ready = asyncio.Event()

        async def request(provider, method, params):
            if provider == "telegram":
                raise RuntimeError("token-bearing-provider-error")
            return {}

        async def slack():
            self.transport.connected["slack"] = True
            ready.set()
            await asyncio.Future()

        self.transport.request = request
        self.transport.slack = slack
        with self.assertLogs("hermes_runtime.channel_agent.transports") as logs:
            await self.transport.start()
            await asyncio.wait_for(ready.wait(), 1)
        self.assertTrue(self.transport.connected["slack"])
        self.assertFalse(self.transport.connected["telegram"])
        self.assertNotIn("token-bearing-provider-error", str(logs.output))

    async def test_cancelled_listener_restarts_and_drain_does_not_restart_it(self):
        self.transport.connected = {"telegram": False}
        self.transport.telegram = AsyncMock(side_effect=self.wait_forever)
        await self.transport.start()
        original = self.transport.listeners[0]
        original.cancel()
        await asyncio.gather(original, return_exceptions=True)
        self.transport.ensure_listeners()
        replacement = self.transport.listeners[0]
        self.assertIsNot(replacement, original)
        await self.transport.stop_intake()
        self.transport.ensure_listeners()
        self.assertEqual(self.transport.listeners, [])
        self.assertTrue(replacement.cancelled())

    async def wait_forever(self):
        await asyncio.Future()

    def event(self, provider="telegram", **extra):
        return {"provider": provider, "conversation": "123", "sender": "123",
                "message_id": "17", "event_id": "update", **extra}

    async def receipts(self):
        await asyncio.gather(*list(self.transport.receipt_jobs.values()))

    async def test_receipt_precedes_routing_even_for_voice_and_duplicate_is_silent(self):
        event = self.event(raw={"voice": {"file_id": "voice"}})
        self.assertTrue(self.transport.accept("update", event))
        self.assertFalse(self.transport.accept("update", event))
        await self.receipts()
        self.transport.request.assert_awaited_once_with(
            "telegram", "sendChatAction", {"chat_id": "123", "action": "typing"}
        )
        self.assertEqual(self.state.queued()[0]["state"], "queued")
        self.assertFalse(self.state.tasks())  # No router, transcription or worker.
        await self.transport.keep_typing("worker", event, 60)
        self.assertNotIn("receipt:update", self.transport.typing)
        self.assertIn("worker", self.transport.typing)

    async def test_slow_feedback_cannot_block_durable_intake_or_handoff(self):
        started = asyncio.Event()

        async def stalled(*args):
            started.set()
            await asyncio.Future()

        self.transport.request = stalled
        self.transport.accept("update", self.event(), cursor=("telegram_offset", 18))
        await asyncio.wait_for(started.wait(), 1)
        self.assertTrue(self.transport.accept("next", self.event(message_id="18")))
        self.assertEqual(self.state.setting("telegram_offset"), 18)
        self.assertEqual(len(self.state.queued()), 2)
        await asyncio.wait_for(self.transport.stop_receipt(self.event()), 1)
        self.assertNotIn("receipt:update", self.transport.receipt_jobs)
        self.assertNotIn("receipt:update", self.transport.typing)

    async def test_slack_receipt_falls_back_in_original_thread_then_clears(self):
        self.transport.request.side_effect = [RuntimeError("no agents support"), {}, {}]
        event = self.event("slack", thread_id="2.5")
        self.transport.accept("update", event)
        await self.receipts()
        calls = self.transport.request.await_args_list
        self.assertEqual(calls[0].args[1], "agents.sessions.setStatus")
        self.assertEqual(calls[1].args, (
            "slack", "assistant.threads.setStatus",
            {"channel_id": "123", "thread_ts": "2.5", "status": "Working…"},
        ))
        await self.transport.stop_receipt(event)
        self.assertEqual(self.transport.request.await_args.args[2]["status"], "")

    async def test_cancelled_slack_status_write_still_attempts_cleanup(self):
        started, cleared = asyncio.Event(), asyncio.Event()

        async def uncertain(provider, method, params):
            if params["status"] == "processing":
                started.set()
                await asyncio.Future()
            if params["status"] == "active":
                cleared.set()
            return {}

        self.transport.request = uncertain
        event = self.event("slack")
        self.transport.accept("update", event)
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.wait_for(self.transport.stop_receipt(event), 1)
        self.assertTrue(cleared.is_set())
        self.assertFalse(self.transport.typing)
        self.assertFalse(self.transport.receipt_events)

    async def test_quiet_preference_is_durable_and_conversation_scoped(self):
        event = self.event()
        self.transport.accept("update", event)
        await self.receipts()
        await self.transport.keep_typing("worker", event, 0, receipt_feedback=False)
        self.assertFalse(self.transport.help(event)["receipt_feedback"])
        self.transport.request.reset_mock()
        self.transport.accept("later", event)
        await self.transport.keep_typing("router", event, 60)
        await self.receipts()
        self.transport.request.assert_not_awaited()
        reopened = State(self.root)
        try:
            self.assertIs(reopened.setting(self.transport.receipt_scope(event)), False)
        finally:
            reopened.close()
        self.assertTrue(self.transport.receipt_enabled(self.event(conversation="456")))
        self.assertTrue(self.transport.receipt_enabled(self.event("slack")))
        await self.transport.keep_typing("worker", event, 60, receipt_feedback=True)
        self.transport.request.assert_awaited_once()

    async def test_feedback_failure_does_not_fail_message_or_leak_provider_error(self):
        self.transport.request.side_effect = RuntimeError("secret-provider-value")
        with self.assertLogs("hermes_runtime.channel_agent.transports") as captured:
            self.transport.accept("update", self.event())
            await self.receipts()
        self.assertEqual(self.state.queued()[0]["state"], "queued")
        self.assertFalse(self.transport.receipt_events)
        self.assertNotIn("secret-provider-value", str(captured.output))
        self.assertIn("channel_receipt_unavailable", str(captured.output))

    async def test_full_inbox_and_drain_never_start_feedback(self):
        self.transport._accept = lambda *a, **kw: (_ for _ in ()).throw(BufferError())
        with self.assertRaises(BufferError):
            self.transport.accept("update", self.event())
        self.assertFalse(self.transport.receipt_jobs)
        self.transport._accept = self.state.ingest
        self.transport.draining = True
        self.transport.accept("update", self.event())
        self.assertFalse(self.transport.receipt_jobs)

    async def test_telegram_unauthorized_updates_receive_no_feedback(self):
        self.transport.values["TELEGRAM_ALLOWED_USERS"] = "123"
        called = 0

        async def updates(provider, method, params):
            nonlocal called
            self.assertEqual(method, "getUpdates")
            called += 1
            if called > 1:
                raise asyncio.CancelledError()
            return [{"update_id": 1, "message": {
                "message_id": 1, "from": {"id": 456},
                "chat": {"id": 123, "type": "private"}, "text": "untrusted",
            }}]

        self.transport.request = updates
        with self.assertRaises(asyncio.CancelledError):
            await self.transport.telegram()
        self.assertFalse(self.transport.receipt_jobs)
        self.assertFalse(self.state.queued())
        self.transport.values.clear()

    async def test_router_cannot_change_saved_preferences(self):
        service = Service.__new__(Service)
        service.transports = self.transport
        with self.assertRaises(ValueError):
            await service.tool("router", self.event(), True, "channel_typing",
                               {"seconds": 0, "receipt_feedback": False})
        self.assertTrue(self.transport.receipt_enabled(self.event()))

    async def test_transient_preflight_failure_recovers_without_process_restart(self):
        self.transport.connected = {"telegram": False}
        ready, retry = asyncio.Event(), asyncio.Event()
        attempts = 0

        async def request(*args):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise TimeoutError()
            return {}

        async def telegram():
            self.transport.connected["telegram"] = True
            ready.set()
            await asyncio.Future()

        async def sleep(seconds):
            retry.set()

        self.transport.request = request
        self.transport.telegram = telegram
        with patch("hermes_runtime.channel_agent.transports.asyncio.sleep", sleep):
            await self.transport.start()
            await asyncio.wait_for(ready.wait(), 1)
        self.assertTrue(retry.is_set())
        self.assertTrue(self.transport.connected["telegram"])

    async def test_slack_activity_lease_clears_only_its_own_status(self):
        event = {"provider": "slack", "conversation": "C1", "message_id": "1.1"}
        await self.transport.keep_typing("task1", event, 60)
        result = await self.transport.keep_typing("task2", event, 60)
        self.assertEqual(result, {"already_working": True})
        self.assertEqual(self.transport.request.await_count, 1)
        await self.transport.stop_typing("task1")
        self.assertEqual(self.transport.request.await_args.args[2]["status"], "active")
        self.assertEqual(self.transport.request.await_args.args[2]["thread_ts"], "1.1")

    async def test_slack_lease_does_not_clear_status_transferred_to_worker(self):
        event = {"provider": "slack", "conversation": "C1", "message_id": "1.1"}
        await self.transport.keep_typing("router", event, 60)
        self.state.set("slack_status:" + json.dumps(["C1", "1.1"]), "worker")
        await self.transport.stop_typing("router")
        self.assertEqual(self.transport.request.await_count, 1)

    async def test_router_capability_cannot_send_messages_or_request_approval(self):
        with patch(
            "hermes_runtime.channel_agent.service.Transports",
            return_value=self.transport,
        ):
            service = Service(self.root, "codex")
        try:
            for name in ["channel_api", "request_approval"]:
                with self.assertRaises(ValueError):
                    await service.tool("router", {}, True, name, {})
        finally:
            service.state.close()

    async def test_worker_failure_is_visible_then_success_restores_health(self):
        with patch(
            "hermes_runtime.channel_agent.service.Transports",
            return_value=self.transport,
        ):
            service = Service(self.root, "codex")
        try:
            service.state.ingest(
                "event",
                {
                    "provider": "telegram",
                    "conversation": "1",
                    "message_id": "1",
                    "text": "test",
                },
            )
            task_id = service.state.route(
                "event",
                {"task_id": None, "title": "test", "clarification": None},
                "codex",
            )
            row = service.state.queued()[0]
            service.run_native = AsyncMock(side_effect=RuntimeError("secret-error"))
            await service.work(row, task_id)
            status = service.snapshot()
            self.assertEqual(status["status"], "running")
            self.assertEqual(status["error"], "native_turn_failed")
            service.run_native = AsyncMock(return_value=("native", ""))
            await service.work(row, task_id)
            self.assertEqual(service.snapshot()["status"], "running")
            self.assertIsNone(service.snapshot()["error"])
        finally:
            service.state.close()
