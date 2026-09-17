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
    def test_workspace_skill_tracks_plugin_updates_and_preserves_owner_override(self):
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
                self.assertEqual(
                    service.codex_skill(root / "work", "tinyhat-respond").read_text(),
                    "owner override",
                )

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
        (self.root / "capabilities/channels/methods.json").write_text("{}")
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
