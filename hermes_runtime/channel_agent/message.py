"""Readable native-session input; the full event stays in our local inbox."""

from __future__ import annotations

import json
from pathlib import Path


def _line(value: object) -> str:
    """Keep transport metadata on one line without changing message text."""
    if isinstance(value, list):
        return ", ".join(_line(item) for item in value)
    return " ".join(str(value).split())


def _telegram_context(event: dict) -> list[str]:
    """Describe content not represented by the caption or prepared media."""
    raw = event.get("raw") or {}
    if event.get("provider") != "telegram" or not isinstance(raw, dict):
        return []
    context = []
    if not event.get("attachments"):
        for kind in (
            "document", "video", "audio", "sticker", "video_note", "animation",
            "location", "venue", "contact", "poll", "dice", "photo", "voice",
        ):
            if kind not in raw:
                continue
            content = raw[kind]
            details = []
            if isinstance(content, dict):
                details = [
                    _line(content[key])
                    for key in ("file_name", "mime_type", "emoji", "title", "question")
                    if content.get(key)
                ]
            label = kind.replace("_", " ").capitalize()
            context.append(
                "Attachment not downloaded: " + " · ".join([label, *details])
            )
    links = []
    for key in ("entities", "caption_entities"):
        for entity in raw.get(key) or []:
            if entity.get("type") == "text_link" and entity.get("url"):
                url = _line(entity["url"])
                if url not in links:
                    links.append(url)
    if links:
        context.append("Links in this message:\n" + "\n".join(links))
    return context


def format_message(
    event: dict, directory: Path, *, event_id: str | None = None
) -> str:
    """Render the owner's content, with source/attachment context underneath.

    Routing and scoped tools still receive the original structured event. The
    SQLite reference is to Tinyhat's own inbox, never a provider's private DB.
    """
    context = _telegram_context(event)
    content = str(event.get("text") or "").strip()
    parts = ["Subject: " + _line(event["subject"])] if event.get("subject") else []
    if content:
        parts.append(content)
    elif not context and not parts:
        parts.append("[Attachment]")
    parts.extend(context)
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
    record_id = event_id if event_id is not None else event.get("event_id")
    if record_id is not None:
        record = json.dumps(str(record_id), ensure_ascii=False)
        parts.append(
            f"Original update (local): {directory / 'channels.sqlite3'}\n"
            f"Table: events · ID: {record}"
        )
    return "\n\n".join(parts)
