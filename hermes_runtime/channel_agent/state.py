"""Private, durable message receipt, task and outgoing-action bookkeeping."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


class State:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        self.db = sqlite3.connect(directory / "channels.sqlite3")
        self.db.row_factory = sqlite3.Row
        os.chmod(directory / "channels.sqlite3", 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, received REAL NOT NULL, payload TEXT NOT NULL,
                task_id TEXT, state TEXT NOT NULL DEFAULT 'queued');
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, framework TEXT NOT NULL, native_id TEXT,
                title TEXT NOT NULL, status TEXT NOT NULL, updated REAL NOT NULL,
                summary TEXT NOT NULL DEFAULT '', error TEXT);
            CREATE TABLE IF NOT EXISTS actions (
                task_id TEXT NOT NULL, id TEXT NOT NULL, request TEXT NOT NULL,
                state TEXT NOT NULL, receipt TEXT, PRIMARY KEY(task_id,id));
            CREATE TABLE IF NOT EXISTS approvals (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, request TEXT NOT NULL,
                decision TEXT, created REAL NOT NULL);
        """)
        self.db.commit()

    def setting(self, key, default=None):
        row = self.db.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value))
            )

    def ingest(self, key, payload, *, cursor=None):
        payload = {**payload, "event_id": key}
        with self.db:
            count = self.db.execute(
                "SELECT count(*) FROM events WHERE state='queued'"
            ).fetchone()[0]
            if (
                count >= 500
                and not self.db.execute(
                    "SELECT 1 FROM events WHERE id=?", (key,)
                ).fetchone()
            ):
                raise RuntimeError(
                    "Channel inbox is full; leave the provider update unacknowledged."
                )
            inserted = self.db.execute(
                "INSERT OR IGNORE INTO events(id,received,payload) VALUES (?,?,?)",
                (key, time.time(), json.dumps(payload, ensure_ascii=False)),
            ).rowcount
            if cursor:
                self.db.execute(
                    "INSERT OR REPLACE INTO settings VALUES (?,?)",
                    (cursor[0], json.dumps(cursor[1])),
                )
        return bool(inserted)

    def queued(self):
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM events WHERE state='queued' ORDER BY received LIMIT 30"
            )
        ]

    def task(self, task_id):
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def tasks(self):
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM tasks ORDER BY status IN ('running','waiting','queued') DESC, updated DESC LIMIT 30"
            )
        ]

    def route(self, event_id, decision, framework):
        with self.db:
            event = self.db.execute(
                "SELECT * FROM events WHERE id=?", (event_id,)
            ).fetchone()
            if event["task_id"]:
                return event["task_id"]
            task_id = decision.get("task_id")
            if task_id:
                task = self.task(task_id)
                if not task or task["framework"] != framework:
                    raise ValueError("Router selected an unavailable task.")
            else:
                task_id = str(uuid.uuid4())
                self.db.execute(
                    "INSERT INTO tasks(id,framework,title,status,updated) VALUES (?,?,?,'queued',?)",
                    (task_id, framework, str(decision["title"])[:120], time.time()),
                )
            payload = json.loads(event["payload"])
            payload["clarification"] = decision.get("clarification")
            self.db.execute(
                "UPDATE events SET task_id=?,payload=? WHERE id=?",
                (task_id, json.dumps(payload, ensure_ascii=False), event_id),
            )
        return task_id

    def update_task(self, task_id, *, status, native_id=None, summary=None, error=None):
        with self.db:
            self.db.execute(
                "UPDATE tasks SET status=?,updated=?,native_id=COALESCE(?,native_id),"
                "summary=COALESCE(?,summary),error=? WHERE id=?",
                (
                    status,
                    time.time(),
                    native_id,
                    summary[:2000] if summary else None,
                    error,
                    task_id,
                ),
            )

    def event_state(self, event_id, state):
        with self.db:
            self.db.execute("UPDATE events SET state=? WHERE id=?", (state, event_id))

    def context(self, incoming):
        events = []
        for row in self.db.execute(
            "SELECT payload,task_id FROM events ORDER BY received DESC LIMIT 20"
        ):
            value = json.loads(row[0])
            events.append(
                {
                    key: value.get(key)
                    for key in (
                        "event_id",
                        "provider",
                        "conversation",
                        "message_id",
                        "thread_id",
                        "reply_to",
                        "received_at",
                    )
                }
                | {"task_id": row[1], "text": str(value.get("text", ""))[:2000]}
            )
        # Only bounded public work summaries, never provider private reasoning.
        outgoing = []
        for row in self.db.execute(
            "SELECT task_id,request,receipt FROM actions WHERE state='sent' ORDER BY rowid DESC LIMIT 30"
        ):
            request, receipt = json.loads(row[1]), json.loads(row[2])
            outgoing.append(
                {
                    "task_id": row[0],
                    "provider": request["provider"],
                    "conversation": request["params"].get(
                        "chat_id", request["params"].get("channel")
                    ),
                    "message_id": receipt.get("message_id"),
                    "text": str(
                        request["params"].get("text", request["params"].get("body", ""))
                    )[:2000],
                }
            )
        return {
            "incoming": incoming,
            "recent_messages": list(reversed(events)),
            "recent_replies": list(reversed(outgoing)),
            "tasks": self.tasks(),
        }

    def claim_action(self, task_id, action_id, request):
        encoded = json.dumps(request, sort_keys=True, ensure_ascii=False)
        with self.db:
            old = self.db.execute(
                "SELECT * FROM actions WHERE task_id=? AND id=?", (task_id, action_id)
            ).fetchone()
            if old:
                if old["request"] != encoded:
                    raise ValueError("Use a new action_id for different content.")
                return {
                    "state": old["state"],
                    "receipt": json.loads(old["receipt"]) if old["receipt"] else None,
                }
            self.db.execute(
                "INSERT INTO actions VALUES (?,?,?,'uncertain',NULL)",
                (task_id, action_id, encoded),
            )
        return None

    def finish_action(self, task_id, action_id, receipt, state="sent"):
        with self.db:
            self.db.execute(
                "UPDATE actions SET state=?,receipt=? WHERE task_id=? AND id=?",
                (state, json.dumps(receipt, ensure_ascii=False), task_id, action_id),
            )

    def owns_message(self, task_id, provider, message_id, conversation):
        if message_id is None:
            return False
        for row in self.db.execute(
            "SELECT receipt FROM actions WHERE task_id=? AND state='sent'", (task_id,)
        ):
            receipt = json.loads(row[0])
            if (
                receipt.get("provider") == provider
                and str(receipt.get("message_id")) == str(message_id)
                and receipt.get("conversation") == conversation
            ):
                return True
        return False

    def approve(self, approval_id, decision):
        if decision not in {"allow", "deny"}:
            raise ValueError("Approval must be allow or deny.")
        with self.db:
            changed = self.db.execute(
                "UPDATE approvals SET decision=? WHERE id=? AND decision IS NULL",
                (decision, approval_id),
            ).rowcount
        if not changed:
            raise ValueError("Approval is no longer pending.")

    def recover(self):
        # A dispatched turn may have had side effects. Never silently replay it.
        with self.db:
            self.db.execute(
                "UPDATE events SET state='interrupted' WHERE state='running'"
            )
            self.db.execute(
                "UPDATE tasks SET status='interrupted',error='runtime_restarted' WHERE status IN ('running','waiting')"
            )
            self.db.execute(
                "UPDATE approvals SET decision='deny' WHERE decision IS NULL"
            )

    def close(self):
        self.db.close()
