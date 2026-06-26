"""Tests for the Tinyhat platform HTTP client."""

from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hermes_runtime import client as client_module  # noqa: E402
from hermes_runtime.client import CachedGoogleIdentityToken  # noqa: E402


class _FakeResponse:
    def __init__(self, body: str) -> None:
        self.body = body.encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def _jwt_with_exp(exp: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode("utf-8"))
        .rstrip(b"=")
        .decode()
    )
    return f"{header}.{payload}.signature"


def test_cached_google_identity_token_fetches_metadata_token_once(
    monkeypatch,
) -> None:
    """Production auth is a Google VM identity token with a tiny local cache."""
    calls: list[tuple[str, int | float | None, dict[str, str]]] = []
    token = _jwt_with_exp(int(time.time()) + 600)

    def fake_urlopen(req: Any, timeout: int | float | None = None) -> _FakeResponse:
        calls.append((req.full_url, timeout, dict(req.header_items())))
        return _FakeResponse(token)

    monkeypatch.setattr(client_module.request, "urlopen", fake_urlopen)

    provider = CachedGoogleIdentityToken(
        audience="https://platform.example/",
        timeout_seconds=3,
    )

    assert provider() == token
    assert provider() == token
    assert len(calls) == 1
    url, timeout, headers = calls[0]
    assert timeout == 3
    assert "metadata.google.internal/computeMetadata/v1/instance" in url
    assert "audience=https%3A%2F%2Fplatform.example" in url
    assert "format=full" in url
    assert headers["Metadata-flavor"] == "Google"
