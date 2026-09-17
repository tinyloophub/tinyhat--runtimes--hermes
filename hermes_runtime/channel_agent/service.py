"""One local receiver and bounded native workers, independent of heartbeats."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import secrets
import signal
import sys
import time
import uuid
from pathlib import Path

from hermes_runtime.agent_systems import _write_atomic
from hermes_runtime.channel_agent import native
from hermes_runtime.channel_agent.message import format_message
from hermes_runtime.channel_agent.paths import prepare_socket_directory, socket_path
from hermes_runtime.channel_agent.revision import installed_revision
from hermes_runtime.channel_agent.state import State
from hermes_runtime.channel_agent.transports import Transports
from hermes_runtime.plugin_manager import DEFAULT_TINYHAT_PLUGIN_NAME, plugin_dir

# The service is launched with -m, where __name__ is __main__. Keep diagnostics
# under the package logger that owns the private rotating file handler.
log = logging.getLogger("hermes_runtime.channel_agent.service")


class Service:
    def __init__(self, directory, framework):
        self.directory, self.framework = Path(directory), framework
        self.state = State(self.directory)
        self.workers, self.capabilities = {}, {}
        self.draining = False
        self.shutdown = asyncio.Event()
        self.socket_path = socket_path(self.directory)
        self.control = (self.directory / "control").read_text().strip()
        self.transports = Transports(self.state, self.state.ingest, self.stop_task)
        self.error = None
        self.revision = installed_revision()
        self.model = None

    def skill(self, name):
        return (
            plugin_dir(DEFAULT_TINYHAT_PLUGIN_NAME) / "skills" / name / "SKILL.md"
        ).read_text()

    def codex_skill(self, root, name):
        # Explicit skill input only resolves skills in Codex's catalog. Use its
        # documented workspace discovery directory and symlink support so the
        # plugin stays the canonical source and updates apply to existing tasks.
        link = root / ".agents" / "skills" / name
        source = plugin_dir(DEFAULT_TINYHAT_PLUGIN_NAME) / "skills" / name
        link.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not link.exists() and not link.is_symlink():
            link.symlink_to(source, target_is_directory=True)
        if not link.is_symlink() or link.resolve() != source.resolve():
            # Never promote workspace content to the explicit response policy.
            # Preserve the file; developer instructions still carry the plugin.
            log.warning("codex_skill_override_ignored")
            return None
        return (source / "SKILL.md").resolve()

    def snapshot(self):
        tasks = [
            {key: value for key, value in task.items() if key != "summary"}
            for task in self.state.tasks()
        ]
        approvals = [
            dict(row)
            for row in self.state.db.execute(
                "SELECT id,task_id,request,created FROM approvals WHERE decision IS NULL ORDER BY created"
            )
        ]
        for approval in approvals:
            # The owner sees the proposed action, but channel tokens never
            # travel back through the on-demand session view.
            text = approval["request"]
            for name, value in self.transports.values.items():
                if len(value) >= 8 and re.search(
                    r"TOKEN|SECRET|PASSWORD|API_KEY", name
                ):
                    text = text.replace(value, "[redacted]")
            approval["request"] = re.sub(
                r"(?:xox[baprs]-|xapp-|sk-)[A-Za-z0-9_-]+", "[redacted]", text
            )
        latest = self.state.db.execute(
            "SELECT status FROM tasks WHERE framework=? AND status IN ('failed','completed') "
            "ORDER BY updated DESC LIMIT 1",
            (self.framework,),
        ).fetchone()
        failed = latest is not None and latest[0] == "failed"
        error = self.error or ("native_turn_failed" if failed else None)
        return {
            "schema": "tinyhat.channel-agent.v1",
            "active": self.framework,
            "model": self.model,
            # Liveness is independent of a failed task: a live receiver must
            # keep exposing approvals and accepting the next owner message.
            "status": "draining" if self.draining else "running",
            "updated_at": time.time(),
            "pid": os.getpid(),
            "revision": self.revision,
            "channels": self.transports.connected,
            "error": error,
            "queued": len(self.state.queued()),
            "tasks": tasks,
            "approvals": approvals,
        }

    def publish(self):
        _write_atomic(
            self.directory / "status.json", json.dumps(self.snapshot()), 0o600
        )

    def report_model(self, model):
        self.model = native.model_name(model)
        self.publish()

    async def discover_model(self):
        if self.framework != "codex":
            return

        # An ephemeral thread resolves the official CLI's defaults without
        # generating a response or persisting a conversation.
        async def deny(_request):
            return False

        root = self.directory / "workspaces" / "router"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        agent = native.Codex(cwd=root, approve=deny)
        try:
            await agent.start()
            resolved = await agent.request(
                "thread/start",
                {
                    "cwd": str(root),
                    "ephemeral": True,
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                },
            )
            self.report_model(resolved.get("model"))
        finally:
            await agent.close()

    async def publish_status(self):
        while not self.shutdown.is_set():
            self.transports.ensure_listeners()
            self.publish()
            await asyncio.sleep(3)

    async def approval(self, task_id, request):
        encoded = json.dumps(request)
        # Never ask the owner to approve a truncated command. Keep the full
        # proposal within the bounded live view; larger requests are denied and
        # can be handled through the native app on the Computer instead.
        if (
            len(encoded) > 1800
            or self.state.db.execute(
                "SELECT count(*) FROM approvals WHERE decision IS NULL"
            ).fetchone()[0]
            >= 4
        ):
            return False
        approval_id = str(uuid.uuid4())
        with self.state.db:
            self.state.db.execute(
                "INSERT INTO approvals VALUES (?,?,?,NULL,?)",
                (approval_id, task_id, encoded, time.time()),
            )
        self.state.update_task(task_id, status="waiting")
        self.publish()
        try:
            while not self.shutdown.is_set():
                row = self.state.db.execute(
                    "SELECT decision FROM approvals WHERE id=?", (approval_id,)
                ).fetchone()
                if row[0]:
                    self.state.update_task(task_id, status="running")
                    return row[0] == "allow"
                await asyncio.sleep(1)
            return False
        finally:
            with self.state.db:
                self.state.db.execute(
                    "UPDATE approvals SET decision=COALESCE(decision,'deny') WHERE id=?",
                    (approval_id,),
                )

    async def tool(self, task_id, event, router, name, arguments):
        if router and name not in {"channel_api_help", "channel_typing"}:
            raise ValueError("Routing permits only temporary activity feedback.")
        if name == "channel_api_help":
            return self.transports.help(event)
        if name == "channel_typing":
            if router and "receipt_feedback" in arguments:
                raise ValueError("Only the worker may save response preferences.")
            result = await self.transports.keep_typing(
                task_id, event, arguments.get("seconds"),
                **(
                    {"receipt_feedback": arguments["receipt_feedback"]}
                    if "receipt_feedback" in arguments else {}
                ),
            )
            if result.get("typing_for_seconds"):
                log.info(
                    "router_activity_started" if router else "worker_activity_started"
                )
            return result
        if name == "channel_api":
            await self.transports.stop_receipt(event)
            return await self.transports.action(task_id, event, arguments)
        if name == "request_approval":
            allowed = await self.approval(task_id, arguments)
            return (
                {"behavior": "allow", "updatedInput": arguments.get("input", {})}
                if allowed
                else {
                    "behavior": "deny",
                    "message": "The owner did not approve this action.",
                }
            )
        raise ValueError("Unknown tool.")

    async def connection(self, reader, writer):
        try:
            raw = await asyncio.wait_for(reader.readline(), 10)
            request = json.loads(raw)
            if secrets.compare_digest(str(request.get("control", "")), self.control):
                action = request.get("action")
                if action == "status":
                    result = self.snapshot()
                elif action == "drain":
                    # Stop intake first; finish every already-committed update.
                    self.draining = True
                    await self.transports.stop_intake()
                    result = self.snapshot()
                elif action == "resume":
                    if self.draining:
                        self.draining = False
                        await self.transports.start()
                    result = self.snapshot()
                elif action == "stop":
                    if self.workers or self.state.queued():
                        raise ValueError("Work is still pending.")
                    self.shutdown.set()
                    result = {"stopped": True}
                elif action == "halt":
                    # Assignment revocation is stronger than a normal switch.
                    self.shutdown.set()
                    result = {"stopped": True}
                elif action == "approve":
                    self.state.approve(request["approval_id"], request["decision"])
                    result = {"recorded": True}
                else:
                    raise ValueError("Unknown control action.")
            else:
                capability = self.capabilities.get(str(request.get("capability", "")))
                if not capability:
                    raise ValueError("Task capability expired.")
                result = await self.tool(
                    *capability, request["name"], request.get("arguments", {})
                )
            response = {"result": result}
        except Exception:
            response = {"error": "Channel operation unavailable."}
        writer.write((json.dumps(response) + "\n").encode())
        with contextlib.suppress(ConnectionError):
            await writer.drain()
        writer.close()

    async def run_native(self, prompt, *, task=None, event=None, router=False):
        name = "tinyhat-route-message" if router else "tinyhat-respond"
        instruction = self.skill(name)
        root = self.directory / "workspaces" / (task["id"] if task else "router")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        callback = None
        capability = secrets.token_urlsafe(32)
        tool_id = task["id"] if task else "routing:" + secrets.token_hex(12)
        if event:
            self.capabilities[capability] = (tool_id, event, router)
        if task:

            def callback(sid):
                self.state.update_task(task["id"], status="running", native_id=sid)

        try:
            if self.framework == "claude_code":
                return await native.claude_turn(
                    prompt,
                    native_id=task["native_id"] if task else None,
                    instructions=instruction,
                    cwd=root,
                    socket_path=self.socket_path,
                    capability=capability,
                    router=router,
                    on_session=callback,
                    on_model=self.report_model,
                )
            mcp = (
                {
                    "command": sys.executable,
                    "args": ["-m", "hermes_runtime.channel_agent.mcp"],
                    "env": {
                        "TINYHAT_CHANNEL_SOCKET": str(self.socket_path),
                        "TINYHAT_CHANNEL_CAPABILITY": capability,
                        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
                        "TINYHAT_CHANNEL_ROUTER": "1" if router else "0",
                    },
                    "enabled_tools": (
                        ["channel_api_help", "channel_typing"]
                        if router
                        else [
                            "channel_api",
                            "channel_api_help",
                            "channel_typing",
                        ]
                    ),
                }
                if event
                else None
            )

            async def approve(request):
                return False if router else await self.approval(task["id"], request)

            skill_path = self.codex_skill(root, name)
            agent = native.Codex(cwd=root, approve=approve, mcp=mcp)
            try:
                await agent.start()
                return await agent.turn(
                    prompt,
                    native_id=task["native_id"] if task else None,
                    instructions=instruction,
                    router=router,
                    on_session=callback,
                    on_model=self.report_model,
                    skill_path=skill_path,
                    images=[
                        item["path"]
                        for item in (event or {}).get("attachments", [])
                        if item["kind"] == "image"
                    ],
                )
            finally:
                await agent.close()
        finally:
            self.capabilities.pop(capability, None)
            if event:
                await self.transports.stop_receipt(event)
            if router:
                await self.transports.stop_typing(tool_id)

    async def route(self, row):
        event = json.loads(row["payload"])
        if row["task_id"]:
            return row["task_id"]
        from hermes_runtime.channel_agent.attachments import prepare_media

        raw = event.get("raw") or {}
        if (
            event.get("provider") in {"telegram", "slack"}
            and (raw.get("photo") or raw.get("voice") or raw.get("files"))
            and not event.get("attachments")
            and not event.get("media_error")
        ):
            event = await prepare_media(
                event, self.transports, self.directory / "attachments"
            )
            with self.state.db:
                self.state.db.execute(
                    "UPDATE events SET payload=? WHERE id=?",
                    (json.dumps(event), row["id"]),
                )
        if event.get("provider") == "telegram" and re.fullmatch(
            r"/activity(?:@[A-Za-z0-9_]+)?", event.get("text", "").strip(), re.I
        ):
            from hermes_runtime.channel_agent.transports import plugin_module

            button = await asyncio.to_thread(
                plugin_module("capabilities.channels.sessions").button
            )
            await self.transports.action(
                "command:" + row["id"],
                event,
                {
                    "method": "sendMessage",
                    "action_id": "sessions-link",
                    "params": {
                        "text": "Your sessions",
                        "reply_markup": {"inline_keyboard": [[button]]},
                    },
                },
            )
            self.state.event_state(row["id"], "done")
            await self.transports.stop_receipt(event)
            return None
        explicit = event.get("task_id")
        if explicit:
            task = self.state.task(explicit)
            if not task or task["framework"] != self.framework:
                raise ValueError("Explicit task is unavailable.")
            decision = {
                "task_id": explicit,
                "title": task["title"],
                "clarification": None,
            }
        else:
            context = self.state.context(event)
            context["active_framework"] = self.framework
            # Other frameworks remain visible as context, but State.route
            # rejects resuming their incompatible native session IDs.
            _, text = await asyncio.wait_for(
                self.run_native(
                    json.dumps(context, ensure_ascii=False), event=event, router=True
                ),
                120,
            )
            decision = json.loads(text)
            if set(decision) != {"task_id", "title", "clarification"} or not isinstance(
                decision["title"], str
            ):
                raise ValueError("Router returned an invalid decision.")
        return self.state.route(row["id"], decision, self.framework)

    async def work(self, row, task_id):
        self.state.event_state(row["id"], "running")
        task = self.state.task(task_id)
        self.state.update_task(task_id, status="running")
        event = json.loads(
            self.state.db.execute(
                "SELECT payload FROM events WHERE id=?", (row["id"],)
            ).fetchone()[0]
        )
        try:
            native_id, _ = await self.run_native(
                format_message(event, self.directory, event_id=row["id"]),
                task=task,
                event=event,
            )
            # Completion is work completion, not a requirement to send a message.
            self.state.update_task(
                task_id,
                status="completed",
                native_id=native_id,
                summary=str(event.get("text", ""))[:2000],
            )
            self.state.event_state(row["id"], "done")
        except asyncio.CancelledError:
            self.state.update_task(task_id, status="interrupted")
            self.state.event_state(row["id"], "interrupted")
            raise
        except Exception:
            log.warning("native_turn_failed")
            self.state.update_task(task_id, status="failed", error="native_turn_failed")
            self.state.event_state(row["id"], "failed")
        finally:
            with self.state.db:
                self.state.db.execute(
                    "UPDATE approvals SET decision='deny' WHERE task_id=? AND decision IS NULL",
                    (task_id,),
                )
            await self.transports.stop_typing(task_id)
            self.workers.pop(task_id, None)
            self.publish()

    async def stop_task(self, provider, reference):
        if provider == "telegram":
            task_id = self.state.setting("draft:" + reference)
            if task_id in self.workers:
                self.workers[task_id].cancel()
        elif provider == "slack":
            task_id = self.state.setting("slack_status:" + reference)
            if task_id in self.workers:
                self.workers[task_id].cancel()
                # Stop is a transport lifecycle event, not an authored reply.
                channel, thread = json.loads(reference)
                await self.transports.request(
                    "slack",
                    "agents.sessions.setStatus",
                    {"channel_id": channel, "thread_ts": thread, "status": "active"},
                )

    async def run(self):
        lock = (self.directory / "receiver.lock").open("a")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.state.recover()
        prepare_socket_directory(self.directory)
        self.socket_path.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(
            self.connection, path=self.socket_path, limit=2**20
        )
        self.socket_path.chmod(0o600)
        for sig in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(sig, self.shutdown.set)
        publisher = asyncio.create_task(self.publish_status())
        try:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.discover_model(), 10)
            await self.transports.start()
            while not self.shutdown.is_set():
                if publisher.done():
                    # Let heartbeat supervision restart an unhealthy receiver.
                    # A live PID alone must not strand intake permanently.
                    await publisher
                    raise RuntimeError("Channel health publisher stopped.")
                self.publish()
                # One router is independent of two concurrent native workers.
                for row in self.state.queued(ready_only=True):
                    if len(self.workers) >= 2:
                        break
                    try:
                        task_id = await self.route(row)
                        self.error = None
                        if task_id and task_id not in self.workers:
                            self.workers[task_id] = asyncio.create_task(
                                self.work(row, task_id)
                            )
                    except ValueError:
                        log.warning("routing_decision_invalid")
                        self.error = "routing_decision_invalid"
                        self.state.route_failed(row["id"], permanent=True)
                    except Exception:
                        log.warning("routing_unavailable")
                        self.error = "routing_unavailable"
                        self.state.route_failed(row["id"])
                await asyncio.sleep(1)
        finally:
            publisher.cancel()
            await asyncio.gather(publisher, return_exceptions=True)
            for task in list(self.workers.values()):
                task.cancel()
            await asyncio.gather(*self.workers.values(), return_exceptions=True)
            try:
                await self.transports.close()
            finally:
                server.close()
                await server.wait_closed()
                self.socket_path.unlink(missing_ok=True)
                self.state.close()
                lock.close()


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--framework", choices=["codex", "claude_code"], required=True)
    args = parser.parse_args()
    os.umask(0o077)
    # Only fixed diagnostic codes are logged here, never provider exceptions,
    # event payloads, auth URLs, or credentials. Bound retention locally.
    path = Path(args.state_dir) / "receiver.log"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.touch(mode=0o600, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=262144, backupCount=2)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger = logging.getLogger("hermes_runtime.channel_agent")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    asyncio.run(Service(args.state_dir, args.framework).run())


if __name__ == "__main__":
    main()
