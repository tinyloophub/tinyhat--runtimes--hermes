"""Tests for the Tinyhat platform HTTP client."""

from __future__ import annotations

import base64
import json
import sys
import time
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hermes_runtime import client as client_module  # noqa: E402
from hermes_runtime.client import (  # noqa: E402
    CachedGoogleIdentityToken,
    PlatformClient,
    PlatformError,
)


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


def test_cached_google_identity_token_fetches_metadata_token_once() -> None:
    """Production auth is a Google VM identity token with a tiny local cache."""
    calls: list[tuple[str, int | float | None, dict[str, str]]] = []
    token = _jwt_with_exp(int(time.time()) + 600)

    def fake_urlopen(req: Any, timeout: int | float | None = None) -> _FakeResponse:
        calls.append((req.full_url, timeout, dict(req.header_items())))
        return _FakeResponse(token)

    original_urlopen = client_module.request.urlopen
    client_module.request.urlopen = fake_urlopen  # type: ignore[assignment]
    try:
        provider = CachedGoogleIdentityToken(
            audience="https://platform.example/",
            timeout_seconds=3,
        )

        assert provider() == token
        assert provider() == token
    finally:
        client_module.request.urlopen = original_urlopen  # type: ignore[assignment]

    assert len(calls) == 1
    url, timeout, headers = calls[0]
    assert timeout == 3
    assert "metadata.google.internal/computeMetadata/v1/instance" in url
    assert "audience=https%3A%2F%2Fplatform.example" in url
    assert "format=full" in url
    assert headers["Metadata-flavor"] == "Google"


def test_platform_client_wraps_read_timeout_as_platform_error() -> None:
    def fake_urlopen(req: Any, timeout: int | float | None = None) -> _FakeResponse:
        del req, timeout
        raise TimeoutError("The read operation timed out")

    original_urlopen = client_module.request.urlopen
    client_module.request.urlopen = fake_urlopen  # type: ignore[assignment]
    try:
        platform = PlatformClient(base_url="https://platform.example", token="token")
        try:
            asyncio_result = platform._request_json("POST", "/heartbeat", {})
        except PlatformError as exc:
            assert "POST /heartbeat timed out" in str(exc)
        else:
            raise AssertionError(f"expected PlatformError, got {asyncio_result!r}")
    finally:
        client_module.request.urlopen = original_urlopen  # type: ignore[assignment]


def test_metadata_token_provider_wraps_timeout_as_platform_error() -> None:
    def fake_urlopen(req: Any, timeout: int | float | None = None) -> _FakeResponse:
        del req, timeout
        raise TimeoutError("metadata read timed out")

    original_urlopen = client_module.request.urlopen
    client_module.request.urlopen = fake_urlopen  # type: ignore[assignment]
    try:
        provider = CachedGoogleIdentityToken(
            audience="https://platform.example/",
            timeout_seconds=3,
        )
        try:
            provider()
        except PlatformError as exc:
            assert "Google identity token" in str(exc)
            assert "timed out" in str(exc)
        else:
            raise AssertionError("expected PlatformError")
    finally:
        client_module.request.urlopen = original_urlopen  # type: ignore[assignment]
