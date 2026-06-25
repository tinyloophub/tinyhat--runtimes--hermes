"""Platform API path helpers for this Computer."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote


def computer_api_path(computer_id: str, suffix: str) -> str:
    clean_id = (computer_id or "local-dev").strip() or "local-dev"
    clean_suffix = suffix.lstrip("/")
    return f"/hapi/v1/computers/{quote(clean_id, safe='')}/{clean_suffix}"


def context_computer_api_path(ctx: Any, suffix: str) -> str:
    return computer_api_path(str(getattr(ctx, "computer_id", "local-dev")), suffix)
