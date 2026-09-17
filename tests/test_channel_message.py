"""Readable native conversations retain original local context and media."""

import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from hermes_runtime.channel_agent.message import format_message
from hermes_runtime.channel_agent.service import Service


class MessageTests(unittest.TestCase):
    def event(self, **changes):
        return {
            "provider": "telegram",
            "text": "How are you? 👋\nسلام",
            "event_id": "telegram:42",
            "message_id": "6",
            "raw": {"private_transport_field": "original"},
            **changes,
        }

    def test_text_first_unicode_and_original_record_reference(self):
        event = self.event()
        original = copy.deepcopy(event)
        text = format_message(event, Path("/private/inbox"))
        self.assertTrue(text.startswith(event["text"] + "\n\n"))
        self.assertIn("— Telegram · message 6", text)
        self.assertIn("/private/inbox/channels.sqlite3", text)
        self.assertIn('ID: "telegram:42"', text)
        self.assertNotIn("private_transport_field", text)
        self.assertEqual(event, original)

    def test_reply_and_thread_context(self):
        text = format_message(
            self.event(
                provider="slack",
                thread_id="123.456",
                reply_to="123.455",
                referenced_text="Earlier request\nwith two lines",
                clarification="Which document?",
            ),
            Path("/inbox"),
        )
        self.assertIn("Replying to:\n> Earlier request\n> with two lines", text)
        self.assertIn("Slack · message 6 · thread 123.456 · reply to 123.455", text)
        self.assertIn("Suggested clarification: Which document?", text)

    def test_image_without_caption_and_voice_transcript(self):
        for kind, content in (
            ("image", ""),
            ("voice", "Voice message: Please check this."),
        ):
            with self.subTest(kind=kind):
                text = format_message(
                    self.event(
                        text=content,
                        attachments=[
                            {"kind": kind, "path": "/private/media/original.dat"}
                        ],
                    ),
                    Path("/inbox"),
                )
                self.assertTrue(text.startswith(content or "[Attachment]"))
                self.assertIn(
                    f"- {kind.capitalize()}: /private/media/original.dat", text
                )

    def test_media_failure_visible_and_email_supported(self):
        text = format_message(
            self.event(provider="email", media_error="Cannot read audio."),
            Path("/inbox"),
        )
        self.assertIn("Attachment unavailable: Cannot read audio.", text)
        self.assertIn("— Email", text)

    def test_metadata_cannot_add_extra_lines(self):
        text = format_message(
            self.event(message_id="6\nextra", event_id='odd"\nkey'), Path("/inbox")
        )
        self.assertIn("· message 6 extra", text)
        self.assertIn('ID: "odd\\"\\nkey"', text)


class WorkerMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_preserves_structured_event_and_local_record(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "control").write_text("test-control")
            transport = SimpleNamespace(
                connected={}, values={}, stop_typing=AsyncMock()
            )
            with patch(
                "hermes_runtime.channel_agent.service.Transports",
                return_value=transport,
            ):
                service = Service(root, "codex")
            event = {
                "provider": "telegram",
                "text": "Describe this image.",
                "message_id": "6",
                "raw": {"photo": [{"file_id": "original-image"}]},
                "attachments": [{"kind": "image", "path": "/private/image.jpg"}],
            }
            service.state.ingest("telegram:42", event)
            task_id = service.state.route(
                "telegram:42", {"title": "Image", "task_id": None}, "codex"
            )
            service.run_native = AsyncMock(return_value=("native-session", ""))
            await service.work({"id": "telegram:42"}, task_id)
            prompt = service.run_native.call_args.args[0]
            delivered = service.run_native.call_args.kwargs["event"]
            self.assertTrue(prompt.startswith("Describe this image.\n"))
            self.assertNotIn("file_id", prompt)
            self.assertEqual(delivered["attachments"], event["attachments"])
            saved = json.loads(
                service.state.db.execute(
                    "SELECT payload FROM events WHERE id=?", ("telegram:42",)
                ).fetchone()[0]
            )
            self.assertEqual(saved["raw"], event["raw"])
            self.assertEqual(service.state.task(task_id)["status"], "completed")
            service.state.db.close()


if __name__ == "__main__":
    unittest.main()
