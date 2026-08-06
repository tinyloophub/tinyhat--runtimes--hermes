"""Tests for Computer-initiated grouped Hat credential entry."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_runtime.commands import configure_hat_credentials


def _plugin(root: Path, *, response: str) -> Path:
    root.mkdir(parents=True)
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "secret_handoff.py").write_text(
        f"def start_hat_credentials_handoff(handle):\n    return {response!r}\n",
        encoding="utf-8",
    )
    return root


class ConfigureHatCredentialsCommandTest(unittest.TestCase):
    def test_starts_installed_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = _plugin(
                Path(temp_dir) / "plugins" / "tinyhat",
                response="I sent one secure Enter credentials button.",
            )
            with patch.object(
                configure_hat_credentials,
                "plugin_dir",
                return_value=root,
            ):
                result = asyncio.run(
                    configure_hat_credentials.run(
                        None,
                        {
                            "kind": "configure_hat_credentials",
                            "spec": {"hat_handle": "acme/hats/forecasting"},
                        },
                    )
                )

        self.assertEqual(
            result,
            {
                "schema": "tinyhat_hermes_configure_hat_credentials_v1",
                "started": True,
                "hat_handle": "acme/hats/forecasting",
                "message": "I sent one secure Enter credentials button.",
                "telegram_button_requested": True,
                "gateway_restart_requested": False,
            },
        )

    def test_surfaces_plugin_error_without_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = _plugin(
                Path(temp_dir) / "plugins" / "tinyhat",
                response=(
                    '{"status":"error","message":'
                    '"Define at least one credential first."}'
                ),
            )
            with patch.object(
                configure_hat_credentials,
                "plugin_dir",
                return_value=root,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "Define at least one credential first",
                ):
                    asyncio.run(
                        configure_hat_credentials.run(
                            None,
                            {
                                "kind": "configure_hat_credentials",
                                "spec": {"hat_handle": "acme/hats/forecasting"},
                            },
                        )
                    )

    def test_rejects_noncanonical_handle(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "canonical Hat handle"):
            asyncio.run(
                configure_hat_credentials.run(
                    None,
                    {
                        "kind": "configure_hat_credentials",
                        "spec": {"hat_handle": "forecasting"},
                    },
                )
            )
