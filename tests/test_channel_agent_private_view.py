"""Private session bridge and browser-only sign-in.

Usage: python -m unittest discover -s tests -p test_channel_agent_private_view.py -v
"""

import asyncio
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hermes_runtime.channel_agent import bridge, control, signin
from hermes_runtime.channel_agent.paths import prepare_socket_directory, socket_path
from hermes_runtime.channel_agent.state import State
from hermes_runtime.channel_agent.service import Service
from hermes_runtime.channel_agent.transports import Transports


class PrivateViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ctx = SimpleNamespace(state_dir=Path(self.temp.name))
        await control.bind(self.ctx, "owner-one")

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_heartbeat_omits_private_data_but_bridge_reads_it_only_for_owner(
        self,
    ):
        control.save(
            self.ctx, {"active": "codex", "desired": "codex", "status": "running"}
        )
        private = {
            "active": "codex",
            "updated_at": time.time(),
            "channels": {"telegram": True},
            "tasks": [
                {
                    "id": "1",
                    "title": "Secret task",
                    "summary": "Secret summary",
                    "native_id": "native-secret",
                }
            ],
            "approvals": [{"id": "2", "request": "Secret approval"}],
        }
        (control.directory(self.ctx) / "status.json").write_text(json.dumps(private))
        public = json.dumps(control.snapshot(self.ctx))
        self.assertNotIn("Secret", public)
        self.assertNotIn("native-secret", public)
        view = await bridge.request(self.ctx, "owner-one")
        self.assertEqual(view["tasks"][0]["title"], "Secret task")
        self.assertNotIn("native_id", view["tasks"][0])
        self.assertNotIn("summary", view["tasks"][0])
        with self.assertRaises(ValueError):
            await bridge.request(self.ctx, "owner-two")
        control.save(
            self.ctx, {"active": "hermes", "desired": "hermes", "status": "running"}
        )
        view = await bridge.request(self.ctx, "owner-one")
        self.assertTrue(view["tasks"])
        self.assertEqual(view["approvals"], [])

    async def test_control_rpc_accepts_large_unicode_session_list(self):
        path = control.directory(self.ctx)
        prepare_socket_directory(path)
        (path / "control").write_text("local-control")
        expected = {"tasks": [{"title": "漢字" * 2000} for _ in range(20)]}

        async def serve(reader, writer):
            await reader.readline()
            writer.write(json.dumps({"result": expected}).encode() + b"\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_unix_server(serve, path=socket_path(path))
        try:
            self.assertEqual(await control.rpc(self.ctx, "status"), expected)
        finally:
            server.close()
            await server.wait_closed()
            shutil.rmtree(socket_path(path).parent)

    async def test_signin_requires_installation_and_does_not_expose_cli_output(self):
        with (
            patch.object(
                signin,
                "probe",
                AsyncMock(return_value={"installed": False, "authenticated": False}),
            ),
            patch.object(signin.subprocess, "Popen") as launch,
        ):
            with self.assertRaises(RuntimeError):
                await signin.start(self.ctx, "codex")
            launch.assert_not_called()
        with (
            patch.object(
                signin,
                "probe",
                AsyncMock(return_value={"installed": True, "authenticated": False}),
            ),
            patch.object(signin.subprocess, "Popen") as launch,
        ):
            result = await signin.start(self.ctx, "codex")
            args = launch.call_args.args[0]
            self.assertIn("hermes_runtime.channel_agent.signin", args)
            self.assertNotIn("terminal", " ".join(args))
            self.assertEqual(
                launch.call_args.kwargs["stdout"], signin.subprocess.DEVNULL
            )
            self.assertNotIn("url", result)
            self.assertEqual(
                launch.call_args.kwargs["env"]["PYTHONPATH"],
                str(Path(signin.__file__).resolve().parents[2]),
            )

    async def test_sessions_command_sends_one_button_without_creating_native_task(self):
        state = State(control.directory(self.ctx))
        service = Service.__new__(Service)
        service.state = state
        service.transports = Transports.__new__(Transports)
        service.transports.state = state
        service.transports.methods = {"telegram": {"sendMessage": {"target": "chat_id"}}}
        service.transports.request = AsyncMock(return_value={"message_id": 91})
        service.run_native = AsyncMock()
        event = {"provider": "telegram", "text": "/sessions@my_bot", "conversation": "7"}
        state.ingest("command-update", event)
        button = {"text": "Sessions", "url": "https://computer.example/sessions"}
        try:
            with patch(
                "hermes_runtime.channel_agent.transports.plugin_module",
                return_value=SimpleNamespace(button=lambda: button),
            ):
                row = state.queued()[0]
                self.assertIsNone(await service.route(row))
                # Retrying an already processed update cannot send twice.
                self.assertIsNone(await service.route(row))
            service.run_native.assert_not_called()
            service.transports.request.assert_awaited_once()
            sent = service.transports.request.call_args.args[2]
            self.assertEqual(sent["chat_id"], "7")
            self.assertEqual(sent["reply_markup"]["inline_keyboard"], [[button]])
            self.assertEqual(state.tasks(), [])
            self.assertEqual(state.queued(), [])
        finally:
            state.close()

    async def test_expired_flow_is_failed_not_connected(self):
        path = control.directory(self.ctx) / "signin.json"
        path.write_text(
            json.dumps({"framework": "codex", "status": "waiting", "updated_at": 1})
        )
        self.assertEqual(
            signin.status(self.ctx), {"framework": "codex", "status": "failed"}
        )

    async def test_explicit_suspend_is_not_undone_by_assigned_heartbeat(self):
        self.ctx.platform_state = "active"
        await control.suspend(self.ctx)
        with patch.object(control, "request_switch", AsyncMock()) as switch:
            await control.reconcile(self.ctx)
            switch.assert_not_called()
        await control.suspend(self.ctx, resume_on_assignment=True)
        with patch.object(control, "request_switch", AsyncMock()) as switch:
            await control.reconcile(self.ctx)
            switch.assert_awaited_once_with(self.ctx, "hermes")

    async def test_explicit_hermes_start_clears_suspended_status_after_health_check(self):
        from hermes_runtime import commands

        await control.suspend(self.ctx)
        run = AsyncMock(return_value={"healthy": True})
        with patch.object(commands, "import_module", return_value=SimpleNamespace(run=run)):
            await commands.run_command(self.ctx, {"kind": "start_hermes"})
        self.assertEqual(control.mode(self.ctx)["status"], "running")


class BrowserBoundaryTests(unittest.TestCase):
    def test_only_official_provider_urls_open_without_shell(self):
        with (
            patch.object(signin.shutil, "which", return_value="/usr/bin/chrome"),
            patch.object(signin.subprocess, "Popen") as launch,
        ):
            for url in [
                "https://evil.test/login",
                "javascript:alert(1)",
                "https://auth.openai.com@evil.test/",
                "https://user:pw@claude.ai/",
            ]:
                with self.assertRaises(ValueError):
                    signin.open_browser(url)
            launch.assert_not_called()
            signin.open_browser("https://auth.openai.com/oauth/authorize?state=private")
            self.assertEqual(launch.call_args.args[0][0], "/usr/bin/chrome")
            self.assertNotIn("shell", launch.call_args.kwargs)
            self.assertEqual(launch.call_args.kwargs["env"]["DISPLAY"], ":1")

    def test_poison_event_retry_does_not_block_next_event_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            state = State(Path(directory))
            state.ingest("bad", {"text": "bad"})
            state.ingest("good", {"text": "good"})
            state.route_failed("bad")
            self.assertEqual(
                [row["id"] for row in state.queued(ready_only=True)], ["good"]
            )
            state.close()
            state = State(Path(directory))
            state.route_failed("bad")
            state.route_failed("bad")
            self.assertEqual([row["id"] for row in state.queued()], ["good"])
            state.close()
