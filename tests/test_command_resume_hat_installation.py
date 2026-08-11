"""Tests for deterministic, value-blind Hat installation resumption."""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_runtime.commands import resume_hat_installation, run_command


def _plugin(root: Path, payload: object) -> Path:
    root.mkdir(parents=True)
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "hats.py").write_text(
        "import json\n"
        f"PAYLOAD = {payload!r}\n"
        "def hats(args):\n"
        "    if args != {'action': 'resume_installation'}:\n"
        "        raise AssertionError(args)\n"
        "    return json.dumps(PAYLOAD)\n",
        encoding="utf-8",
    )
    return root


def _run_with_plugin(payload: object) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = _plugin(Path(temp_dir) / "plugins" / "tinyhat", payload)
        with patch.object(resume_hat_installation, "plugin_dir", return_value=root):
            return asyncio.run(
                run_command(None, {"kind": "resume_hat_installation", "spec": {}})
            )


class ResumeHatInstallationCommandTests(unittest.TestCase):
    def test_registered_command_returns_only_minimal_started_status(self) -> None:
        result = _run_with_plugin(
            {
                "installation_id": "hti_12345678",
                "hat_handle": "acme/hats/research",
                "hat_title": "Research",
                "source": "new_agent",
                "status": "credentials_pending",
                "installation_started": True,
                "onboarding_message": "Preparing Research.",
                "repository": {
                    "path": "/private/hat-checkout",
                    "head_sha": "a" * 40,
                },
                "skills": {"count": 1, "installed_names": ["hat-research"]},
                "credential_transfer": {
                    "handoff_id": "sh_private_12345678",
                    "credential_count": 2,
                    "status": "pending",
                },
                "agent_instruction": "Send the progress update.",
                "operation_telemetry": {
                    "estimated_tool_input_tokens": 2,
                    "estimated_tool_output_tokens": 3,
                },
            }
        )

        self.assertEqual(
            result,
            {
                "schema": "tinyhat_hermes_resume_hat_installation_v1",
                "status": "credentials_pending",
                "outcome": "started",
                "installation_started": True,
            },
        )

    def test_missing_hat_is_a_quiet_success(self) -> None:
        result = _run_with_plugin(
            {
                "status": "none",
                "installation_started": False,
                "onboarding_message": None,
                "agent_instruction": "No user-facing warning.",
                "operation_telemetry": {},
            }
        )

        self.assertEqual(result["outcome"], "none")
        self.assertFalse(result["installation_started"])

    def test_active_replay_is_an_idempotent_success(self) -> None:
        result = _run_with_plugin(
            {
                "status": "active",
                "installation_started": False,
                "agent_instruction": "Already ready.",
                "operation_telemetry": {},
            }
        )

        self.assertEqual(result["outcome"], "already_active")
        self.assertFalse(result["installation_started"])

    def test_pending_state_is_reported_as_waiting(self) -> None:
        result = _run_with_plugin(
            {
                "status": "assignment_pending",
                "payment_required": False,
                "agent_instruction": "Wait for assignment.",
                "operation_telemetry": {},
            }
        )

        self.assertEqual(result["outcome"], "waiting")
        self.assertFalse(result["installation_started"])

    def test_missing_plugin_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(
                resume_hat_installation,
                "plugin_dir",
                return_value=Path(temp_dir) / "missing",
            ):
                with self.assertRaisesRegex(RuntimeError, "plugin is unavailable"):
                    asyncio.run(
                        run_command(
                            None,
                            {"kind": "resume_hat_installation", "spec": {}},
                        )
                    )

    def test_malformed_and_tool_error_results_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "plugins" / "tinyhat"
            root.mkdir(parents=True)
            (root / "__init__.py").write_text("", encoding="utf-8")
            (root / "hats.py").write_text(
                "def hats(args):\n    return 'not-json'\n",
                encoding="utf-8",
            )
            with patch.object(resume_hat_installation, "plugin_dir", return_value=root):
                with self.assertRaisesRegex(RuntimeError, "invalid result"):
                    asyncio.run(resume_hat_installation.run(None, {}))

        with self.assertRaisesRegex(RuntimeError, "could not resume installation"):
            _run_with_plugin(
                {
                    "schema": "tinyhat_tool_error_v1",
                    "status": "error",
                    "error": "platform_request_failed",
                    "message": "private diagnostic",
                }
            )

    def test_secret_shaped_plugin_output_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unsafe result"):
            _run_with_plugin(
                {
                    "status": "active",
                    "installation_started": False,
                    "access_token": "must-not-cross-runtime-boundary",
                }
            )

    def test_cancellation_waits_for_plugin_worker_before_propagating(self) -> None:
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def blocked_resume() -> dict[str, object]:
            started.set()
            release.wait(timeout=2)
            finished.set()
            return {
                "status": "active",
                "outcome": "already_active",
                "installation_started": False,
            }

        async def scenario() -> None:
            with patch.object(
                resume_hat_installation,
                "_resume_from_installed_plugin",
                blocked_resume,
            ):
                task = asyncio.create_task(resume_hat_installation.run(None, {}))
                self.assertTrue(await asyncio.to_thread(started.wait, 0.5))
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        asyncio.run(scenario())
        self.assertTrue(finished.is_set())


if __name__ == "__main__":
    unittest.main()
