"""Email setup through public config without overwriting owner choices."""

import asyncio
import copy
import json
import io
import importlib.util
import os
import tempfile
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase, skipUnless
from unittest.mock import AsyncMock, patch
from urllib.error import HTTPError

from hermes_runtime import email_onboarding
from hermes_runtime.client import PlatformError
from hermes_runtime.commands import apply_config, configure_telegram
from hermes_runtime.main import _heartbeat_metrics

VALUES = {
    "TINYHAT_EMAIL_CHANNEL_ENABLED": "1",
    "TINYHAT_EMAIL_OWNER": "owner@example.test",
    "TINYHAT_MAILBOX_ADDRESS": "agent@example.test",
    "TINYHAT_MAILBOX_USERNAME": "agent@example.test",
    "TINYHAT_MAILBOX_PASSWORD": "fixture",
    "TINYHAT_MAILBOX_JMAP_URL": "https://mail.example.test/.well-known/jmap",
    "OPENROUTER_API_KEY": "fixture",
    "TINYHAT_EMAIL_INITIAL_MODEL": "example/small",
}


class ConfigTests(TestCase):
    def test_runtime_import_does_not_require_hermes_yaml_dependency(self):
        probe = """import importlib.abc,sys
class BlockYaml(importlib.abc.MetaPathFinder):
 def find_spec(self, fullname, path=None, target=None):
  if fullname == 'yaml' or fullname.startswith('yaml.'):
   raise ImportError('YAML deliberately absent')
sys.meta_path.insert(0,BlockYaml())
import importlib,pkgutil,hermes_runtime
for module in pkgutil.walk_packages(hermes_runtime.__path__, 'hermes_runtime.'):
 importlib.import_module(module.name)
assert 'yaml' not in sys.modules
"""
        result = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @skipUnless(
        importlib.util.find_spec("yaml"),
        "YAML integration runs in the Hermes environment",
    )
    def test_actual_yaml_file_preserves_model_and_writes_private_idempotent_config(
        self,
    ):
        import yaml

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(
                "model:\n  provider: openai-codex\n  default: owner/model\n"
            )
            payload = json.dumps({"home": tmp, "model": "initial/model"})
            for changed in (True, False):
                output = io.StringIO()
                with (
                    patch.object(email_onboarding.sys, "stdin", io.StringIO(payload)),
                    patch.object(email_onboarding.sys, "stdout", output),
                ):
                    email_onboarding._configure_file()
                self.assertEqual(json.loads(output.getvalue()), {"changed": changed})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                yaml.safe_load(path.read_text())["model"]["default"], "owner/model"
            )

    @skipUnless(
        importlib.util.find_spec("yaml"),
        "YAML integration runs in the Hermes environment",
    )
    def test_invalid_yaml_reports_class_without_file_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            secret_text = "private-fixture: [broken"
            path.write_text(secret_text)
            output = io.StringIO()
            with (
                patch.object(
                    email_onboarding.sys,
                    "stdin",
                    io.StringIO(json.dumps({"home": tmp, "model": "example/model"})),
                ),
                patch.object(email_onboarding.sys, "stdout", output),
                self.assertRaises(SystemExit),
            ):
                email_onboarding._configure_file()
            self.assertEqual(
                json.loads(output.getvalue()), {"error_type": "ParserError"}
            )
            self.assertEqual(path.read_text(), secret_text)

    def test_initial_config_is_idempotent(self):
        config = email_onboarding.merge_config(
            {}, VALUES["TINYHAT_EMAIL_INITIAL_MODEL"]
        )
        self.assertEqual(config["plugins"]["enabled"], ["tinyhat"])
        self.assertFalse(config["display"]["platforms"]["tinyhat_email"]["streaming"])
        self.assertEqual(config["model"]["provider"], "openrouter")
        self.assertEqual(config["model"]["max_tokens"], 4096)
        again = email_onboarding.merge_config(
            copy.deepcopy(config), VALUES["TINYHAT_EMAIL_INITIAL_MODEL"]
        )
        self.assertEqual(config, again)
        self.assertNotIn("fixture", json.dumps(config))

    def test_existing_model_and_other_channels_survive(self):
        existing = {
            "model": {"provider": "openai-codex", "default": "owner/model"},
            "plugins": {"enabled": ["other"]},
            "platforms": {"telegram": {"enabled": True}},
        }
        result = email_onboarding.merge_config(
            copy.deepcopy(existing), VALUES["TINYHAT_EMAIL_INITIAL_MODEL"]
        )
        self.assertEqual(result["model"], existing["model"])
        self.assertEqual(
            result["platforms"]["telegram"], existing["platforms"]["telegram"]
        )
        self.assertEqual(result["plugins"]["enabled"], ["other", "tinyhat"])

    def test_yaml_runs_in_hermes_python_without_passing_credentials(self):
        result = SimpleNamespace(returncode=0, stdout='{"changed": true}')
        with (
            patch.object(
                email_onboarding, "hermes_python", return_value=Path("/hermes/python")
            ),
            patch.object(
                email_onboarding.subprocess, "run", return_value=result
            ) as run,
        ):
            self.assertTrue(email_onboarding.configure(Path("/hermes/home"), VALUES))
        args, kwargs = run.call_args
        self.assertEqual(args[0][0], "/hermes/python")
        self.assertEqual(
            json.loads(kwargs["input"]),
            {"home": "/hermes/home", "model": "example/small"},
        )
        self.assertNotIn("fixture", str(args) + kwargs["input"])
        self.assertEqual(kwargs["timeout"], 30)
        self.assertFalse(kwargs["env"]["PYTHONPATH"].endswith(os.pathsep))

    def test_username_and_initial_key_are_required_before_starting_email(self):
        for key in ("TINYHAT_MAILBOX_USERNAME", "OPENROUTER_API_KEY"):
            values = {k: v for k, v in VALUES.items() if k != key}
            with (
                self.assertRaisesRegex(ValueError, "incomplete"),
                patch.object(email_onboarding.subprocess, "run") as run,
            ):
                email_onboarding.configure(Path("/unused"), values)
            run.assert_not_called()


class ReconcileTests(IsolatedAsyncioTestCase):
    async def test_reachable_channel_clears_old_status_failure_and_restores_setup_cadence(
        self,
    ):
        ctx = SimpleNamespace(
            platform=SimpleNamespace(
                get_json=AsyncMock(return_value={"status": "pending"})
            ),
            email_setup_failure={"stage": "channel status", "http_status": 404},
            email_failure_count=6,
        )
        await email_onboarding.reconcile(ctx)
        self.assertIsNone(ctx.email_setup_failure)
        self.assertEqual(ctx.email_failure_count, 0)
        self.assertEqual(email_onboarding.retry_interval(ctx), 60)
        ctx.email_setup_failure = {"stage": "gateway restart"}
        ctx.email_failure_count = 0
        self.assertEqual(email_onboarding.retry_interval(ctx), 60)

    async def test_failed_gateway_retries_are_capped_and_timed_out(self):
        async def get(path):
            return (
                {"status": "ready"} if path.endswith("/email") else {"secrets": VALUES}
            )

        ctx = SimpleNamespace(
            platform=SimpleNamespace(get_json=get), platform_auth="gcloud"
        )
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(email_onboarding, "configure", return_value=False),
            patch.object(
                email_onboarding, "find_hermes_binary", return_value=Path("/hermes")
            ),
            patch.object(
                apply_config, "_env_file_candidates", return_value=[Path(tmp) / "env"]
            ),
            patch.object(apply_config, "load_env_files_into_process"),
            patch.object(apply_config, "sync_terminal_env_passthrough"),
            patch.object(
                configure_telegram,
                "_run_gateway",
                new_callable=AsyncMock,
                return_value={"healthy": False},
            ) as restart,
            self.assertLogs(email_onboarding.logger),
        ):
            for _ in range(8):
                await email_onboarding.reconcile(ctx)
            self.assertEqual(restart.await_count, 5)
            self.assertEqual(email_onboarding.retry_interval(ctx), 1800)
            ctx.email_gateway_attempts = 0

            async def slow(_):
                await asyncio.sleep(10)

            restart.side_effect = slow
            with patch.object(email_onboarding, "GATEWAY_TIMEOUT_SECONDS", 0.001):
                await email_onboarding.reconcile(ctx)
            self.assertEqual(ctx.email_setup_failure["error_type"], "TimeoutError")

    async def test_unsupported_local_identity_defers_quietly(self):
        error = PlatformError("private response")
        error.__cause__ = HTTPError(
            "https://example.test/email", 401, "unauthorized", {}, None
        )
        ctx = SimpleNamespace(
            platform=SimpleNamespace(get_json=AsyncMock(side_effect=error))
        )
        with self.assertLogs(email_onboarding.logger, level="DEBUG") as messages:
            await email_onboarding.reconcile(ctx)
        self.assertTrue(all(row.levelname == "DEBUG" for row in messages.records))

    def test_setup_failure_is_visible_in_heartbeat_without_secret_values(self):
        failure = {
            "stage": "apply_config",
            "error_type": "ValueError",
            "http_status": None,
        }
        ctx = SimpleNamespace(
            started_at=0,
            current_version=lambda: "fixture",
            staged_version=lambda: None,
            email_setup_failure=failure,
            email_gateway_ready=False,
        )
        metrics = _heartbeat_metrics(ctx, status="running")
        self.assertEqual(
            metrics["hermes_runtime"]["email_onboarding"]["failure"], failure
        )

    async def test_restarts_for_changed_credentials_even_with_unchanged_yaml(self):
        values = dict(VALUES)

        async def get(path):
            return (
                {"status": "ready"} if path.endswith("/email") else {"secrets": values}
            )

        ctx = SimpleNamespace(
            platform=SimpleNamespace(get_json=get), platform_auth="gcloud"
        )
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(email_onboarding, "configure", return_value=False),
            patch.object(
                email_onboarding, "find_hermes_binary", return_value=Path("/hermes")
            ),
            patch.object(
                apply_config, "_env_file_candidates", return_value=[Path(tmp) / "env"]
            ),
            patch.object(apply_config, "load_env_files_into_process"),
            patch.object(apply_config, "sync_terminal_env_passthrough"),
            patch.object(
                configure_telegram,
                "_run_gateway",
                new_callable=AsyncMock,
                return_value={"healthy": True},
            ) as restart,
        ):
            await email_onboarding.reconcile(ctx)
            self.assertTrue(ctx.email_gateway_ready)
            await email_onboarding.reconcile(ctx)
            self.assertEqual(restart.await_count, 1)
            values["TINYHAT_MAILBOX_USERNAME"] = "renamed@example.test"
            await email_onboarding.reconcile(ctx)
            self.assertEqual(restart.await_count, 2)

    async def test_failure_does_not_latch_success_and_can_retry(self):
        get = AsyncMock(
            side_effect=[
                {"status": "ready"},
                {"secrets": VALUES},
                {"status": "pending"},
            ]
        )
        ctx = SimpleNamespace(
            platform=SimpleNamespace(get_json=get), platform_auth="gcloud"
        )
        with (
            patch.object(
                email_onboarding, "configure", side_effect=ValueError("incomplete")
            ),
            self.assertLogs(email_onboarding.logger, level="WARNING"),
        ):
            await email_onboarding.reconcile(ctx)
        self.assertFalse(ctx.email_gateway_ready)
        self.assertFalse(hasattr(ctx, "email_config_fingerprint"))
        self.assertEqual(ctx.email_setup_failure["stage"], "Hermes email configuration")
        await email_onboarding.reconcile(ctx)
        self.assertEqual(get.await_count, 3)

    async def test_older_platform_is_quiet_and_diagnostics_exclude_response_body(self):
        error = PlatformError("private response body")
        error.__cause__ = HTTPError(
            "https://example.test/email", 404, "missing", {}, None
        )
        ctx = SimpleNamespace(
            platform=SimpleNamespace(get_json=AsyncMock(side_effect=error))
        )
        with self.assertLogs(email_onboarding.logger, level="DEBUG") as messages:
            await email_onboarding.reconcile(ctx)
        self.assertEqual(ctx.email_setup_failure["http_status"], 404)
        self.assertTrue(all(row.levelname == "DEBUG" for row in messages.records))
        self.assertNotIn("private response body", str(messages.output))


class ApplyConfigTests(IsolatedAsyncioTestCase):
    async def test_email_does_not_block_secrets_or_silence_existing_telegram(self):
        for telegram, deferred in ((False, False), (False, True), (True, True)):
            with (
                self.subTest(telegram=telegram, deferred=deferred),
                tempfile.TemporaryDirectory() as tmp,
            ):
                values = {
                    "TINYHAT_EMAIL_CHANNEL_ENABLED": "1",
                    "OTHER_TOOL_KEY": "private-fixture",
                }
                ctx = SimpleNamespace(
                    platform=SimpleNamespace(
                        get_json=AsyncMock(return_value={"secrets": values})
                    ),
                    platform_auth="gcloud",
                )
                with (
                    patch.dict(os.environ, {"HOME": tmp}, clear=True),
                    patch.object(
                        apply_config,
                        "_env_file_candidates",
                        return_value=[Path(tmp) / "env"],
                    ),
                    patch.object(
                        apply_config, "find_hermes_binary", return_value=Path("/hermes")
                    ),
                    patch.object(apply_config, "sync_terminal_env_passthrough"),
                    patch.object(
                        apply_config,
                        "_run_gateway",
                        new_callable=AsyncMock,
                        return_value={"healthy": True},
                    ) as restart,
                    patch.object(
                        apply_config,
                        "_telegram_credentials",
                        side_effect=None
                        if telegram
                        else RuntimeError("not configured"),
                        return_value=("fixture", "owner"),
                    ),
                    patch.object(
                        apply_config, "_telegram_send", return_value={"ok": True}
                    ) as send,
                    patch.object(
                        email_onboarding,
                        "configure",
                        side_effect=ValueError("incomplete") if deferred else None,
                        return_value=False,
                    ),
                ):
                    result = await apply_config.run(ctx, {"spec": {}})
                    self.assertEqual(os.environ["OTHER_TOOL_KEY"], "private-fixture")
                    if not deferred:
                        self.assertTrue(ctx.email_gateway_ready)
                        ctx.platform.get_json.side_effect = lambda path: (
                            {"status": "ready"}
                            if path.endswith("/email")
                            else {"secrets": values}
                        )
                        with patch.object(
                            configure_telegram, "_run_gateway", new_callable=AsyncMock
                        ) as background_restart:
                            await email_onboarding.reconcile(ctx)
                        background_restart.assert_not_called()
                        self.assertIsNone(ctx.email_setup_failure)
                self.assertTrue(result["configured"])
                self.assertEqual(restart.await_count, 1)
                self.assertEqual(send.call_count, int(telegram))
                self.assertEqual(result["secret_available_notice"]["sent"], telegram)
                self.assertEqual(
                    result["email_setup_failure"], "ValueError" if deferred else None
                )
                if not telegram:
                    self.assertIsNone(result["secret_available_notice"]["ok"])


class ScheduleTests(IsolatedAsyncioTestCase):
    async def test_healthy_checks_slow_down_and_failures_back_off(self):
        ctx = SimpleNamespace(
            agent_api_context_ready=True, email_gateway_ready=True, email_checked_at=0
        )
        with (
            patch.object(email_onboarding, "reconcile", new_callable=AsyncMock) as run,
            patch.object(email_onboarding.time, "monotonic", return_value=100),
        ):
            email_onboarding.schedule(ctx)
            run.assert_not_called()
        self.assertEqual(email_onboarding.retry_interval(ctx), 300)
        ctx.email_setup_failure = {"stage": "gateway restart"}
        ctx.email_failure_count = 3
        self.assertEqual(email_onboarding.retry_interval(ctx), 240)

    async def test_slow_mail_setup_never_blocks_heartbeat_or_duplicates_task(self):
        gate = asyncio.Event()

        async def slow(ctx):
            await gate.wait()

        ctx = SimpleNamespace(agent_api_context_ready=True)
        with patch.object(email_onboarding, "reconcile", side_effect=slow) as reconcile:
            with patch.object(email_onboarding.time, "monotonic", return_value=0):
                email_onboarding.schedule(ctx)
            first = ctx.email_setup_task
            await asyncio.sleep(0)
            for _ in range(10):
                email_onboarding.schedule(ctx)
            self.assertIs(ctx.email_setup_task, first)
            self.assertEqual(reconcile.call_count, 1)
            gate.set()
            await first

    async def test_unassigned_computers_do_not_get_email(self):
        with patch.object(email_onboarding, "reconcile", new_callable=AsyncMock) as run:
            email_onboarding.schedule(SimpleNamespace(agent_api_context_ready=False))
            run.assert_not_called()
