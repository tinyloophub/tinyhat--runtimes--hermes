"""Build a Computer-authenticated platform client outside the heartbeat loop."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from hermes_runtime.client import CachedGoogleIdentityToken, PlatformClient
from hermes_runtime.platform_paths import computer_api_path
from hermes_runtime.runtime_env import env_file_candidates, read_env_values

RUNTIME_ENV_FILES = (
    Path("/opt/tinyhat-hermes-runtime/env/runtime.env"),
    Path("/etc/tinyhat/hermes-runtime.env"),
)
_PLATFORM_ENV_NAMES = (
    "TINYHAT_PLATFORM_URL",
    "TINYHAT_LOCAL_DEV_TOKEN",
    "TINYHAT_COMPUTER_TOKEN_AUDIENCE",
)


@dataclass(frozen=True)
class StandalonePlatformContext:
    """The platform client and route mode for one local helper invocation."""

    client: PlatformClient
    auth_mode: str

    def computer_path(self, suffix: str) -> str:
        clean_suffix = suffix.lstrip("/")
        if self.auth_mode == "gcloud":
            return f"/hapi/v1/computers/me/{clean_suffix}"
        return computer_api_path("local-dev", clean_suffix)


def _platform_values() -> dict[str, str]:
    candidates = [*RUNTIME_ENV_FILES, *env_file_candidates()]
    values = read_env_values(candidates, names=_PLATFORM_ENV_NAMES)
    for name in _PLATFORM_ENV_NAMES:
        if name in os.environ:
            values[name] = os.environ[name]
    return values


def build_standalone_platform_context() -> StandalonePlatformContext:
    """Use the same Computer identity boundary as the resident runtime."""

    values = _platform_values()
    base_url = values.get("TINYHAT_PLATFORM_URL", "").strip()
    if not base_url:
        raise RuntimeError("TINYHAT_PLATFORM_URL is not configured.")
    local_token = values.get("TINYHAT_LOCAL_DEV_TOKEN", "").strip()
    if local_token:
        return StandalonePlatformContext(
            client=PlatformClient(base_url=base_url, token=local_token),
            auth_mode="local_dev",
        )
    audience = (
        values.get("TINYHAT_COMPUTER_TOKEN_AUDIENCE", "").strip() or base_url
    )
    return StandalonePlatformContext(
        client=PlatformClient(
            base_url=base_url,
            token_provider=CachedGoogleIdentityToken(audience=audience),
        ),
        auth_mode="gcloud",
    )


__all__ = ["StandalonePlatformContext", "build_standalone_platform_context"]
