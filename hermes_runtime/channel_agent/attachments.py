"""Bounded owner media intake on the Computer; channel tokens stay in transport."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from hermes_runtime.channel_agent.native import terminate

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_CACHE_BYTES = 256 * 1024 * 1024


def reserve_cache(transports, directory, protected):
    """Reclaim old media without deleting a queued/running turn's inputs."""
    keep = {Path(path).stem for path in protected}
    state = getattr(transports, "state", None)
    if state is not None:
        for row in state.db.execute(
            "SELECT payload FROM events WHERE state IN ('queued','running')"
        ):
            keep.update(
                Path(item["path"]).stem
                for item in json.loads(row[0]).get("attachments", [])
            )
    files = [(path, path.stat()) for path in directory.iterdir() if path.is_file()]
    cached = sum(stat.st_size for _, stat in files)
    # Recent temporary files can belong to a download or the bounded 240s STT
    # process. Abandoned files become reclaimable after that operation window.
    keep.update(
        path.stem
        for path, stat in files
        if path.suffix in {".part", ".transcribing"}
        and time.time() - stat.st_mtime < 300
    )
    for path, stat in sorted(files, key=lambda item: item[1].st_mtime):
        if cached + MAX_FILE_BYTES <= MAX_CACHE_BYTES:
            return
        if path.stem not in keep:
            path.unlink(missing_ok=True)
            cached -= stat.st_size
    if cached + MAX_FILE_BYTES > MAX_CACHE_BYTES:
        raise ValueError(
            "Message attachment storage is busy. Try again after active work finishes."
        )


async def download(
    transports, directory, key, suffix, url, *, headers=None, protected=()
):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / (hashlib.sha256(key.encode()).hexdigest() + suffix)
    if path.is_file():
        path.touch()
        return path
    reserve_cache(transports, directory, protected)
    temporary = path.with_suffix(".part")
    try:
        async with transports.session.get(
            url, headers=headers or {}, allow_redirects=False
        ) as response:
            if response.status != 200:
                raise RuntimeError("Attachment download failed.")
            size = 0
            with os.fdopen(
                os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb"
            ) as output:
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > MAX_FILE_BYTES:
                        raise ValueError("Message attachment is too large.")
                    output.write(chunk)
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


async def transcribe(path):
    transcript = path.with_suffix(".txt")
    if not transcript.is_file():
        pending = transcript.with_suffix(".transcribing")
        pending.touch(mode=0o600)
        process = None
        try:
            # The configured STT utility may call a provider. It does not run
            # a Hermes agent turn; routing/responding remain native.
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "hermes_runtime.openrouter_stt",
                "--input",
                str(path),
                "--output",
                str(pending),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            await asyncio.wait_for(process.wait(), 240)
            if process.returncode or not pending.stat().st_size:
                raise RuntimeError("Voice transcription did not complete.")
            pending.replace(transcript)
        finally:
            if process:
                await terminate(process)
            pending.unlink(missing_ok=True)
    return transcript.read_text()[:16000]


async def telegram_media(event, transports, directory):
    if event.get("provider") != "telegram":
        return event
    raw = event.get("raw") or {}
    photos = raw.get("photo") or []
    media = photos[-1] if photos else raw.get("voice")
    if not isinstance(media, dict) or not media.get("file_id"):
        return event
    owner = transports.values.get("TELEGRAM_ALLOWED_USERS", "").strip()
    if (
        not owner.isdigit()
        or event.get("sender") != owner
        or event.get("conversation") != owner
    ):
        raise ValueError("Media does not belong to the authenticated owner.")
    kind = "image" if photos else "voice"
    if int(media.get("file_size") or 0) > MAX_FILE_BYTES:
        raise ValueError("Message attachment is too large.")
    suffix = ".jpg" if photos else ".ogg"
    path = Path(directory) / (
        hashlib.sha256(str(media["file_id"]).encode()).hexdigest() + suffix
    )
    if not path.is_file():
        info = await transports.request(
            "telegram", "getFile", {"file_id": media["file_id"]}
        )
        remote = info.get("file_path", "")
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", remote) or any(
            part in {"", ".", ".."} for part in remote.split("/")
        ):
            raise ValueError("Invalid Telegram attachment path.")
        url = (
            "https://api.telegram.org/file/bot"
            + transports.values["TELEGRAM_BOT_TOKEN"]
            + "/"
            + remote
        )
        path = await download(transports, directory, media["file_id"], suffix, url)
    else:
        path.touch()
    result = {**event, "attachments": [{"kind": kind, "path": str(path)}]}
    if kind == "voice":
        result["text"] = (
            str(event.get("text") or "") + "\nVoice message: " + await transcribe(path)
        )
    return result


async def slack_media(event, transports, directory):
    if event.get("provider") != "slack":
        return event
    owners = {
        v.strip()
        for v in transports.values.get("SLACK_ALLOWED_USERS", "").split(",")
        if v.strip()
    }
    if event.get("sender") not in owners:
        raise ValueError("Media does not belong to an authenticated owner.")
    result = {**event, "attachments": []}
    files = (event.get("raw") or {}).get("files") or []
    if len(files) > 4:
        raise ValueError("At most four attachments per message are supported.")
    for item in files:
        file_id = item.get("id", "")
        if not re.fullmatch(r"F[A-Z0-9]{4,32}", file_id):
            raise ValueError("Invalid Slack attachment.")
        response = await transports.request("slack", "files.info", {"file": file_id})
        info = response.get("file") or {}
        if info.get("id") != file_id or info.get("user") != event["sender"]:
            raise ValueError("Attachment was not uploaded by this sender.")
        mime = info.get("mimetype", "")
        images = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }
        audio = {
            "audio/ogg": ".ogg",
            "audio/mpeg": ".mp3",
            "audio/mp4": ".m4a",
            "audio/wav": ".wav",
            "audio/webm": ".webm",
        }
        suffix = {**images, **audio}.get(mime)
        if not suffix:
            raise ValueError("Only image and audio attachments are supported.")
        if int(info.get("size") or 0) > MAX_FILE_BYTES:
            raise ValueError("Message attachment is too large.")
        url = info.get("url_private_download") or info.get("url_private", "")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "files.slack.com"
            or parsed.port not in {None, 443}
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Invalid Slack attachment origin.")
        path = await download(
            transports,
            directory,
            "slack:" + file_id,
            suffix,
            url,
            headers={"Authorization": "Bearer " + transports.values["SLACK_BOT_TOKEN"]},
            protected=[item["path"] for item in result["attachments"]],
        )
        kind = "image" if mime in images else "voice"
        result["attachments"].append({"kind": kind, "path": str(path)})
        if kind == "voice":
            result["text"] = (
                str(result.get("text") or "")
                + "\nVoice message: "
                + await transcribe(path)
            )
    return result


async def prepare_media(event, transports, directory):
    """A failed attachment is visible to the agent, never a silently lost update."""
    try:
        if event["provider"] == "telegram":
            return await telegram_media(event, transports, directory)
        return await slack_media(event, transports, directory)
    except Exception:
        return {
            **event,
            "media_error": "The attachment could not be read or transcribed. It may be unavailable, too large, unsupported, or need channel file permission.",
        }
