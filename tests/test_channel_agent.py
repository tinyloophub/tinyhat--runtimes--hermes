"""Durable routing, scoped output, concurrent turns and receiver handoff.

Usage: python -m unittest discover -s tests -p test_channel_agent.py -v
"""

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hermes_runtime.channel_agent import control
from hermes_runtime.channel_agent.native import Codex
from hermes_runtime.channel_agent.paths import prepare_socket_directory, socket_path
from hermes_runtime.channel_agent.service import Service
from hermes_runtime.channel_agent.state import State
from hermes_runtime.channel_agent.transports import Transports


class SocketPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_ipc_stays_off_persistent_mount_and_supports_long_state_paths(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / ("mounted-computer-state-" * 12)
            path = socket_path(state)
            self.assertNotIn(str(state), str(path))
            self.assertLess(len(str(path).encode()), 104)
            prepare_socket_directory(state)
            server = await asyncio.start_unix_server(lambda r, w: w.close(), path=path)
            try:
                _, writer = await asyncio.open_unix_connection(path)
                writer.close()
                await writer.wait_closed()
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            finally:
                server.close()
                await server.wait_closed()
                shutil.rmtree(path.parent)

    async def test_socket_directory_cannot_redirect_through_symlink(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / "state"
            parent = socket_path(state).parent
            parent.symlink_to(root)
            try:
                with self.assertRaises(RuntimeError):
                    prepare_socket_directory(state)
            finally:
                parent.unlink()


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = State(self.root)

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def test_committed_event_survives_restart_without_duplicate_or_cursor_regression(
        self,
    ):
        self.assertTrue(
            self.state.ingest("telegram:20", {"text": "first"}, cursor=("offset", 21))
        )
        self.state.close()
        self.state = State(self.root)
        self.assertFalse(
            self.state.ingest(
                "telegram:20", {"text": "duplicate"}, cursor=("offset", 21)
            )
        )
        self.assertEqual(json.loads(self.state.queued()[0]["payload"])["text"], "first")
        self.assertEqual(self.state.setting("offset"), 21)

    def test_route_is_saved_once_and_cannot_resume_another_framework(self):
        self.state.ingest("1", {"text": "build"})
        task = self.state.route(
            "1", {"task_id": None, "title": "Build", "clarification": None}, "codex"
        )
        self.assertEqual(
            self.state.route("1", {"task_id": None, "title": "Different"}, "codex"),
            task,
        )
        self.state.ingest("2", {"text": "follow up"})
        with self.assertRaises(ValueError):
            self.state.route("2", {"task_id": task}, "claude_code")

    def test_full_inbox_signals_capacity_without_acknowledging_provider_cursor(self):
        for index in range(500):
            self.state.ingest(str(index), {"text": "pending"})
        with self.assertRaises(BufferError):
            self.state.ingest("new", {"text": "later"}, cursor=("offset", 501))
        self.assertIsNone(self.state.setting("offset"))
        self.state.event_state("0", "done")
        self.assertTrue(
            self.state.ingest("new", {"text": "later"}, cursor=("offset", 501))
        )
        self.assertEqual(self.state.setting("offset"), 501)

    def test_crash_does_not_reexecute_dispatched_work_or_repeat_uncertain_output(self):
        self.state.ingest("1", {"text": "send"})
        task = self.state.route("1", {"title": "Send"}, "codex")
        self.state.event_state("1", "running")
        self.state.update_task(task, status="running", native_id="native-session")
        request = {
            "provider": "telegram",
            "method": "sendMessage",
            "params": {"text": "hello"},
        }
        self.state.claim_action(task, "output1", request)
        self.state.recover()
        self.assertFalse(self.state.queued())
        self.assertEqual(self.state.task(task)["native_id"], "native-session")
        self.assertEqual(self.state.task(task)["status"], "interrupted")
        self.assertEqual(
            self.state.claim_action(task, "output1", request)["state"], "uncertain"
        )

    def test_context_includes_reply_mapping_and_bounded_recent_text(self):
        self.state.ingest("1", {"text": "a" * 10000, "raw": {"large": "private"}})
        task = self.state.route("1", {"title": "Research"}, "codex")
        request = {
            "provider": "slack",
            "params": {"channel": "C1", "text": "Question?"},
        }
        self.state.claim_action(task, "question", request)
        self.state.finish_action(
            task,
            "question",
            {"provider": "slack", "message_id": "1.2", "conversation": "C1"},
        )
        ctx = self.state.context({"text": "Yes", "thread_id": "1.2"})
        self.assertEqual(ctx["recent_replies"][0]["task_id"], task)
        self.assertEqual(len(ctx["recent_messages"][0]["text"]), 2000)
        self.assertNotIn("raw", ctx["recent_messages"][0])
        self.assertFalse(self.state.owns_message(task, "slack", "1.2", "OTHER"))


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = State(self.root)
        methods = self.root / "capabilities/channels/methods.json"
        methods.parent.mkdir(parents=True)
        methods.write_text(
            json.dumps(
                {
                    "telegram": {
                        "sendMessage": {"target": "chat_id"},
                        "editMessageText": {
                            "target": "chat_id",
                            "message": "message_id",
                        },
                    }
                }
            )
        )
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
        self.transport.request = AsyncMock(return_value={"message_id": 10})
        self.event = {"provider": "telegram", "conversation": "123", "message_id": "1"}

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    async def test_multiple_replies_and_edits_are_agent_actions_and_retries_do_not_resend(
        self,
    ):
        action = {
            "method": "sendMessage",
            "params": {"text": "Starting"},
            "action_id": "1:start",
        }
        await self.transport.action("task1", self.event, action)
        await self.transport.action("task1", self.event, action)
        self.assertEqual(self.transport.request.await_count, 1)
        await self.transport.action(
            "task1",
            self.event,
            {
                "method": "editMessageText",
                "params": {"text": "Finished", "message_id": 10},
                "action_id": "1:finish",
            },
        )
        self.assertEqual(self.transport.request.await_args.args[2]["chat_id"], "123")
        with self.assertRaises(ValueError):
            await self.transport.action(
                "other-task",
                self.event,
                {
                    "method": "editMessageText",
                    "params": {"text": "stolen", "message_id": 10},
                    "action_id": "edit",
                },
            )

    async def test_destination_override_and_unreviewed_methods_fail_before_network(
        self,
    ):
        for action in [
            {
                "method": "sendMessage",
                "params": {"chat_id": "OTHER", "text": "hello"},
                "action_id": "x",
            },
            {
                "method": "setWebhook",
                "params": {"url": "https://example.org"},
                "action_id": "y",
            },
            {
                "method": "sendMessage",
                "params": {"allow_paid_broadcast": True},
                "action_id": "z",
            },
        ]:
            with self.assertRaises(ValueError):
                await self.transport.action("task1", self.event, action)
        self.transport.request.assert_not_awaited()

    async def test_transport_failure_stays_uncertain_instead_of_automatic_duplicate(
        self,
    ):
        self.transport.request.side_effect = TimeoutError()
        action = {
            "method": "sendMessage",
            "params": {"text": "hello"},
            "action_id": "uncertain",
        }
        with self.assertRaises(TimeoutError):
            await self.transport.action("task1", self.event, action)
        retry = await self.transport.action("task1", self.event, action)
        self.assertEqual(retry["state"], "uncertain")
        self.transport.request.assert_awaited_once()

    async def test_drain_closes_intake_but_leaves_output_available_until_work_finishes(
        self,
    ):
        self.transport.session = SimpleNamespace(close=AsyncMock())
        self.transport.listeners = [asyncio.create_task(asyncio.sleep(100))]
        await self.transport.stop_intake()
        self.transport.session.close.assert_not_awaited()
        await self.transport.action(
            "task",
            self.event,
            {
                "method": "sendMessage",
                "params": {"text": "Completed"},
                "action_id": "final",
            },
        )
        await self.transport.close()
        self.transport.session.close.assert_awaited_once()

    async def test_typing_is_opt_in_bounded_and_cleaned_up(self):
        self.transport.request.assert_not_awaited()
        await self.transport.keep_typing("task1", self.event, 10)
        self.assertEqual(
            self.transport.request.await_args.args,
            ("telegram", "sendChatAction", {"chat_id": "123", "action": "typing"}),
        )
        lease = self.transport.typing["task1"]
        await self.transport.keep_typing("task1", self.event, 0)
        self.assertTrue(lease.done())
        self.assertFalse(self.transport.typing)
        for seconds in (-1, 121, True):
            with self.assertRaises(ValueError):
                await self.transport.keep_typing("task1", self.event, seconds)

    async def test_slack_stream_targets_current_owner_and_root_thread(self):
        self.transport.methods["slack"] = {
            "chat.startStream": {"target": "channel", "thread_status": True},
            "chat.appendStream": {"target": "channel", "message": "ts"},
            "chat.stopStream": {"target": "channel", "message": "ts"},
        }
        event = {
            "provider": "slack",
            "conversation": "C123",
            "sender": "U123",
            "team_id": "T123",
            "message_id": "10.001",
        }
        self.transport.request.return_value = {"ts": "20.001"}
        await self.transport.action(
            "task1",
            event,
            {
                "method": "chat.startStream",
                "params": {"markdown_text": "Hello"},
                "action_id": "start",
            },
        )
        sent = self.transport.request.await_args.args[2]
        self.assertEqual(sent["thread_ts"], "10.001")
        self.assertEqual(sent["recipient_user_id"], "U123")
        self.assertEqual(sent["recipient_team_id"], "T123")
        for method in ("chat.appendStream", "chat.stopStream"):
            await self.transport.action(
                "task1",
                event,
                {"method": method, "params": {"ts": "20.001"}, "action_id": method},
            )
            with self.assertRaises(ValueError):
                await self.transport.action(
                    "task2",
                    event,
                    {"method": method, "params": {"ts": "20.001"}, "action_id": method},
                )
        with self.assertRaises(ValueError):
            await self.transport.action(
                "task1",
                event,
                {
                    "method": "chat.startStream",
                    "params": {"recipient_user_id": "OTHER"},
                    "action_id": "foreign",
                },
            )


class HandoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_inventory_reports_only_a_valid_configured_hermes_model(self):
        ctx = SimpleNamespace()
        with (
            patch.object(control, "selected", return_value="hermes"),
            patch.object(
                control,
                "probe",
                new=AsyncMock(return_value={"installed": True, "authenticated": True}),
            ),
            patch.object(
                control, "find_hermes_binary", return_value=Path("/bin/hermes")
            ),
            patch.object(control, "run_process", new_callable=AsyncMock) as run,
        ):
            for output, expected in [
                ("openai/gpt-5.4\n", "openai/gpt-5.4"),
                ("notice\nopenai/gpt-5.4\n", None),
            ]:
                with self.subTest(output=output):
                    run.return_value = {"ok": True, "stdout": output}
                    await control.inventory(ctx)
                    self.assertEqual(ctx.hermes_channel_model, expected)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ctx = SimpleNamespace(state_dir=Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    async def test_new_assignment_cannot_reuse_previous_framework_or_history(self):
        await control.bind(self.ctx, "owner-one")
        old_path = control.directory(self.ctx)
        control.save(
            self.ctx, {"active": "codex", "desired": "codex", "status": "running"}
        )
        with patch.object(
            control, "rpc", AsyncMock(return_value={"stopped": True})
        ) as rpc:
            await control.bind(self.ctx, "owner-two")
        self.assertEqual(rpc.await_args.args[1], "halt")
        self.assertNotEqual(control.directory(self.ctx), old_path)
        self.assertEqual(control.selected(self.ctx), "hermes")
        self.assertNotIn("tasks", control.snapshot(self.ctx))

    async def test_missing_login_does_not_stop_or_change_current_receiver(self):
        with (
            patch.object(
                control, "preflight", AsyncMock(side_effect=RuntimeError("Sign in"))
            ),
            patch.object(control, "rpc", AsyncMock()) as rpc,
            self.assertRaises(RuntimeError),
        ):
            await control.request_switch(self.ctx, "codex")
        rpc.assert_not_awaited()
        self.assertEqual(control.selected(self.ctx), "hermes")

    async def test_switch_waits_for_inflight_hermes_startup(self):
        self.ctx.gateway_reconcile_task = asyncio.get_running_loop().create_future()
        with patch.object(control, "preflight", AsyncMock()) as preflight:
            with self.assertRaisesRegex(RuntimeError, "startup"):
                await control.request_switch(self.ctx, "codex")
        preflight.assert_not_awaited()
        self.assertEqual(control.mode(self.ctx)["desired"], "hermes")
        self.ctx.gateway_reconcile_task.cancel()

    async def test_failed_switch_does_not_restart_legacy_receiver_in_parallel(self):
        from hermes_runtime.main import _maybe_start_gateway_reconcile
        from hermes_runtime.commands import run_command

        control.save(
            self.ctx, {"active": "hermes", "desired": "codex", "status": "failed"}
        )
        self.ctx.gateway_reconcile_task = None
        self.ctx.gateway_reconciled = False
        self.ctx.platform_state = "active"
        with patch("hermes_runtime.main._telegram_env_configured", return_value=True):
            _maybe_start_gateway_reconcile(self.ctx)
        self.assertIsNone(self.ctx.gateway_reconcile_task)
        with patch("hermes_runtime.commands.heal_hermes.run", AsyncMock()) as heal:
            result = await run_command(self.ctx, {"kind": "heal_hermes"})
        heal.assert_not_awaited()
        self.assertFalse(result["changed"])

    async def test_native_switch_waits_for_active_work_and_does_not_stop_it(self):
        control.save(
            self.ctx,
            {"active": "codex", "desired": "claude_code", "status": "switching"},
        )
        with (
            patch.object(control, "preflight", AsyncMock()),
            patch.object(control, "start_native", AsyncMock()),
            patch.object(
                control,
                "rpc",
                AsyncMock(return_value={"queued": 0, "tasks": [{"status": "running"}]}),
            ) as rpc,
        ):
            await control.reconcile(self.ctx)
        self.assertEqual([call.args[1] for call in rpc.await_args_list], ["drain"])
        self.assertEqual(control.selected(self.ctx), "codex")

    async def test_runtime_update_drains_existing_service_before_replacing_it(self):
        running = {
            "active": "codex",
            "revision": "old",
            "queued": 0,
            "tasks": [{"status": "running"}],
            "channels": {"telegram": True},
        }
        with (
            patch.object(control, "rpc", AsyncMock(return_value=running)) as rpc,
            patch.object(
                control.asyncio, "create_subprocess_exec", AsyncMock()
            ) as spawn,
        ):
            result = await control.start_native(self.ctx, "codex")
        self.assertEqual(
            [call.args[1] for call in rpc.await_args_list], ["status", "drain"]
        )
        spawn.assert_not_awaited()
        self.assertEqual(result["revision"], "old")

    async def test_failed_hermes_activation_is_retried(self):
        control.save(
            self.ctx, {"active": "hermes", "desired": "hermes", "status": "failed"}
        )
        with (
            patch.object(control, "preflight", AsyncMock()),
            patch(
                "hermes_runtime.commands.start_hermes.run",
                AsyncMock(return_value={"healthy": True}),
            ) as start,
        ):
            await control.reconcile(self.ctx)
        start.assert_awaited_once()
        self.assertEqual(control.mode(self.ctx)["status"], "running")

    async def test_unconfirmed_hermes_stop_never_starts_the_other_receiver(self):
        control.save(
            self.ctx, {"active": "hermes", "desired": "codex", "status": "switching"}
        )
        with (
            patch.object(control, "preflight", AsyncMock()),
            patch.object(control, "find_hermes_binary", return_value=Path("/hermes")),
            patch.object(
                control,
                "run_process",
                AsyncMock(return_value={"ok": True, "stdout": "Running"}),
            ),
            patch.object(control, "start_native", AsyncMock()) as start,
            self.assertRaises(RuntimeError),
        ):
            await control.reconcile(self.ctx)
        start.assert_not_awaited()
        self.assertEqual(control.selected(self.ctx), "hermes")

    async def test_activation_failure_never_rolls_back_to_a_competing_receiver(self):
        control.save(
            self.ctx, {"active": "hermes", "desired": "codex", "status": "switching"}
        )

        async def start(ctx, framework):
            self.assertEqual(
                control.selected(ctx), framework
            )  # durable ownership precedes intake
            raise RuntimeError("Channel unavailable")

        with (
            patch.object(control, "preflight", AsyncMock()),
            patch.object(control, "find_hermes_binary", return_value=Path("/hermes")),
            patch.object(
                control,
                "run_process",
                AsyncMock(
                    return_value={
                        "ok": True,
                        "stdout": "✗ User gateway service is stopped\n  Run: hermes gateway start\n",
                    }
                ),
            ),
            patch.object(control, "start_native", side_effect=start),
            patch("hermes_runtime.commands.start_hermes.run", AsyncMock()) as old,
            self.assertRaises(RuntimeError),
        ):
            await control.reconcile(self.ctx)
        old.assert_not_awaited()
        self.assertEqual(control.selected(self.ctx), "codex")


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_jobs_and_same_task_serialization_without_automatic_reply(
        self,
    ):
        with tempfile.TemporaryDirectory(dir="/tmp") as folder:
            root = Path(folder)
            (root / "control").write_text("local-control")
            transport = SimpleNamespace(
                connected={"telegram": True},
                values={},
                start=AsyncMock(),
                close=AsyncMock(),
                action=AsyncMock(),
                stop_intake=AsyncMock(),
                stop_typing=AsyncMock(),
            )
            with patch(
                "hermes_runtime.channel_agent.service.Transports",
                return_value=transport,
            ):
                service = Service(root, "codex")
            first = asyncio.Event()
            second = asyncio.Event()
            running, completed = [], []

            async def native(prompt, *, task=None, event=None, router=False):
                if router:
                    incoming = json.loads(prompt)["incoming"]
                    return "router", json.dumps(
                        {
                            "task_id": incoming.get("task_id"),
                            "title": incoming["text"],
                            "clarification": None,
                        }
                    )
                running.append(event["message_id"])
                if event["message_id"] == "1":
                    await first.wait()
                elif event["message_id"] == "2":
                    await second.wait()
                completed.append(event["message_id"])
                return (
                    "native-" + task["id"],
                    "Terminal output must not become a channel reply",
                )

            service.run_native = native
            for key in ("1", "2"):
                service.state.ingest(
                    key,
                    {
                        "provider": "telegram",
                        "conversation": "1",
                        "message_id": key,
                        "text": "job " + key,
                    },
                )
            runner = asyncio.create_task(service.run())

            async def wait_for(predicate):
                async def poll():
                    while not predicate():
                        await asyncio.sleep(0.02)

                await asyncio.wait_for(poll(), 8)

            try:
                await wait_for(lambda: len(running) == 2)
                task_id = next(
                    row["task_id"]
                    for row in service.state.db.execute(
                        "SELECT task_id FROM events WHERE id='1'"
                    )
                )
                service.state.ingest(
                    "3",
                    {
                        "provider": "telegram",
                        "conversation": "1",
                        "message_id": "3",
                        "text": "follow up",
                        "task_id": task_id,
                    },
                )
                second.set()
                await wait_for(lambda: "2" in completed)
                self.assertNotIn("3", running)
                first.set()
                await wait_for(lambda: "3" in completed)
                self.assertEqual(completed, ["2", "1", "3"])
                self.assertEqual(len(service.state.tasks()), 2)
                transport.action.assert_not_awaited()
            finally:
                service.shutdown.set()
                await runner


class NativeApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_permission_request_is_denied_without_truncated_approval(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "control").write_text("test-control")
            with patch("hermes_runtime.channel_agent.service.Transports"):
                service = Service(root, "codex")
            try:
                self.assertFalse(
                    await service.approval("task", {"command": "x" * 2000})
                )
                self.assertEqual(
                    service.state.db.execute(
                        "SELECT count(*) FROM approvals"
                    ).fetchone()[0],
                    0,
                )
            finally:
                service.state.close()

    async def test_only_private_channel_tool_approval_is_automatic(self):
        approval = AsyncMock(return_value=False)
        agent = Codex(
            cwd=Path("/tmp"), approve=approval, mcp={"enabled_tools": ["channel_api"]}
        )
        agent.send = AsyncMock()
        for server, expected in [
            ("tinyhat_channel", "accept"),
            ("another_server", "decline"),
        ]:
            await agent.answer(
                {
                    "id": 1,
                    "method": "mcpServer/elicitation/request",
                    "params": {
                        "serverName": server,
                        "mode": "form",
                        "_meta": {"codex_approval_kind": "mcp_tool_call"},
                    },
                }
            )
            self.assertEqual(
                agent.send.await_args.args[0]["result"]["action"], expected
            )
        approval.assert_not_awaited()
        await agent.answer(
            {
                "id": 2,
                "method": "item/commandExecution/requestApproval",
                "params": {"command": "touch file"},
            }
        )
        approval.assert_awaited_once()
        self.assertEqual(agent.send.await_args.args[0]["result"]["decision"], "decline")

    async def test_router_cannot_approve_channel_tools(self):
        agent = Codex(cwd=Path("/tmp"), approve=AsyncMock(return_value=False))
        agent.send = AsyncMock()
        await agent.answer(
            {
                "id": 1,
                "method": "mcpServer/elicitation/request",
                "params": {
                    "serverName": "tinyhat_channel",
                    "mode": "form",
                    "_meta": {"codex_approval_kind": "mcp_tool_call"},
                },
            }
        )
        self.assertEqual(agent.send.await_args.args[0]["result"]["action"], "decline")


class ConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_busy_native_task_blocks_credential_file_changes(self):
        from hermes_runtime.commands import apply_config

        ctx = SimpleNamespace(
            platform=SimpleNamespace(
                get_json=AsyncMock(return_value={"secrets": {"EXAMPLE_KEY": "new"}})
            )
        )
        with (
            patch.object(control, "selected", return_value="codex"),
            patch(
                "hermes_runtime.channel_agent.configure.stop_for_configuration",
                AsyncMock(side_effect=RuntimeError("busy")),
            ),
            patch.object(apply_config, "_write_runtime_secret_env_file") as write,
        ):
            with self.assertRaisesRegex(RuntimeError, "busy"):
                await apply_config.run(ctx, {"spec": {}})
            write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
