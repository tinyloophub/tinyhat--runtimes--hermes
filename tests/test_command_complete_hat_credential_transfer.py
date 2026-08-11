"""Tests for the value-blind automatic Hat credential runtime command."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_runtime.commands import complete_hat_credential_transfer


def _plugin(root: Path, *, extra_result: str = "") -> Path:
    root.mkdir(parents=True)
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "hat_transfer.py").write_text(
        "def complete_hat_credential_transfer(handoff_id, *, "
        "expected_hat_handle=None):\n"
        "    return {\n"
        "        'schema': 'tinyhat_hat_credential_transfer_result_v1',\n"
        "        'handoff_id': handoff_id,\n"
        "        'hat_handle': expected_hat_handle,\n"
        "        'credential_count': 2,\n"
        "        'submitted': True,\n"
        "        'authenticated_envelope': True,\n"
        "        'value_available': False,\n"
        f"        {extra_result}"
        "    }\n",
        encoding="utf-8",
    )
    return root


class CompleteHatCredentialTransferCommandTests(unittest.TestCase):
    def test_calls_installed_plugin_and_returns_safe_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = _plugin(Path(temp_dir) / "plugins" / "tinyhat")
            with patch.object(
                complete_hat_credential_transfer,
                "plugin_dir",
                return_value=root,
            ):
                result = asyncio.run(
                    complete_hat_credential_transfer.run(
                        None,
                        {
                            "kind": "complete_hat_credential_transfer",
                            "spec": {
                                "handoff_id": "sh_install_12345678",
                                "hat_handle": "acme/hats/research",
                            },
                        },
                    )
                )

        self.assertEqual(
            result,
            {
                "schema": "tinyhat_hermes_hat_credential_transfer_v1",
                "transfer": {
                    "schema": "tinyhat_hat_credential_transfer_result_v1",
                    "handoff_id": "sh_install_12345678",
                    "hat_handle": "acme/hats/research",
                    "credential_count": 2,
                    "submitted": True,
                    "authenticated_envelope": True,
                    "value_available": False,
                },
            },
        )

    def test_rejects_plugin_result_with_any_extra_field(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = _plugin(
                Path(temp_dir) / "plugins" / "tinyhat",
                extra_result="'access_token': 'must-not-escape',\n",
            )
            with patch.object(
                complete_hat_credential_transfer,
                "plugin_dir",
                return_value=root,
            ):
                with self.assertRaisesRegex(RuntimeError, "unsafe transfer result"):
                    asyncio.run(
                        complete_hat_credential_transfer.run(
                            None,
                            {
                                "spec": {
                                    "handoff_id": "sh_install_12345678",
                                    "hat_handle": "acme/hats/research",
                                }
                            },
                        )
                    )

    def test_rejects_invalid_identifiers_before_loading_plugin(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "valid handoff id"):
            asyncio.run(
                complete_hat_credential_transfer.run(
                    None,
                    {
                        "spec": {
                            "handoff_id": "not-a-handoff",
                            "hat_handle": "acme/hats/research",
                        }
                    },
                )
            )
        with self.assertRaisesRegex(RuntimeError, "canonical Hat handle"):
            asyncio.run(
                complete_hat_credential_transfer.run(
                    None,
                    {
                        "spec": {
                            "handoff_id": "sh_install_12345678",
                            "hat_handle": "research",
                        }
                    },
                )
            )


if __name__ == "__main__":
    unittest.main()
