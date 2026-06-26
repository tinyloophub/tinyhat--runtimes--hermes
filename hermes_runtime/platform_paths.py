"""Platform API path helpers for this Computer."""

from __future__ import annotations

from typing import Any


def computer_api_path(computer_id: str, suffix: str) -> str:
    """Return the platform API path for this foundation release.

    Local Docker authenticates with ``TINYHAT_LOCAL_DEV_TOKEN`` and keeps using
    the scoped local-dev paths. GCloud Computers authenticate with a Google
    identity token and use the existing ``/computers/me`` API surface.
    """
    _ = computer_id
    clean_suffix = suffix.lstrip("/")
    return f"/hapi/v1/computers/local-dev/{clean_suffix}"


def context_computer_api_path(ctx: Any, suffix: str) -> str:
    clean_suffix = suffix.lstrip("/")
    if getattr(ctx, "platform_auth", "local_dev") == "gcloud":
        return f"/hapi/v1/computers/me/{clean_suffix}"
    return computer_api_path(str(getattr(ctx, "computer_id", "local-dev")), suffix)
