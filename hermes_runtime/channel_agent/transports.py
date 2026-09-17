"""Thin native provider transports with owner and conversation checks."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import logging
import os
import re
import sys
from types import ModuleType

from hermes_runtime.plugin_manager import DEFAULT_TINYHAT_PLUGIN_NAME, plugin_dir
from hermes_runtime.runtime_env import env_file_candidates, read_env_values

log = logging.getLogger(__name__)


def plugin_module(name):
    package = "_tinyhat_native_channels"
    if package not in sys.modules:
        module = ModuleType(package)
        module.__path__ = [str(plugin_dir(DEFAULT_TINYHAT_PLUGIN_NAME))]
        sys.modules[package] = module
    return importlib.import_module(package + "." + name)


class Transports:
    def __init__(self, state, accept, stop_task):
        self.state, self._accept, self.stop_task = state, accept, stop_task
        self.values = {**os.environ, **read_env_values(env_file_candidates())}
        self.methods = json.loads(
            (
                plugin_dir(DEFAULT_TINYHAT_PLUGIN_NAME)
                / "capabilities/channels/methods.json"
            ).read_text()
        )
        self.connected = {
            name: False
            for name, present in (
                ("telegram", self.values.get("TELEGRAM_BOT_TOKEN")),
                ("slack", self.values.get("SLACK_BOT_TOKEN")),
                ("email", self.values.get("TINYHAT_EMAIL_CHANNEL_ENABLED") == "1"),
            )
            if present
        }
        self.session = None
        self.listeners = []
        self.listener_by_provider = {}
        self.draining = False
        self.typing = {}
        self.receipt_jobs = {}
        self.receipt_events = {}
        self.receipt_default = True
        try:
            policy = json.loads(
                (
                    plugin_dir(DEFAULT_TINYHAT_PLUGIN_NAME)
                    / "skills/tinyhat-respond/receipt.json"
                ).read_text()
            )
            if type(policy.get("enabled")) is bool:
                self.receipt_default = policy["enabled"]
        except (OSError, ValueError, AttributeError):
            pass  # Older plugins still receive the standard activity receipt.

    @staticmethod
    def receipt_scope(event):
        return "receipt_feedback:" + json.dumps(
            [event["provider"], event["conversation"], event.get("sender", "")]
        )

    def receipt_enabled(self, event):
        return self.state.setting(self.receipt_scope(event), self.receipt_default)

    def accept(self, key, event, **kwargs):
        event = {**event, "event_id": key}
        inserted = self._accept(key, event, **kwargs)
        # Authorization has already passed; acknowledge only a durable new event.
        # Never wait on provider feedback before acknowledging Socket Mode or
        # polling the next Telegram update, even while both workers are occupied.
        if (
            inserted
            and not self.draining
            and event["provider"] in {"telegram", "slack"}
            and self.receipt_enabled(event)
        ):
            if len(self.receipt_events) >= 64:
                log.warning("channel_receipt_capacity")
                return inserted
            receipt_id = "receipt:" + key
            self.receipt_events[receipt_id] = event
            job = asyncio.create_task(self.receipt(receipt_id, event))
            self.receipt_jobs[receipt_id] = job
            job.add_done_callback(lambda _: self.receipt_jobs.pop(receipt_id, None))
        return inserted

    async def receipt(self, receipt_id, event):
        try:
            await asyncio.wait_for(
                self.keep_typing(receipt_id, event, 60, _initial_receipt=True), 5
            )
            log.info("channel_receipt_started")
        except Exception:
            log.warning("channel_receipt_unavailable")
        finally:
            lease = self.typing.get(receipt_id)
            if not lease or lease.done():
                self.receipt_events.pop(receipt_id, None)
                self.typing.pop(receipt_id, None)

    async def stop_receipt(self, event):
        receipt_id = "receipt:" + event.get("event_id", "")
        job = self.receipt_jobs.pop(receipt_id, None)
        if job:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        await self.stop_typing(receipt_id)
        self.receipt_events.pop(receipt_id, None)

    async def request(self, provider, method, payload, *, app=False):
        if provider == "telegram":
            url = (
                "https://api.telegram.org/bot"
                + self.values["TELEGRAM_BOT_TOKEN"]
                + "/"
                + method
            )
            headers = {}
        else:
            url = "https://slack.com/api/" + method
            key = "SLACK_APP_TOKEN" if app else "SLACK_BOT_TOKEN"
            headers = {"Authorization": "Bearer " + self.values[key]}
        try:
            async with self.session.post(
                url, json=payload, headers=headers, allow_redirects=False
            ) as response:
                if response.status != 200:
                    raise RuntimeError("Channel API unavailable.")
                raw = await response.content.read(2**20)
                result = json.loads(raw)
                if not result.get("ok"):
                    raise RuntimeError("Channel API rejected the request.")
                return result.get("result", result)
        except Exception:
            # Never echo provider response bodies, URLs or token-bearing errors.
            raise RuntimeError(
                "Channel request failed; inspect its receipt before retrying."
            ) from None

    async def start(self):
        # The control process uses only the standard library. Provider I/O
        # runs in the separate receiver's Hermes environment.
        self.draining = False
        if self.session is None or self.session.closed:
            import aiohttp

            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=40)
            )
        if not self.connected:
            raise RuntimeError("No channel credentials are installed.")
        self.ensure_listeners()

    def ensure_listeners(self):
        """Supervise intake separately from routing and individual workers."""
        if self.draining or self.session is None:
            return
        for provider in self.connected:
            task = self.listener_by_provider.get(provider)
            if task is None or task.done():
                if task and not task.cancelled():
                    task.exception()  # Retrieve without logging secret-bearing errors.
                self.connected[provider] = False
                self.listener_by_provider[provider] = asyncio.create_task(
                    self.listen(provider)
                )
        self.listeners = list(self.listener_by_provider.values())

    async def listen(self, provider):
        while not self.draining:
            try:
                if provider == "telegram":
                    await self.request(provider, "getMe", {})
                    info = await self.request(provider, "getWebhookInfo", {})
                    if info.get("url"):
                        raise RuntimeError("Telegram webhook still owns intake.")
                elif provider == "slack":
                    await self.request(provider, "auth.test", {})
                await getattr(self, provider)()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            finally:
                self.connected[provider] = False
            if not self.draining:
                log.warning("%s_listener_retry", provider)
                await asyncio.sleep(5)

    async def telegram(self):
        owner = self.values.get("TELEGRAM_ALLOWED_USERS", "").strip()
        if not owner.isdigit():
            self.connected["telegram"] = False
            return
        while not self.draining:
            try:
                updates = await self.request(
                    "telegram",
                    "getUpdates",
                    {
                        "offset": self.state.setting("telegram_offset", 0),
                        "timeout": 20,
                        "allowed_updates": [
                            "message",
                            "edited_message",
                            "stopped_message_generation",
                        ],
                    },
                )
                self.connected["telegram"] = True
                for update in updates:
                    next_offset = int(update["update_id"]) + 1
                    stop = update.get("stopped_message_generation")
                    if stop and str(stop.get("chat", {}).get("id")) == owner:
                        await self.stop_task("telegram", str(stop.get("draft_id", "")))
                    message = update.get("message") or update.get("edited_message")
                    if (
                        message
                        and str(message.get("from", {}).get("id")) == owner
                        and str(message.get("chat", {}).get("id")) == owner
                        and message["chat"].get("type") == "private"
                        and not message["from"].get("is_bot")
                    ):
                        reply = message.get("reply_to_message") or {}
                        payload = {
                            "provider": "telegram",
                            "conversation": owner,
                            "sender": owner,
                            "text": message.get("text") or message.get("caption") or "",
                            "message_id": str(message["message_id"]),
                            "thread_id": message.get("message_thread_id"),
                            "reply_to": str(reply.get("message_id", "")),
                            "referenced_text": reply.get("text", "")[:4000],
                            "received_at": message.get("date"),
                            "raw": message,
                        }
                        self.accept(
                            "telegram:" + str(update["update_id"]),
                            payload,
                            cursor=("telegram_offset", next_offset),
                        )
                    else:
                        self.state.set("telegram_offset", next_offset)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.connected["telegram"] = False
                log.warning("telegram_receive_retry")
                await asyncio.sleep(5)

    async def slack(self):
        import aiohttp

        owners = set(self.values.get("SLACK_ALLOWED_USERS", "").split(","))
        while not self.draining:
            try:
                opened = await self.request(
                    "slack", "apps.connections.open", {}, app=True
                )
                url = opened.get("url", "")
                from urllib.parse import urlsplit

                parsed = urlsplit(url)
                if parsed.scheme != "wss" or not (parsed.hostname or "").endswith(
                    ".slack.com"
                ):
                    raise RuntimeError("Invalid Slack Socket Mode endpoint.")
                async with self.session.ws_connect(
                    url, heartbeat=15, max_msg_size=2**20
                ) as ws:
                    self.connected["slack"] = True
                    async for frame in ws:
                        if self.draining:
                            break
                        if frame.type != aiohttp.WSMsgType.TEXT:
                            continue
                        envelope = json.loads(frame.data)
                        payload = envelope.get("payload") or {}
                        event = payload.get("event") or {}
                        if (
                            event.get("type") == "agent_session_stopped"
                            and event.get("user") in owners
                        ):
                            await self.stop_task(
                                "slack",
                                json.dumps(
                                    [event.get("channel"), event.get("thread_ts")]
                                ),
                            )
                        if (
                            event.get("type") in {"message", "app_mention"}
                            and event.get("user") in owners
                            and not event.get("bot_id")
                            and event.get("subtype") in {None, "file_share"}
                        ):
                            self.accept(
                                "slack:"
                                + str(
                                    payload.get("event_id") or envelope["envelope_id"]
                                ),
                                {
                                    "provider": "slack",
                                    "conversation": event["channel"],
                                    "sender": event["user"],
                                    "team_id": payload.get("team_id"),
                                    "text": event.get("text", ""),
                                    "message_id": event["ts"],
                                    "thread_id": event.get("thread_ts"),
                                    "received_at": event.get("event_ts"),
                                    "raw": event,
                                },
                            )
                        # Receipt is independent of the agent's response, and follows
                        # durable ingest. A full inbox leaves the envelope unacked.
                        if envelope.get("envelope_id"):
                            await ws.send_json({"envelope_id": envelope["envelope_id"]})
                self.connected["slack"] = False
                if not self.draining:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.connected["slack"] = False
                log.warning("slack_receive_retry")
                await asyncio.sleep(5)

    async def email(self):
        # Plugin uses authenticated owner ingress and the existing mailbox cursor.
        loop = asyncio.get_running_loop()

        async def accepted(key, payload):
            self.accept(key, payload)

        def poll():
            os.environ.update(
                {
                    k: v
                    for k, v in self.values.items()
                    if k.startswith(("TINYHAT_", "HERMES_"))
                }
            )
            inbox = plugin_module("capabilities.mail.ingress").OwnerInbox()
            try:
                inbox.poll(
                    lambda key, value: asyncio.run_coroutine_threadsafe(
                        accepted(key, value), loop
                    ).result(30)
                )
            finally:
                inbox.close()

        while not self.draining:
            try:
                running_poll = asyncio.create_task(asyncio.to_thread(poll))
                try:
                    await asyncio.shield(running_poll)
                except asyncio.CancelledError:
                    # A cancelled to_thread cannot stop its thread. Wait for its
                    # durable ingest before allowing another receiver to start.
                    await running_poll
                    raise
                self.connected["email"] = True
            except asyncio.CancelledError:
                raise
            except Exception:
                self.connected["email"] = False
                log.warning("email_receive_retry")
            await asyncio.sleep(15)

    def help(self, event):
        provider = event["provider"]
        return {
            "provider": provider,
            "methods": self.methods[provider],
            "documentation": {
                "telegram": "https://core.telegram.org/bots/api",
                "slack": "https://docs.slack.dev/reference/methods/",
                "email": "send accepts subject and body; owner recipient is fixed.",
            }[provider],
            "helpers": ["channel_typing"] if provider in {"telegram", "slack"} else [],
            "receipt_feedback": self.receipt_enabled(event),
            "reply_thread": (event.get("thread_id") or event.get("message_id"))
            if provider == "slack" else event.get("thread_id"),
            "scope": "The runtime supplies destination, thread and streaming recipient. Edit only messages returned to this task.",
        }

    async def stop_typing(self, task_id):
        running = self.typing.pop(task_id, None)
        if running:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    async def keep_typing(
        self, task_id, event, seconds, *, receipt_feedback=None, _initial_receipt=False
    ):
        # Receipt activity and agent-requested activity share a bounded lease.
        # Neither sends an authored reply or forces one response per event.
        if (
            event["provider"] not in {"telegram", "slack"}
            or type(seconds) is not int
            or not 0 <= seconds <= 120
        ):
            raise ValueError("Activity needs a Telegram/Slack event and 0–120 seconds.")
        if receipt_feedback is not None:
            if type(receipt_feedback) is not bool:
                raise ValueError("Receipt feedback must be a boolean.")
            self.state.set(self.receipt_scope(event), receipt_feedback)
            if not receipt_feedback:
                await asyncio.gather(*(
                    self.stop_receipt(pending)
                    for pending in list(self.receipt_events.values())
                    if self.receipt_scope(pending) == self.receipt_scope(event)
                ))
        if not _initial_receipt:
            # A router renewal must finish the initial provider request before
            # replacing its lease. Its timeout must not clear the newer lease.
            await self.stop_receipt(event)
        await self.stop_typing(task_id)
        if not seconds or not self.receipt_enabled(event):
            return {"stopped": True}
        if task_id.startswith("receipt:"):
            self.receipt_events[task_id] = event
        try:
            return await self.start_typing(task_id, event, seconds)
        finally:
            self.track_receipt_lease(task_id)

    async def start_typing(self, task_id, event, seconds):
        if event["provider"] == "slack":
            return await self.slack_typing(task_id, event, seconds)
        params = {"chat_id": event["conversation"], "action": "typing"}
        if event.get("thread_id"):
            params["message_thread_id"] = event["thread_id"]
        await self.request("telegram", "sendChatAction", params)

        async def renew():
            end = asyncio.get_running_loop().time() + seconds
            try:
                while asyncio.get_running_loop().time() + 4 < end:
                    await asyncio.sleep(4)
                    await self.request("telegram", "sendChatAction", params)
            except Exception:
                pass  # Ephemeral feedback failing must not fail the owner's work.

        self.typing[task_id] = asyncio.create_task(renew())
        return {"typing_for_seconds": seconds}

    def track_receipt_lease(self, task_id):
        lease = self.typing.get(task_id)
        if not task_id.startswith("receipt:"):
            return
        if lease is None:
            self.receipt_events.pop(task_id, None)
            return

        def finished(_):
            if self.typing.get(task_id) is lease:
                self.typing.pop(task_id, None)
                self.receipt_events.pop(task_id, None)

        lease.add_done_callback(finished)

    async def slack_typing(self, task_id, event, seconds):
        params = {
            "channel_id": event["conversation"],
            "thread_ts": event.get("thread_id") or event["message_id"],
        }
        scope = "slack_status:" + json.dumps(
            [params["channel_id"], params["thread_ts"]]
        )
        other = self.state.setting(scope)
        if other and other != task_id:
            task = self.state.task(other)
            if other in self.typing or (
                task and task["status"] in {"running", "waiting"}
            ):
                return {"already_working": True}
        method, status, clear = "agents.sessions.setStatus", "processing", "active"
        clear_methods = [(method, clear)]
        self.state.set(scope, task_id)

        async def clear_status():
            try:
                for clear_method, clear_status in clear_methods:
                    if self.state.setting(scope) != task_id:
                        break
                    try:
                        await asyncio.wait_for(
                            self.request(
                                "slack", clear_method,
                                {**params, "status": clear_status},
                            ), 5
                        )
                    except Exception:
                        pass
            finally:
                if self.state.setting(scope) == task_id:
                    self.state.set(scope, None)

        async def lease():
            try:
                await asyncio.sleep(seconds)
            finally:
                if self.state.setting(scope) == task_id:
                    cleanup = asyncio.create_task(clear_status())
                    while not cleanup.done():
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            # A second cancellation must not cancel bounded
                            # cleanup or leave an ownerless working indicator.
                            continue

        # Own cleanup before the request: a timed-out status write may still
        # have reached Slack. Never leave an uncertain processing status forever.
        typing_lease = asyncio.create_task(lease())
        self.typing[task_id] = typing_lease
        await asyncio.sleep(0)
        try:
            try:
                await self.request("slack", method, {**params, "status": status})
            except Exception:
                method, clear = "assistant.threads.setStatus", ""
                clear_methods.append((method, clear))
                await self.request("slack", method, {**params, "status": "Working…"})
        except (Exception, asyncio.CancelledError):
            if self.typing.get(task_id) is typing_lease:
                await self.stop_typing(task_id)
            raise
        return {"typing_for_seconds": seconds}

    async def action(self, task_id, event, arguments):
        provider, method = event["provider"], arguments.get("method")
        rule = self.methods.get(provider, {}).get(method)
        params = arguments.get("params")
        action_id = arguments.get("action_id")
        if (
            rule is None
            or not isinstance(params, dict)
            or not isinstance(action_id, str)
            or not 1 <= len(action_id) <= 120
        ):
            raise ValueError("Unknown channel action.")
        params = dict(params)
        # Content remains provider-native; account/recipient/paid overrides do not.
        if set(params) & {
            "business_connection_id",
            "allow_paid_broadcast",
            "token",
            "to",
            "cc",
            "bcc",
            "username",
            "icon_url",
            "icon_emoji",
            "reply_parameters",
            "inline_message_id",
        }:
            raise ValueError("The action cannot override its conversation or identity.")
        target = rule.get("target")
        if target:
            if target in params and str(params[target]) != event["conversation"]:
                raise ValueError("The action belongs to another conversation.")
            params[target] = event["conversation"]
        ref = rule.get("message")
        if ref and not self.state.owns_message(
            task_id, provider, params.get(ref), event["conversation"]
        ):
            raise ValueError("Only this task's messages can be changed.")
        if "thread_ts" in params or "message_thread_id" in params:
            expected = event.get("thread_id") or (
                event.get("message_id") if provider == "slack" else None
            )
            supplied = params.get("thread_ts", params.get("message_thread_id"))
            if str(supplied) != str(expected):
                raise ValueError("The action belongs to another thread.")
        if event.get("thread_id") and not ref:
            params["thread_ts" if provider == "slack" else "message_thread_id"] = event[
                "thread_id"
            ]
        if provider == "slack" and method == "chat.postMessage":
            # Activity opens this thread in Slack. Keep the eventual answer in
            # that same place, including when the owner wrote in the DM root.
            params["thread_ts"] = event.get("thread_id") or event["message_id"]
        if provider == "slack" and method == "chat.startStream":
            for key, value in (
                ("recipient_user_id", event.get("sender")),
                ("recipient_team_id", event.get("team_id")),
            ):
                if key in params and params[key] != value:
                    raise ValueError("The stream belongs to another recipient.")
                if value:
                    params[key] = value
            # Root messages also need a thread for the normal Slack chat UX.
            params["thread_ts"] = event.get("thread_id") or event["message_id"]
        elif {"recipient_user_id", "recipient_team_id"} & set(params):
            raise ValueError("Recipient overrides are not supported.")
        if rule.get("thread_status"):
            thread = event.get("thread_id") or event.get("message_id")
            params["thread_ts"] = thread
            scope = json.dumps([event["conversation"], thread])
            other = self.state.setting("slack_status:" + scope)
            if other and other != task_id:
                task = self.state.task(other)
                if task and task["status"] in {"running", "waiting"}:
                    raise ValueError("Another task is using this thread's status.")
            self.state.set("slack_status:" + scope, task_id)
        if rule.get("draft"):
            draft = str(int(hashlib.sha256(task_id.encode()).hexdigest()[:15], 16) or 1)
            params[rule["draft"]] = int(draft)
            self.state.set("draft:" + draft, task_id)
        if provider == "email" and (
            set(params) - {"subject", "body"}
            or not all(isinstance(params.get(k), str) for k in ("subject", "body"))
        ):
            raise ValueError("Email requires subject and body strings only.")
        request = {"provider": provider, "method": method, "params": params}
        old = self.state.claim_action(task_id, action_id, request)
        if old:
            if provider == "email" and old["state"] != "sent":
                try:
                    key = hashlib.sha256((task_id + action_id).encode()).hexdigest()
                    result = await asyncio.to_thread(
                        plugin_module("capabilities.mail.owner").request,
                        "deliveries/" + key,
                    )
                    state = "sent" if result.get("status") == "sent" else "uncertain"
                    receipt = {
                        "provider": provider,
                        "conversation": event["conversation"],
                        "result": result,
                        "message_id": result.get("message_id"),
                    }
                    self.state.finish_action(task_id, action_id, receipt, state=state)
                    return {"state": state, "receipt": receipt}
                except Exception:
                    pass  # No delivery proof: never resubmit an uncertain send.
            return old
        if provider == "email":
            message_ids = event.get("email_message_ids") or []
            reply_to = (
                message_ids[0]
                if message_ids
                and re.fullmatch(r"[^<>\s]{1,250}@[^<>\s]{1,250}", message_ids[0])
                else None
            )
            result = await asyncio.to_thread(
                plugin_module("capabilities.mail.owner").send_owner,
                {
                    **params,
                    "in_reply_to": "<" + reply_to + ">" if reply_to else None,
                    "idempotency_key": hashlib.sha256(
                        (task_id + action_id).encode()
                    ).hexdigest(),
                },
            )
        else:
            result = await self.request(provider, method, params)
        message_id = (
            result.get("message_id", result.get("ts"))
            if isinstance(result, dict)
            else None
        )
        receipt = {
            "provider": provider,
            "conversation": event["conversation"],
            "message_id": message_id or (params.get(ref) if ref else None),
            "draft_id": params.get("draft_id"),
            "result": result,
        }
        state = (
            "uncertain"
            if provider == "email" and result.get("status") != "sent"
            else "sent"
        )
        self.state.finish_action(task_id, action_id, receipt, state=state)
        return {"state": state, "receipt": receipt}

    async def stop_intake(self):
        self.draining = True
        for task in self.listeners:
            task.cancel()
        await asyncio.gather(*self.listeners, return_exceptions=True)
        self.listeners = []
        self.listener_by_provider = {}
        for provider in self.connected:
            self.connected[provider] = False
        # Confirm only the Telegram updates already committed locally. A next
        # update returned here remains unacknowledged for the next receiver.
        offset = self.state.setting("telegram_offset")
        if (
            offset
            and self.values.get("TELEGRAM_BOT_TOKEN")
            and self.session
            and not self.session.closed
        ):
            await self.request(
                "telegram", "getUpdates", {"offset": offset, "timeout": 0, "limit": 1}
            )

    async def close(self):
        try:
            await self.stop_intake()
        finally:
            await asyncio.gather(*(
                self.stop_receipt(event) for event in list(self.receipt_events.values())
            ))
            await asyncio.gather(*(
                self.stop_typing(task_id) for task_id in list(self.typing)
            ))
            if self.session:
                await self.session.close()
