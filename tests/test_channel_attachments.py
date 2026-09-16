"""Owner-only media intake and native image delivery."""

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from hermes_runtime.channel_agent.attachments import telegram_media
from hermes_runtime.channel_agent.native import Codex


class AttachmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_cache_recovers_without_evicting_active_media(self):
        from hermes_runtime.channel_agent import attachments
        from hermes_runtime.channel_agent.state import State

        async def chunks(_size):
            yield b"next"

        context = AsyncMock()
        context.__aenter__.return_value = SimpleNamespace(
            status=200, content=SimpleNamespace(iter_chunked=chunks)
        )
        with tempfile.TemporaryDirectory() as root:
            state = State(Path(root) / "state")
            media = Path(root) / "media"
            media.mkdir()
            active, old, pending = [
                media / name for name in ("active.jpg", "old.jpg", "busy.part")
            ]
            for path in (active, old, pending):
                path.write_bytes(b"12345678")
            os.utime(active, (1, 1))
            os.utime(old, (2, 2))
            state.ingest("owner-event", {"attachments": [{"path": str(active)}]})
            state.event_state("owner-event", "running")
            transport = SimpleNamespace(
                state=state, session=SimpleNamespace(get=Mock(return_value=context))
            )
            try:
                with patch.object(attachments, "MAX_CACHE_BYTES", 24), patch.object(
                    attachments, "MAX_FILE_BYTES", 4
                ):
                    path = await attachments.download(
                        transport,
                        media,
                        "new-photo",
                        ".jpg",
                        "https://example.test/photo",
                    )
                    self.assertEqual(path.read_bytes(), b"next")
                    self.assertFalse(old.exists())
                    self.assertTrue(active.exists())
                    self.assertTrue(pending.exists())
                    # A second image from this same message must keep its first.
                    second = await attachments.download(
                        transport,
                        media,
                        "next-photo",
                        ".jpg",
                        "https://example.test/photo",
                        protected=[path],
                    )
                    self.assertTrue(path.exists())
                    self.assertEqual(second.read_bytes(), b"next")
                    # Finished turns and abandoned partials become reclaimable.
                    state.event_state("owner-event", "done")
                    os.utime(pending, (0, 0))
                    third = await attachments.download(
                        transport,
                        media,
                        "third-photo",
                        ".jpg",
                        "https://example.test/photo",
                        protected=[path, second],
                    )
                    self.assertTrue(path.exists())
                    self.assertTrue(second.exists())
                    self.assertFalse(pending.exists())
                    self.assertEqual(third.read_bytes(), b"next")
            finally:
                state.close()

    async def test_voice_transcription_is_private_cached_and_delivered_as_owner_text(
        self,
    ):
        transport = SimpleNamespace(values={"TELEGRAM_ALLOWED_USERS": "123"})
        event = {
            "provider": "telegram",
            "sender": "123",
            "conversation": "123",
            "raw": {"voice": {"file_id": "voice-id"}},
        }
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / (hashlib.sha256(b"voice-id").hexdigest() + ".ogg")
            path.write_bytes(b"voice fixture")

            async def spawn(*args, **kwargs):
                output = Path(args[args.index("--output") + 1])
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                output.write_text("Please remember the blue kite.")
                return SimpleNamespace(returncode=0, wait=AsyncMock(return_value=0))

            with patch(
                "hermes_runtime.channel_agent.attachments.asyncio.create_subprocess_exec",
                side_effect=spawn,
            ) as launch:
                prepared = await telegram_media(event, transport, root)
                self.assertIn(
                    "Voice message: Please remember the blue kite.", prepared["text"]
                )
                self.assertEqual(prepared["attachments"][0]["kind"], "voice")
                self.assertEqual(path.with_suffix(".txt").stat().st_mode & 0o777, 0o600)
                await telegram_media(event, transport, root)
                self.assertEqual(launch.call_count, 1)

    async def test_failed_transcription_does_not_cache_a_partial_transcript(self):
        transport = SimpleNamespace(values={"TELEGRAM_ALLOWED_USERS": "123"})
        event = {
            "provider": "telegram",
            "sender": "123",
            "conversation": "123",
            "raw": {"voice": {"file_id": "voice-id"}},
        }
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / (hashlib.sha256(b"voice-id").hexdigest() + ".ogg")
            path.write_bytes(b"voice fixture")

            async def spawn(*args, **kwargs):
                Path(args[args.index("--output") + 1]).write_text("Partial result")
                return SimpleNamespace(returncode=1, wait=AsyncMock(return_value=1))

            with patch(
                "hermes_runtime.channel_agent.attachments.asyncio.create_subprocess_exec",
                side_effect=spawn,
            ):
                with self.assertRaises(RuntimeError):
                    await telegram_media(event, transport, root)
            self.assertFalse(path.with_suffix(".txt").exists())
            self.assertFalse(path.with_suffix(".transcribing").exists())

    async def test_photo_download_stays_private_and_native_input_contains_image(self):
        async def chunks(_size):
            yield b"example image bytes"

        response = SimpleNamespace(
            status=200, content=SimpleNamespace(iter_chunked=chunks)
        )
        context = AsyncMock()
        context.__aenter__.return_value = response
        transport = SimpleNamespace(
            values={"TELEGRAM_ALLOWED_USERS": "123", "TELEGRAM_BOT_TOKEN": "private"},
            request=AsyncMock(return_value={"file_path": "photos/file_1.jpg"}),
            session=SimpleNamespace(get=Mock(return_value=context)),
        )
        event = {
            "provider": "telegram",
            "sender": "123",
            "conversation": "123",
            "raw": {"photo": [{"file_id": "photo-id", "file_size": 19}]},
        }
        with tempfile.TemporaryDirectory() as root:
            prepared = await telegram_media(event, transport, Path(root) / "media")
            path = Path(prepared["attachments"][0]["path"])
            self.assertEqual(path.read_bytes(), b"example image bytes")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("private", str(prepared))
            await telegram_media(event, transport, Path(root) / "media")
            transport.request.assert_awaited_once()
            models = []
            agent = Codex(cwd=Path(root), approve=AsyncMock())

            async def request(method, params):
                if method == "thread/start":
                    return {"thread": {"id": "native-1"}, "model": "gpt-5.4"}
                self.assertEqual(
                    params["input"][1], {"type": "localImage", "path": str(path)}
                )
                agent.done.set_result({"status": "completed"})
                return {"turn": {"id": "turn-1"}}

            agent.request = request
            await agent.turn(
                "Describe this photo",
                native_id=None,
                instructions="",
                images=[path],
                on_model=models.append,
            )
            self.assertEqual(models, ["gpt-5.4"])

    async def test_foreign_owner_oversized_and_redirect_paths_never_download(self):
        transport = SimpleNamespace(
            values={"TELEGRAM_ALLOWED_USERS": "123", "TELEGRAM_BOT_TOKEN": "private"},
            request=AsyncMock(return_value={"file_path": "../escape.jpg"}),
            session=SimpleNamespace(get=Mock()),
        )
        event = {
            "provider": "telegram",
            "sender": "OTHER",
            "conversation": "123",
            "raw": {"photo": [{"file_id": "photo-id"}]},
        }
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                await telegram_media(event, transport, root)
            transport.request.assert_not_awaited()
            event["sender"] = "123"
            event["raw"]["photo"][0]["file_size"] = 21 * 1024 * 1024
            with self.assertRaises(ValueError):
                await telegram_media(event, transport, root)
            transport.request.assert_not_awaited()
            event["raw"]["photo"][0]["file_size"] = 100
            with self.assertRaises(ValueError):
                await telegram_media(event, transport, root)
            transport.session.get.assert_not_called()


class SlackAttachmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_slack_voice_uses_authenticated_download_and_transcription(self):
        from hermes_runtime.channel_agent.attachments import slack_media

        event = {
            "provider": "slack",
            "conversation": "D123",
            "sender": "U123",
            "raw": {"files": [{"id": "F12345"}]},
        }
        info = {
            "id": "F12345",
            "user": "U123",
            "mimetype": "audio/mp4",
            "size": 16,
            "url_private": "https://files.slack.com/files-pri/test/file.m4a",
        }
        transport = SimpleNamespace(
            values={"SLACK_ALLOWED_USERS": "U123", "SLACK_BOT_TOKEN": "private"},
            request=AsyncMock(return_value={"file": info}),
        )
        with tempfile.TemporaryDirectory() as root, patch(
            "hermes_runtime.channel_agent.attachments.download", new_callable=AsyncMock
        ) as download, patch(
            "hermes_runtime.channel_agent.attachments.transcribe",
            new_callable=AsyncMock,
        ) as transcribe:
            download.return_value = Path(root) / "voice.m4a"
            transcribe.return_value = "Tell me the answer briefly."
            result = await slack_media(event, transport, root)
            self.assertIn("Tell me the answer briefly.", result["text"])
            self.assertEqual(result["attachments"][0]["kind"], "voice")
            self.assertEqual(
                download.await_args.kwargs["headers"],
                {"Authorization": "Bearer private"},
            )
            self.assertNotIn("private", str(result["attachments"]))
            for changed in (
                {"user": "OTHER"},
                {"url_private": "https://files.slack.com.evil.test/a"},
                {"url_private": "http://files.slack.com/a"},
                {"size": 30 * 1024 * 1024},
            ):
                transport.request.return_value = {"file": {**info, **changed}}
                download.reset_mock()
                with self.assertRaises(ValueError):
                    await slack_media(event, transport, root)
                download.assert_not_awaited()

    async def test_failure_reaches_agent_without_private_error_details(self):
        from hermes_runtime.channel_agent.attachments import prepare_media

        event = {
            "provider": "slack",
            "sender": "U123",
            "text": "Please look",
            "raw": {"files": [{"id": "F12345"}]},
        }
        transport = SimpleNamespace(
            values={"SLACK_ALLOWED_USERS": "U123"},
            request=AsyncMock(side_effect=RuntimeError("PRIVATE_TOKEN_URL")),
        )
        with tempfile.TemporaryDirectory() as root:
            result = await prepare_media(event, transport, root)
        self.assertEqual(result["text"], "Please look")
        self.assertIn("media_error", result)
        self.assertNotIn("PRIVATE_TOKEN_URL", str(result))
