"""Focused tests for the Tinyhat settings Telegram helper."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib import parse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import hermes_runtime.telegram_tinyhat_settings as settings  # noqa: E402


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


def test_open_settings_sends_web_app_button() -> None:
    requests: list[dict[str, object]] = []

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *_exc_info: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"ok": true, "result": {"message_id": 1}}'

    def fake_urlopen(req, timeout: int):  # noqa: ANN001
        requests.append({"url": req.full_url, "data": req.data, "timeout": timeout})
        return FakeResponse()

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        env_file = home / ".hermes" / ".env"
        env_file.parent.mkdir(parents=True)
        env_file.write_text(
            'TELEGRAM_BOT_TOKEN="123456:secret-token"\n'
            'TELEGRAM_HOME_CHANNEL="555111"\n'
            'TINYHAT_SETTINGS_MINIAPP_URL="https://tinyloop-wt5.ngrok.app/tinyhat/miniapp/agents/agt_test/settings"\n',
            encoding="utf-8",
        )
        old_env = os.environ.copy()
        os.environ.update({"HOME": str(home)})
        try:
            with patch("hermes_runtime.telegram_tinyhat_settings.request.urlopen", fake_urlopen):
                message = settings.open_settings()
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    assert message == "I sent the Tinyhat settings button."
    assert len(requests) == 1
    request_body = parse.parse_qs(requests[0]["data"].decode("utf-8"))
    reply_markup = json.loads(request_body["reply_markup"][0])
    assert requests[0]["url"] == "https://api.telegram.org/bot123456:secret-token/sendMessage"
    assert request_body["chat_id"] == ["555111"]
    assert request_body["text"] == ["Tinyhat settings"]
    assert reply_markup == {
        "inline_keyboard": [
            [
                {
                    "text": "Open settings",
                    "web_app": {
                        "url": "https://tinyloop-wt5.ngrok.app/tinyhat/miniapp/agents/agt_test/settings"
                    },
                }
            ]
        ]
    }
