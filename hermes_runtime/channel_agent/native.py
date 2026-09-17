"""Documented native session interfaces; no transcript or credential parsing."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import signal
import sys
import uuid
from pathlib import Path

from hermes_runtime.hermes_cli import find_hermes_binary, run_process

ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "task_id": {"type": ["string", "null"]},
        "title": {"type": "string"},
        "clarification": {"type": ["string", "null"]},
    },
    "required": ["task_id", "title", "clarification"],
    "additionalProperties": False,
}


class CodexRequestError(RuntimeError):
    """Classify the known pre-turn conflict without retaining provider output."""

    def __init__(self, error):
        self.active_writer = (
            isinstance(error, dict)
            and error.get("code") == -32600
            and isinstance(error.get("message"), str)
            and re.fullmatch(
                r"thread [0-9a-f-]{36} already has an active writer", error["message"]
            )
            is not None
        )
        super().__init__(
            "codex_active_writer" if self.active_writer else "codex_request_failed"
        )


def model_name(value):
    """Only a model identifier may cross into health telemetry."""
    return (
        value
        if isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}", value)
        else None
    )


def child_env():
    # Provider login stays in its supported home. Channel/platform credentials
    # do not cross the tool boundary as environment variables.
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("TELEGRAM_", "SLACK_", "TINYHAT_", "HERMES_"))
        and k not in {"OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"}
    }


async def probe(framework):
    command = {"codex": "codex", "claude_code": "claude", "hermes": "hermes"}[framework]
    binary = find_hermes_binary() if framework == "hermes" else shutil.which(command)
    if not binary:
        return {"installed": False, "authenticated": False}
    version = await run_process(
        [str(binary), "--version"],
        timeout_seconds=15,
        env=child_env(),
        replace_env=True,
    )
    if framework == "hermes":
        return {"installed": bool(version.get("ok")), "authenticated": True}
    args = (
        [binary, "login", "status"]
        if framework == "codex"
        else [binary, "auth", "status", "--json"]
    )
    result = await run_process(
        args, timeout_seconds=20, env=child_env(), replace_env=True
    )
    authenticated = bool(result.get("ok"))
    if framework == "claude_code":
        try:
            authenticated = (
                authenticated
                and json.loads(result.get("stdout", "{}")).get("loggedIn") is True
            )
        except (ValueError, AttributeError):
            authenticated = False
    return {"installed": bool(version.get("ok")), "authenticated": authenticated}


async def terminate(process):
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), 5)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()


class Codex:
    def __init__(self, *, cwd, approve, mcp=None):
        self.cwd, self.approve = cwd, approve
        self.mcp = mcp
        self.pending, self.sequence, self.requests = {}, 0, set()
        self.done = None
        self.text = ""

    async def start(self):
        self.process = await asyncio.create_subprocess_exec(
            "codex",
            "app-server",
            "--listen",
            "stdio://",
            cwd=self.cwd,
            env=child_env(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            limit=2**22,
        )
        self.reader = asyncio.create_task(self.read())
        await self.request(
            "initialize",
            {
                "clientInfo": {"name": "tinyhat", "version": "1.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        await self.send({"method": "initialized"})

    async def send(self, message):
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        await self.process.stdin.drain()

    async def request(self, method, params):
        self.sequence += 1
        request_id = self.sequence
        future = self.pending[request_id] = asyncio.get_running_loop().create_future()
        await self.send({"id": request_id, "method": method, "params": params})
        try:
            return await asyncio.wait_for(future, 60)
        finally:
            self.pending.pop(request_id, None)

    async def read(self):
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if "method" not in message:
                    future = self.pending.get(message.get("id"))
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(CodexRequestError(message["error"]))
                        else:
                            future.set_result(message.get("result", {}))
                elif "id" in message:
                    task = asyncio.create_task(self.answer(message))
                    self.requests.add(task)
                    task.add_done_callback(self.requests.discard)
                elif message["method"] == "item/completed":
                    item = message.get("params", {}).get("item", {})
                    if item.get("type") == "agentMessage":
                        self.text = str(item.get("text", ""))[:12000]
                elif (
                    message["method"] == "turn/completed"
                    and self.done
                    and not self.done.done()
                ):
                    self.done.set_result(message["params"]["turn"])
        except (ValueError, OSError):
            pass
        finally:
            for future in [*self.pending.values(), self.done]:
                if future and not future.done():
                    future.set_exception(
                        RuntimeError("Native Codex connection closed.")
                    )

    async def answer(self, message):
        method, params = message["method"], message.get("params", {})
        try:
            if method in {
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
            }:
                allowed = await self.approve({"method": method, "request": params})
                answer = {"decision": "accept" if allowed else "decline"}
            elif method == "mcpServer/elicitation/request":
                # This private stdio server exposes only the two owner-scoped
                # messaging tools. Reply authorization is already established
                # by authenticated ingress; no shell/file approval is implied.
                own_tool = (
                    self.mcp is not None
                    and params.get("serverName") == "tinyhat_channel"
                    and params.get("mode") == "form"
                    and params.get("_meta", {}).get("codex_approval_kind")
                    == "mcp_tool_call"
                )
                answer = {
                    "action": "accept" if own_tool else "decline",
                    "content": {} if own_tool else None,
                }
            else:
                # Unsupported approval/elicitation must not implicitly authorize.
                await self.send(
                    {
                        "id": message["id"],
                        "error": {
                            "code": -32601,
                            "message": "Use the native app for this request.",
                        },
                    }
                )
                return
        except Exception:
            answer = {
                "contentItems": [
                    {"type": "inputText", "text": "The scoped tool could not complete."}
                ],
                "success": False,
            }
        await self.send({"id": message["id"], "result": answer})

    async def turn(
        self,
        prompt,
        *,
        native_id,
        instructions,
        router=False,
        on_session=None,
        on_model=None,
        images=(),
    ):
        config = {
            "cwd": str(self.cwd),
            "developerInstructions": instructions,
            "approvalPolicy": "untrusted",
            "sandbox": "workspace-write",
        }
        if self.mcp:
            config["config"] = {"mcp_servers.tinyhat_channel": self.mcp}
        if router:
            config.update(
                approvalPolicy="never",
                sandbox="read-only",
                config={"tools": {"web_search": False}, "mcp_servers": {}},
            )
            if self.mcp:
                config["config"]["mcp_servers"] = {"tinyhat_channel": self.mcp}
        if native_id:
            try:
                result = await self.request(
                    "thread/resume", {**config, "threadId": native_id}
                )
            except CodexRequestError as exc:
                if not exc.active_writer:
                    raise
                # The desktop may own this thread. Continue its stored history
                # through the official fork API; never steal its writer or
                # replay a turn that might already have performed actions.
                result = await self.request(
                    "thread/fork", {**config, "threadId": native_id}
                )
                logging.getLogger(__name__).warning("codex_writer_continuation")
        else:
            result = await self.request("thread/start", {**config, "ephemeral": router})
        self.thread_id = result["thread"]["id"]
        if on_model:
            on_model(model_name(result.get("model")))
        if on_session:
            on_session(self.thread_id)
        self.done = asyncio.get_running_loop().create_future()
        params = {
            "threadId": self.thread_id,
            "input": [
                {"type": "text", "text": prompt},
                *({"type": "localImage", "path": str(path)} for path in images),
            ],
        }
        if router:
            params["outputSchema"] = ROUTE_SCHEMA
            params["effort"] = "low"
        started = await self.request("turn/start", params)
        self.turn_id = started["turn"]["id"]
        turn = await self.done
        if turn.get("status") != "completed":
            raise RuntimeError("Native Codex turn did not complete.")
        return self.thread_id, self.text

    async def close(self):
        for task in self.requests:
            task.cancel()
        if getattr(self, "process", None):
            if getattr(self, "turn_id", None) and self.done and not self.done.done():
                with contextlib.suppress(Exception):
                    await self.request(
                        "turn/interrupt",
                        {"threadId": self.thread_id, "turnId": self.turn_id},
                    )
            await terminate(self.process)
        if not getattr(self, "reader", None):
            return
        self.reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.reader


async def claude_turn(
    prompt,
    *,
    native_id,
    instructions,
    cwd,
    socket_path,
    capability,
    router=False,
    on_session=None,
    on_model=None,
):
    session_id = native_id or str(uuid.uuid4())
    args = [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--system-prompt-snapshot",
        "off",
        "--append-system-prompt",
        instructions,
        "--resume" if native_id else "--session-id",
        session_id,
    ]
    env = child_env()
    mcp = {
        "mcpServers": {
            "tinyhat_channel": {
                "command": sys.executable,
                "args": ["-m", "hermes_runtime.channel_agent.mcp"],
                "env": {
                    "TINYHAT_CHANNEL_SOCKET": str(socket_path),
                    "TINYHAT_CHANNEL_CAPABILITY": capability,
                    "TINYHAT_CHANNEL_ROUTER": "1" if router else "0",
                    "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
                },
            }
        }
    }
    if router:
        args += [
            "--tools",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            json.dumps(mcp),
            "--allowedTools",
            "mcp__tinyhat_channel__channel_api_help,mcp__tinyhat_channel__channel_typing",
            "--json-schema",
            json.dumps(ROUTE_SCHEMA),
        ]
    else:
        args += [
            "--mcp-config",
            json.dumps(mcp),
            "--allowedTools",
            "mcp__tinyhat_channel__channel_api,mcp__tinyhat_channel__channel_api_help,mcp__tinyhat_channel__channel_typing",
            "--permission-prompt-tool",
            "mcp__tinyhat_channel__request_approval",
        ]
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
        limit=2**22,
    )
    result = None
    try:
        process.stdin.write(prompt.encode())
        await process.stdin.drain()
        process.stdin.close()
        if on_session:
            on_session(session_id)
        while line := await process.stdout.readline():
            message = json.loads(line)
            if (
                message.get("type") == "system"
                and message.get("subtype") == "init"
                and on_model
            ):
                on_model(model_name(message.get("model")))
            if message.get("type") == "result":
                result = message
        await process.wait()
        if not result or result.get("is_error") or process.returncode != 0:
            raise RuntimeError("Native Claude Code turn did not complete.")
        text = (
            json.dumps(result["structured_output"])
            if router
            else str(result.get("result", ""))
        )
        return result.get("session_id", session_id), text[:12000]
    finally:
        await terminate(process)
