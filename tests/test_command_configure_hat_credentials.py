"""Tests for Computer-initiated grouped Hat credential entry."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from hermes_runtime.commands import configure_hat_credentials

if TYPE_CHECKING:
    from pathlib import Path


def _plugin(root: Path, *, response: str) -> Path:
    root.mkdir(parents=True)
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "secret_handoff.py").write_text(
        f"def start_hat_credentials_handoff(handle):\n    return {response!r}\n",
        encoding="utf-8",
    )
    return root


def test_configure_hat_credentials_starts_installed_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _plugin(
        tmp_path / "plugins" / "tinyhat",
        response="I sent one secure Enter credentials button.",
    )
    monkeypatch.setattr(configure_hat_credentials, "plugin_dir", lambda _name: root)

    result = asyncio.run(
        configure_hat_credentials.run(
            None,
            {
                "kind": "configure_hat_credentials",
                "spec": {"hat_handle": "acme/hats/forecasting"},
            },
        )
    )

    assert result == {
        "schema": "tinyhat_hermes_configure_hat_credentials_v1",
        "started": True,
        "hat_handle": "acme/hats/forecasting",
        "message": "I sent one secure Enter credentials button.",
        "telegram_button_requested": True,
        "gateway_restart_requested": False,
    }


def test_configure_hat_credentials_surfaces_plugin_error_without_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _plugin(
        tmp_path / "plugins" / "tinyhat",
        response=(
            '{"status":"error","message":"Define at least one credential first."}'
        ),
    )
    monkeypatch.setattr(configure_hat_credentials, "plugin_dir", lambda _name: root)

    with pytest.raises(RuntimeError, match="Define at least one credential first"):
        asyncio.run(
            configure_hat_credentials.run(
                None,
                {
                    "kind": "configure_hat_credentials",
                    "spec": {"hat_handle": "acme/hats/forecasting"},
                },
            )
        )


def test_configure_hat_credentials_rejects_noncanonical_handle() -> None:
    with pytest.raises(RuntimeError, match="canonical Hat handle"):
        asyncio.run(
            configure_hat_credentials.run(
                None,
                {
                    "kind": "configure_hat_credentials",
                    "spec": {"hat_handle": "forecasting"},
                },
            )
        )
