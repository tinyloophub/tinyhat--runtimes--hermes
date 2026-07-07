"""Small async HTTP client for the Tinyhat platform."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any
from urllib import error, parse, request


class PlatformError(RuntimeError):
    """The Tinyhat platform returned an error or malformed response."""


class CachedGoogleIdentityToken:
    """Fetch and cache the VM identity token used for production platform calls."""

    def __init__(self, *, audience: str, timeout_seconds: int = 5) -> None:
        self.audience = audience.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._token: str | None = None
        self._expires_at: int = 0

    def __call__(self) -> str:
        now = int(time.time())
        if self._token and self._expires_at - 60 > now:
            return self._token
        token = self._fetch()
        self._token = token
        self._expires_at = _jwt_exp(token) or (now + 300)
        return token

    def _fetch(self) -> str:
        query = parse.urlencode({"audience": self.audience, "format": "full"})
        req = request.Request(
            "http://metadata.google.internal/computeMetadata/v1/instance/"
            f"service-accounts/default/identity?{query}",
            headers={"Metadata-Flavor": "Google"},
            method="GET",
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                return response.read().decode("utf-8").strip()
        except error.URLError as exc:
            raise PlatformError(
                f"failed to fetch Google identity token: {exc.reason}"
            ) from exc
        except TimeoutError as exc:
            raise PlatformError("failed to fetch Google identity token: timed out") from exc


def _jwt_exp(token: str) -> int | None:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (ValueError, json.JSONDecodeError):
        return None
    exp = decoded.get("exp") if isinstance(decoded, dict) else None
    return int(exp) if isinstance(exp, int) else None


class PlatformClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str | None = None,
        token_provider: Any | None = None,
        timeout_seconds: int = 20,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.token_provider = token_provider
        self.timeout_seconds = timeout_seconds

    async def get_json(self, path: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._request_json, "GET", path, None)

    async def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._request_json, "POST", path, payload)

    def _request_json(
        self, method: str, path: str, payload: dict[str, Any] | None
    ) -> dict[str, Any]:
        body = None
        token = self.token_provider() if self.token_provider else self.token
        if not token:
            raise PlatformError("missing platform authentication token")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
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
        except TimeoutError as exc:
            raise PlatformError(f"{method} {path} timed out") from exc
        if not raw:
            return {}
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise PlatformError(f"{method} {path} returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise PlatformError(f"{method} {path} returned non-object JSON")
        return decoded
