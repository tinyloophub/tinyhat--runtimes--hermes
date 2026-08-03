"""Tests for the value-blind Google Workspace reviewer OAuth command."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hermes_runtime.commands import COMMAND_MODULES, run_command  # noqa: E402
from hermes_runtime.commands.start_google_workspace_reviewer_oauth import (  # noqa: E402
    COMMAND_SCHEMA,
    PLUGIN_FAILURE_MESSAGE,
)
from hermes_runtime.main import _heartbeat_metrics, _run_one_command  # noqa: E402

VALID_REQUEST_ID = "gwrq_0123456789abcdefghijklmnopqrstuvwxyzABCDEFG"


def _write_fake_plugin(home: Path, source: str) -> Path:
    plugin = home / "plugins" / "tinyhat"
    plugin.mkdir(parents=True)
    (plugin / "__init__.py").write_text("", encoding="utf-8")
    (plugin / "google_workspace.py").write_text(source, encoding="utf-8")
    return plugin


class ReviewerOAuthCommandTests(TestCase):
    def test_command_is_explicitly_whitelisted(self) -> None:
        self.assertEqual(
            COMMAND_MODULES["start_google_workspace_reviewer_oauth"],
            "hermes_runtime.commands.start_google_workspace_reviewer_oauth",
        )

    def test_command_invokes_only_documented_plugin_function_and_returns_fixed_result(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "hermes-home"
            capture = Path(tmp) / "captured-request-id"
            _write_fake_plugin(
                home,
                """
import os
from pathlib import Path

def start_google_workspace_reviewer_oauth(reviewer_request_id):
    Path(os.environ["TINYHAT_TEST_REVIEWER_CAPTURE"]).write_text(
        reviewer_request_id,
        encoding="utf-8",
    )
    return {
        "schema": "tinyhat_google_workspace_reviewer_oauth_start_v1",
        "action": "reviewer_oauth_start",
        "status": "started",
    }
""".lstrip(),
            )
            with patch.dict(
                os.environ,
                {
                    "TINYHAT_HERMES_HOME": str(home),
                    "TINYHAT_TEST_REVIEWER_CAPTURE": str(capture),
                },
                clear=True,
            ):
                result = asyncio.run(
                    run_command(
                        SimpleNamespace(),
                        {
                            "kind": "start_google_workspace_reviewer_oauth",
                            "spec": {"reviewer_request_id": VALID_REQUEST_ID},
                        },
                    )
                )
            captured_request_id = capture.read_text(encoding="utf-8")

        self.assertEqual(captured_request_id, VALID_REQUEST_ID)
        self.assertEqual(
            result,
            {
                "schema": COMMAND_SCHEMA,
                "action": "reviewer_oauth_start",
                "status": "started",
            },
        )

    def test_installed_plugin_can_use_sibling_relative_imports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "hermes-home"
            plugin = _write_fake_plugin(
                home,
                """
from .reviewer_contract import START_RESULT

def start_google_workspace_reviewer_oauth(reviewer_request_id):
    return dict(START_RESULT)
""".lstrip(),
            )
            (plugin / "reviewer_contract.py").write_text(
                """
START_RESULT = {
    "schema": "tinyhat_google_workspace_reviewer_oauth_start_v1",
    "action": "reviewer_oauth_start",
    "status": "started",
}
""".lstrip(),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"TINYHAT_HERMES_HOME": str(home)},
                clear=True,
            ):
                result = asyncio.run(
                    run_command(
                        SimpleNamespace(),
                        {
                            "kind": "start_google_workspace_reviewer_oauth",
                            "spec": {"reviewer_request_id": VALID_REQUEST_ID},
                        },
                    )
                )

        self.assertEqual(
            result,
            {
                "schema": COMMAND_SCHEMA,
                "action": "reviewer_oauth_start",
                "status": "started",
            },
        )

    def test_command_rejects_missing_or_extra_spec_fields_before_plugin_load(
        self,
    ) -> None:
        commands = (
            {"kind": "start_google_workspace_reviewer_oauth"},
            {
                "kind": "start_google_workspace_reviewer_oauth",
                "spec": {},
            },
            {
                "kind": "start_google_workspace_reviewer_oauth",
                "spec": {
                    "reviewer_request_id": VALID_REQUEST_ID,
                    "authorization_url": "https://example.invalid",
                },
            },
        )
        for command in commands:
            with self.subTest(command=command), self.assertRaisesRegex(
                ValueError,
                "must contain exactly reviewer_request_id",
            ):
                asyncio.run(run_command(SimpleNamespace(), command))

    def test_command_rejects_non_opaque_or_out_of_bounds_request_ids(self) -> None:
        invalid_values = (
            None,
            123,
            "gwrq_short",
            f"gwrq_{'a' * 42}",
            f"gwrq_{'a' * 44}",
            f" {VALID_REQUEST_ID}",
            f"{VALID_REQUEST_ID} ",
            "gwrq_../../etc/passwd____________________________",
            "gwrq_0123456789abcdefghijklmnopqrstuvwxyzABCDEFé",
        )
        for value in invalid_values:
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError,
                "valid opaque reviewer_request_id",
            ):
                asyncio.run(
                    run_command(
                        SimpleNamespace(),
                        {
                            "kind": "start_google_workspace_reviewer_oauth",
                            "spec": {"reviewer_request_id": value},
                        },
                    )
                )

    def test_plugin_failure_does_not_expose_exception_values(self) -> None:
        sensitive = (
            "https://accounts.google.com/o/oauth2/v2/auth?code=secret-code"
        )
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "hermes-home"
            _write_fake_plugin(
                home,
                f"""
def start_google_workspace_reviewer_oauth(reviewer_request_id):
    raise RuntimeError({sensitive!r})
""".lstrip(),
            )
            with (
                patch.dict(
                    os.environ,
                    {"TINYHAT_HERMES_HOME": str(home)},
                    clear=True,
                ),
                self.assertRaisesRegex(RuntimeError, PLUGIN_FAILURE_MESSAGE) as raised,
            ):
                asyncio.run(
                    run_command(
                        SimpleNamespace(),
                        {
                            "kind": "start_google_workspace_reviewer_oauth",
                            "spec": {"reviewer_request_id": VALID_REQUEST_ID},
                        },
                    )
                )

        self.assertNotIn(sensitive, str(raised.exception))
        self.assertNotIn("secret-code", str(raised.exception))

    def test_plugin_result_with_oauth_values_is_rejected_without_echoing_them(
        self,
    ) -> None:
        sensitive = "owner-token-and-authorization-url"
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "hermes-home"
            _write_fake_plugin(
                home,
                f"""
def start_google_workspace_reviewer_oauth(reviewer_request_id):
    return {{
        "schema": "tinyhat_google_workspace_reviewer_oauth_start_v1",
        "action": "reviewer_oauth_start",
        "status": "started",
        "owner_token": {sensitive!r},
    }}
""".lstrip(),
            )
            with (
                patch.dict(
                    os.environ,
                    {"TINYHAT_HERMES_HOME": str(home)},
                    clear=True,
                ),
                self.assertRaisesRegex(RuntimeError, PLUGIN_FAILURE_MESSAGE) as raised,
            ):
                asyncio.run(
                    run_command(
                        SimpleNamespace(),
                        {
                            "kind": "start_google_workspace_reviewer_oauth",
                            "spec": {"reviewer_request_id": VALID_REQUEST_ID},
                        },
                    )
                )

        self.assertNotIn(sensitive, str(raised.exception))

    def test_command_settlement_redacts_plugin_failure_from_report_and_ledger(
        self,
    ) -> None:
        sensitive_values = (
            "https://accounts.google.com/o/oauth2/v2/auth?state=private-state",
            "reviewer-code-should-not-escape",
            "oauth-token-should-not-escape",
        )

        class RecordingPlatform:
            def __init__(self) -> None:
                self.posts: list[tuple[str, dict[str, object]]] = []

            async def post_json(
                self,
                path: str,
                payload: dict[str, object],
            ) -> dict[str, bool]:
                self.posts.append((path, payload))
                return {"ok": True}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "hermes-home"
            state_dir = root / "runtime-state"
            _write_fake_plugin(
                home,
                f"""
def start_google_workspace_reviewer_oauth(reviewer_request_id):
    raise RuntimeError({(' '.join(sensitive_values))!r})
""".lstrip(),
            )
            platform = RecordingPlatform()
            ctx = SimpleNamespace(
                platform=platform,
                platform_auth="local_dev",
                computer_id="local-dev",
                state_dir=state_dir,
            )
            command = {
                "command_id": "cmd-reviewer-oauth",
                "idempotency_key": "idem-reviewer-oauth",
                "kind": "start_google_workspace_reviewer_oauth",
                "spec": {"reviewer_request_id": VALID_REQUEST_ID},
            }
            with patch.dict(
                os.environ,
                {"TINYHAT_HERMES_HOME": str(home)},
                clear=True,
            ):
                asyncio.run(_run_one_command(ctx, command))

            self.assertEqual(len(platform.posts), 1)
            reported = platform.posts[0][1]["result"]
            reported_json = json.dumps(reported, sort_keys=True)
            ledger_lines = (state_dir / "commands" / "ledger.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual(len(ledger_lines), 1)
            ledger_entry = json.loads(ledger_lines[0])
            ledger_json = json.dumps(ledger_entry, sort_keys=True)

        self.assertEqual(reported["status"], "failed")
        self.assertEqual(
            reported["result"]["message"],
            PLUGIN_FAILURE_MESSAGE,
        )
        self.assertNotIn(VALID_REQUEST_ID, reported_json)
        self.assertEqual(
            ledger_entry["spec"],
            {"reviewer_request_id": VALID_REQUEST_ID},
        )
        self.assertEqual(ledger_entry["result"]["message"], PLUGIN_FAILURE_MESSAGE)
        for sensitive in sensitive_values:
            self.assertNotIn(sensitive, reported_json)
            self.assertNotIn(sensitive, ledger_json)

    def test_heartbeat_advertises_reviewer_oauth_start_capability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = SimpleNamespace(
                state_dir=Path(tmp),
                started_at=0.0,
                current_version=lambda: "v0.0.49",
                current_commit_sha=lambda: None,
                staged_version=lambda: None,
            )

            metrics = _heartbeat_metrics(ctx, status="running")

        self.assertIs(
            metrics["hermes_runtime"]["capabilities"][
                "google_workspace_reviewer_oauth_start"
            ],
            True,
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
