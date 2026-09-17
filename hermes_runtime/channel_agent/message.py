"""Readable native-session input; the full event stays in our local inbox."""

from __future__ import annotations

import json
from pathlib import Path


def _line(value: object) -> str:
    """Keep transport metadata on one line without changing message text."""
    return " ".join(str(value).split())


def format_message(event: dict, directory: Path) -> str:
    """Render the owner's content, with source/attachment context underneath.

    Routing and scoped tools still receive the original structured event. The
    SQLite reference is to Tinyhat's own inbox, never a provider's private DB.
    """
    parts = [str(event.get("text") or "").strip() or "[Attachment]"]
    if event.get("referenced_text"):
        quote = "\n".join(
            "> " + line for line in str(event["referenced_text"]).splitlines()
        )
        parts.append("Replying to:\n" + quote)
    attachments = event.get("attachments") or []
    if attachments:
        parts.append(
            "Attachments on this Computer:\n"
            + "\n".join(
                f"- {_line(item['kind']).capitalize()}: {_line(item['path'])}"
                for item in attachments
            )
        )
    if event.get("media_error"):
        parts.append("Attachment unavailable: " + str(event["media_error"]))
    if event.get("clarification"):
        parts.append("Suggested clarification: " + str(event["clarification"]))

    provider = {"telegram": "Telegram", "slack": "Slack", "email": "Email"}.get(
        event.get("provider"), "Channel"
    )
    source = [provider]
    for key, label in (
        ("message_id", "message"),
        ("thread_id", "thread"),
        ("reply_to", "reply to"),
    ):
        if event.get(key):
            source.append(f"{label} {_line(event[key])}")
    parts.append("— " + " · ".join(source))
    # A single durable reference replaces the raw transport JSON in the visible
    # turn. An agent can read the original event and media metadata when needed.
    record = json.dumps(str(event["event_id"]), ensure_ascii=False)
    parts.append(
        f"Original update (local): {directory / 'channels.sqlite3'}\n"
        f"Table: events · ID: {record}"
    )
    return "\n\n".join(parts)
