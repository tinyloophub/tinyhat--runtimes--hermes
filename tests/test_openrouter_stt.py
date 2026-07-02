"""Focused tests for the OpenRouter STT command bridge."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hermes_runtime.openrouter_stt as openrouter_stt  # noqa: E402


def load_tests(
    loader: unittest.TestLoader,
    tests: unittest.TestSuite,
    pattern: str | None,
) -> unittest.TestSuite:
    del loader, tests, pattern
    suite = unittest.TestSuite()
    module = sys.modules[__name__]
    for name, value in sorted(vars(module).items()):
        if name.startswith("test_") and callable(value):
            suite.addTest(unittest.FunctionTestCase(value))
    return suite


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_openrouter_stt_posts_base64_audio_and_writes_transcript() -> None:
    seen: dict[str, object] = {}

    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        seen["url"] = getattr(req, "full_url")
        seen["timeout"] = timeout
        seen["headers"] = dict(getattr(req, "header_items")())
        seen["payload"] = json.loads(getattr(req, "data").decode("utf-8"))
        return FakeResponse({"text": "Hello from OpenRouter"})

    with tempfile.TemporaryDirectory() as tmp:
        audio = Path(tmp) / "voice.ogg"
        output = Path(tmp) / "transcript.txt"
        audio.write_bytes(b"fake-audio")
        old_env = os.environ.copy()
        os.environ.update(
            {
                "OPENROUTER_API_KEY": "sk-or-v1-test",
                "OPENROUTER_BASE_URL": "https://openrouter.ai/api/v1/",
            }
        )
        stdout = StringIO()
        try:
            with (
                patch("hermes_runtime.openrouter_stt.request.urlopen", fake_urlopen),
                redirect_stdout(stdout),
            ):
                code = openrouter_stt.main(
                    [
                        "--input",
                        str(audio),
                        "--output",
                        str(output),
                        "--format",
                        "txt",
                        "--language",
                        "auto",
                        "--model",
                        "openai/whisper-large-v3-turbo",
                    ]
                )
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        output_text = output.read_text(encoding="utf-8")

    assert code == 0
    assert output_text == "Hello from OpenRouter\n"
    assert stdout.getvalue().strip() == "Hello from OpenRouter"
    assert seen["url"] == "https://openrouter.ai/api/v1/audio/transcriptions"
    assert seen["timeout"] == 120.0
    assert seen["headers"]["Authorization"] == "Bearer sk-or-v1-test"
    payload = seen["payload"]
    assert isinstance(payload, dict)
    assert payload["model"] == "openai/whisper-large-v3-turbo"
    assert payload["input_audio"] == {
        "data": "ZmFrZS1hdWRpbw==",
        "format": "ogg",
    }
    assert "language" not in payload


def test_openrouter_stt_reports_missing_key_without_secret_output() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        audio = Path(tmp) / "voice.wav"
        audio.write_bytes(b"fake-audio")
        old_env = os.environ.copy()
        os.environ.pop("OPENROUTER_API_KEY", None)
        stderr = StringIO()
        try:
            with redirect_stderr(stderr):
                code = openrouter_stt.main(["--input", str(audio)])
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    assert code == 1
    assert "OPENROUTER_API_KEY is not available" in stderr.getvalue()
    assert "sk-" not in stderr.getvalue()
