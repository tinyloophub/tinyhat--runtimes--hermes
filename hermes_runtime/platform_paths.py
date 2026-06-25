"""Platform API path helpers for this Computer."""

from __future__ import annotations

from typing import Any
def computer_api_path(computer_id: str, suffix: str) -> str:
    """Return the local-dev runtime API path for this foundation release.

    The local Docker foundation authenticates with ``TINYHAT_LOCAL_DEV_TOKEN``.
    The platform resolves that token to the concrete Computer row, so the
    runtime deliberately does not put the database id into the URL.
    """
    _ = computer_id
    clean_suffix = suffix.lstrip("/")
    return f"/hapi/v1/computers/local-dev/{clean_suffix}"


def context_computer_api_path(ctx: Any, suffix: str) -> str:
    return computer_api_path(str(getattr(ctx, "computer_id", "local-dev")), suffix)
