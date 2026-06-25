"""Small async HTTP client for the Tinyhat platform."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib import error, request


class PlatformError(RuntimeError):
    """The Tinyhat platform returned an error or malformed response."""


class PlatformClient:
    def __init__(self, *, base_url: str, token: str, timeout_seconds: int = 20) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds

    async def get_json(self, path: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._request_json, "GET", path, None)

    async def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._request_json, "POST", path, payload)

    def _request_json(
        self, method: str, path: str, payload: dict[str, Any] | None
    ) -> dict[str, Any]:
        body = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "User-Agent": "tinyhat-hermes-runtime/0.0.1",
        }
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        req = request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise PlatformError(
                f"{method} {path} failed with HTTP {exc.code}: {detail}"
            ) from exc
        except error.URLError as exc:
            raise PlatformError(f"{method} {path} failed: {exc.reason}") from exc
        if not raw:
            return {}
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise PlatformError(f"{method} {path} returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise PlatformError(f"{method} {path} returned non-object JSON")
        return decoded
