"""Smoke tests for the Hermes runtime command whitelist."""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import TestCase
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hermes_runtime import __version__  # noqa: E402
from hermes_runtime.commands import run_command  # noqa: E402
from hermes_runtime.local_ledger import append_entry, report  # noqa: E402
from hermes_runtime.main import (  # noqa: E402
    RuntimeContext,
    _heartbeat_interval_seconds,
    _heartbeat_metrics,
    _heartbeat_once,
    _inspect_gateway_state,
    _service_generation_started_unix,
    _reexec_after_code_swap,
    _safe_activate_staged_on_startup,
    _run_one_command,
    _scheduled_update_check,
    run,
)
from hermes_runtime.platform_paths import (  # noqa: E402
    computer_api_path,
    context_computer_api_path,
)
from hermes_runtime.update_artifacts import (  # noqa: E402
    _safe_extract_tarball,
    activate_staged_runtime_code,
    prepare_staged_runtime,
    staged_bootstrap_file,
    staged_package_dir,
)
from hermes_runtime.update_check import (  # noqa: E402
    PENDING_SCHEDULED_RESULT_FILE,
    _write_text_atomic,
    run_update_check,
    scheduled_check_due,
)
from tinyhat_hermes_runtime_bootstrap import (  # noqa: E402
    recover_interrupted_package_swap as bootstrap_recover_interrupted_package_swap,
)


class FakePlatform:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[str] = []
        self.fail_posts = False
        self.post_response: dict[str, Any] = {"ok": True}

    async def post_json(self, path: str, payload: dict) -> dict:
        if self.fail_posts:
            raise RuntimeError("post failed")
        self.posts.append((path, payload))
        return self.post_response

    async def get_json(self, path: str) -> dict:
        self.gets.append(path)
        return {"ok": True, "path": path}


async def fake_plugin_update_status(_command: dict[str, Any]) -> dict[str, Any]:
    return {
        "plugin_ref": "channels/lts",
        "installed": {
            "installed": True,
            "plugin_dir": "/private/plugin",
            "manifest": "/private/plugin/plugin.yaml",
        },
        "target_commit": "a" * 40,
        "update_available": False,
        "decision": "installed_matches_target",
    }


class CommandTests(TestCase):
    def test_ping_returns_pong(self) -> None:
        result = asyncio.run(run_command(SimpleNamespace(), {"kind": "ping"}))
        self.assertEqual(result["message"], "pong")

    def test_platform_paths_use_local_dev_context(self) -> None:
        self.assertEqual(
            computer_api_path("computer 123", "heartbeat"),
            "/hapi/v1/computers/local-dev/heartbeat",
        )

    def test_platform_paths_use_gcloud_me_context(self) -> None:
        ctx = SimpleNamespace(platform_auth="gcloud", computer_id="123")

        self.assertEqual(
            context_computer_api_path(ctx, "heartbeat"),
            "/hapi/v1/computers/me/heartbeat",
        )
        self.assertEqual(
            context_computer_api_path(ctx, "runtime-command/result"),
            "/hapi/v1/computers/me/runtime-command/result",
        )

    def test_whoami_uses_local_dev_attestation_path(self) -> None:
        platform = FakePlatform()
        ctx = SimpleNamespace(
            platform=platform,
            computer_id="computer 123",
            platform_auth="local_dev",
        )

        result = asyncio.run(run_command(ctx, {"kind": "whoami"}))

        self.assertEqual(
            platform.gets,
            ["/hapi/v1/computers/local-dev/whoami"],
        )
        self.assertEqual(
            result["attestation"]["path"],
            "/hapi/v1/computers/local-dev/whoami",
        )

    def test_whoami_uses_gcloud_attestation_path(self) -> None:
        platform = FakePlatform()
        ctx = SimpleNamespace(platform=platform, computer_id="123", platform_auth="gcloud")

        result = asyncio.run(run_command(ctx, {"kind": "whoami"}))

        self.assertEqual(platform.gets, ["/hapi/v1/computers/me/whoami"])
        self.assertEqual(result["attestation"]["path"], "/hapi/v1/computers/me/whoami")

    def test_heartbeat_records_platform_state_for_cadence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            platform = FakePlatform()
            platform.post_response = {"ok": True, "state": "assigned"}
            ctx = RuntimeContext(
                platform=platform,
                state_dir=Path(tmp),
                started_at=0,
                platform_state="ready",
            )

            with patch.dict(os.environ, {}, clear=True):
                asyncio.run(_heartbeat_once(ctx))

        self.assertEqual(ctx.platform_state, "assigned")

    def test_gateway_heartbeat_reports_serving_draining_and_unknown(self) -> None:
        async def inspect(
            result: dict[str, Any],
            *,
            functional_ready: bool = False,
        ) -> dict[str, Any]:
            async def fake_run_process(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
                return result

            async def fake_probe(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
                return {
                    "functionally_ready": functional_ready,
                    "telegram_evidence": "journal",
                }

            discovery = {
                "ok": True,
                "reason": "gateway_service_owner_found",
                "owner": {"manager": "user", "systemctl": "/bin/systemctl"},
                "generation": {
                    "manager": "user",
                    "load_state": "loaded",
                    "active_state": "active",
                    "sub_state": "running",
                    "main_pid": 42,
                    "invocation_id": "invocation-1",
                },
            }

            with (
                patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "token"}, clear=True),
                patch(
                    "hermes_runtime.main.find_hermes_binary",
                    return_value=Path("/usr/local/bin/hermes"),
                ),
                patch("hermes_runtime.main.run_process", fake_run_process),
                patch(
                    "hermes_runtime.main.discover_gateway_service",
                    return_value=discovery,
                ),
                patch(
                    "hermes_runtime.main.probe_functional_readiness",
                    fake_probe,
                ),
            ):
                return await _inspect_gateway_state()

        serving = asyncio.run(
            inspect(
                {"ok": True, "stdout": "Active: active (running)", "stderr": ""},
                functional_ready=True,
            )
        )
        serving_unverified = asyncio.run(
            inspect(
                {"ok": True, "stdout": "Active: active (running)", "stderr": ""}
            )
        )
        draining = asyncio.run(
            inspect(
                {
                    "ok": True,
                    "stdout": "Gateway draining for restart",
                    "stderr": "",
                }
            )
        )
        non_serving = asyncio.run(
            inspect({"ok": True, "stdout": "Active: inactive (dead)", "stderr": ""})
        )
        non_serving_error = asyncio.run(
            inspect(
                {
                    "ok": False,
                    "stdout": "Active: inactive (dead)",
                    "stderr": "gateway is not running",
                }
            )
        )
        unknown = asyncio.run(inspect({"ok": False, "stdout": "", "stderr": "timeout"}))
        telegram_fatal = asyncio.run(
            inspect(
                {
                    "ok": True,
                    "stdout": (
                        "Active: active (running)\nRecent gateway health:\n"
                        "  ⚠ telegram: polling task stopped"
                    ),
                    "stderr": "",
                },
                functional_ready=True,
            )
        )

        self.assertEqual((serving["status"], serving["ready"]), ("serving", True))
        self.assertEqual(
            (serving_unverified["status"], serving_unverified["ready"]),
            ("serving_unverified", None),
        )
        self.assertIs(serving["details"]["functional_ready"], True)
        self.assertIs(serving_unverified["details"]["functional_ready"], False)
        self.assertEqual(
            (draining["status"], draining["ready"]),
            ("draining_restarting", False),
        )
        self.assertEqual(
            (non_serving["status"], non_serving["ready"]),
            ("non_serving", False),
        )
        self.assertEqual(
            (non_serving_error["status"], non_serving_error["ready"]),
            ("non_serving", False),
        )
        self.assertEqual((unknown["status"], unknown["ready"]), ("unknown", None))
        self.assertEqual(
            (telegram_fatal["status"], telegram_fatal["ready"]),
            ("non_serving", False),
        )
        self.assertEqual(
            telegram_fatal["reason"],
            "gateway_status_telegram_fatal",
        )

    def test_service_generation_start_is_converted_to_unix_time(self) -> None:
        with (
            patch("hermes_runtime.main.time.time", return_value=1_000.0),
            patch("hermes_runtime.main.time.monotonic", return_value=50.0),
        ):
            started = _service_generation_started_unix(
                {"exec_main_start_timestamp_monotonic": 40_000_000}
            )
            missing = _service_generation_started_unix({})

        self.assertEqual(started, 990.0)
        self.assertEqual(missing, 1_000.0)

    def test_gateway_heartbeat_reports_verified_foreground_as_serving(self) -> None:
        runtime_generation = {
            "pid": 321,
            "start_time": 654,
            "started_at_unix": 1000.0,
            "argv": [
                "gateway",
                "run",
                "--replace",
                "--force",
                "--accept-hooks",
            ],
        }
        foreground_generation = {
            "schema": "tinyhat_hermes_foreground_gateway_v1",
            "pid": 321,
            "process_start_time": 654,
            "started_at_unix": 1000.0,
            "log_offset": 27,
            "argv": runtime_generation["argv"],
        }

        async def fake_status(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "stdout": "Active: active (running)",
                "stderr": "",
            }

        async def fake_probe(*_args: Any, **kwargs: Any) -> dict[str, Any]:
            self.assertEqual(kwargs["since_unix"], 1000.0)
            self.assertEqual(kwargs["log_offset"], 27)
            self.assertEqual(kwargs["service_main_pid"], 321)
            self.assertEqual(kwargs["expected_process_start_time"], 654)
            self.assertEqual(
                kwargs["expected_gateway_argv"], runtime_generation["argv"]
            )
            self.assertTrue(
                str(kwargs["log_path"]).endswith("hermes-gateway.log")
            )
            self.assertNotIn("service_invocation_id", kwargs)
            return {
                "functionally_ready": True,
                "telegram_evidence": "runtime_state",
            }

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.dict(
                    os.environ,
                    {
                        "TELEGRAM_BOT_TOKEN": "token",
                        "TINYHAT_RUNTIME_STATE_DIR": tmp,
                    },
                    clear=True,
                ),
                patch(
                    "hermes_runtime.main.find_hermes_binary",
                    return_value=Path("/usr/local/bin/hermes"),
                ),
                patch("hermes_runtime.main.run_process", fake_status),
                patch(
                    "hermes_runtime.main.discover_gateway_service",
                    return_value={
                        "ok": False,
                        "reason": "systemctl_unavailable",
                        "owner": None,
                        "generation": None,
                    },
                ),
                patch(
                    "hermes_runtime.main.read_gateway_runtime_generation",
                    return_value=runtime_generation,
                ),
                patch(
                    "hermes_runtime.main._active_gateway_foreground_generation",
                    return_value=foreground_generation,
                ),
                patch(
                    "hermes_runtime.main.probe_functional_readiness",
                    fake_probe,
                ),
            ):
                state = asyncio.run(_inspect_gateway_state())

        self.assertEqual((state["status"], state["ready"]), ("serving", True))
        self.assertIs(state["details"]["functional_ready"], True)
        self.assertEqual(
            state["details"]["telegram_evidence"], "runtime_state"
        )
        self.assertEqual(
            state["details"]["runtime_generation"],
            {
                "pid": 321,
                "process_start_time": 654,
                "started_at_unix": 1000.0,
                "command_kind": "gateway_run",
                "identity_verified": True,
            },
        )
        self.assertEqual(
            state["details"]["foreground_generation"],
            {
                "pid": 321,
                "process_start_time": 654,
                "started_at_unix": 1000.0,
                "command_kind": "gateway_run",
                "identity_verified": True,
                "matches_runtime": True,
            },
        )
        self.assertIsNone(state["details"]["service_generation"])

    def test_gateway_heartbeat_caches_only_matching_foreground_generation(
        self,
    ) -> None:
        generation = {
            "pid": 321,
            "start_time": 654,
            "started_at_unix": 1000.0,
            "argv": ["gateway", "run"],
        }

        async def fake_status(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "stdout": "Active: active (running)",
                "stderr": "",
            }

        async def fail_probe(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("fresh matching proof should be cached")

        previous = {
            "status": "serving",
            "ready": True,
            "details": {
                "functional_ready": True,
                "functional_verified_at_unix": int(time.time()),
                "runtime_generation": generation,
                "telegram_evidence": "log",
                "service_generation": None,
            },
        }
        with (
            patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "token"}, clear=True),
            patch(
                "hermes_runtime.main.find_hermes_binary",
                return_value=Path("/usr/local/bin/hermes"),
            ),
            patch("hermes_runtime.main.run_process", fake_status),
            patch(
                "hermes_runtime.main.discover_gateway_service",
                return_value={
                    "ok": False,
                    "reason": "gateway_service_not_found",
                    "owner": None,
                    "generation": None,
                },
            ),
            patch(
                "hermes_runtime.main.read_gateway_runtime_generation",
                return_value=generation,
            ),
            patch(
                "hermes_runtime.main._active_gateway_foreground_generation",
                return_value=None,
            ),
            patch(
                "hermes_runtime.main.probe_functional_readiness",
                fail_probe,
            ),
        ):
            state = asyncio.run(_inspect_gateway_state(previous))

        self.assertEqual((state["status"], state["ready"]), ("serving", True))
        self.assertEqual(state["details"]["telegram_evidence"], "log")

    def test_gateway_heartbeat_reports_non_systemd_supervisor_as_serving(
        self,
    ) -> None:
        generation = {
            "pid": 444,
            "start_time": 4004,
            "started_at_unix": 1100.0,
            "argv": ["--profile", "work", "gateway", "run"],
        }

        async def fake_status(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "stdout": "Active: active (running)",
                "stderr": "",
            }

        async def fake_probe(*_args: Any, **kwargs: Any) -> dict[str, Any]:
            self.assertEqual(kwargs["service_main_pid"], 444)
            self.assertEqual(kwargs["expected_process_start_time"], 4004)
            self.assertEqual(
                kwargs["expected_gateway_argv"], generation["argv"]
            )
            self.assertIsNone(kwargs["log_path"])
            return {
                "functionally_ready": True,
                "telegram_evidence": "runtime_state",
            }

        with (
            patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "token"}, clear=True),
            patch(
                "hermes_runtime.main.find_hermes_binary",
                return_value=Path("/usr/local/bin/hermes"),
            ),
            patch("hermes_runtime.main.run_process", fake_status),
            patch(
                "hermes_runtime.main.discover_gateway_service",
                return_value={
                    "ok": False,
                    "reason": "systemd_manager_absent",
                    "owner": None,
                    "generation": None,
                },
            ),
            patch(
                "hermes_runtime.main.read_gateway_runtime_generation",
                return_value=generation,
            ),
            patch(
                "hermes_runtime.main._active_gateway_foreground_generation",
                return_value=None,
            ),
            patch(
                "hermes_runtime.main.probe_functional_readiness",
                fake_probe,
            ),
        ):
            state = asyncio.run(_inspect_gateway_state())

        self.assertEqual((state["status"], state["ready"]), ("serving", True))
        self.assertEqual(
            state["details"]["runtime_generation"],
            {
                "pid": 444,
                "process_start_time": 4004,
                "started_at_unix": 1100.0,
                "command_kind": "gateway_run",
                "identity_verified": True,
            },
        )
        self.assertIsNone(state["details"]["foreground_generation"])

    def test_gateway_heartbeat_keeps_unproven_foreground_unverified(self) -> None:
        async def fake_status(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "stdout": "Active: active (running)",
                "stderr": "",
            }

        async def fail_probe(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("unproven foreground owner must not be probed")

        with (
            patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "token"}, clear=True),
            patch(
                "hermes_runtime.main.find_hermes_binary",
                return_value=Path("/usr/local/bin/hermes"),
            ),
            patch("hermes_runtime.main.run_process", fake_status),
            patch(
                "hermes_runtime.main.discover_gateway_service",
                return_value={
                    "ok": False,
                    "reason": "systemctl_unavailable",
                    "owner": None,
                    "generation": None,
                },
            ),
            patch(
                "hermes_runtime.main.read_gateway_runtime_generation",
                return_value=None,
            ),
            patch(
                "hermes_runtime.main._active_gateway_foreground_generation",
                return_value=None,
            ),
            patch(
                "hermes_runtime.main.probe_functional_readiness",
                fail_probe,
            ),
        ):
            state = asyncio.run(_inspect_gateway_state())

        self.assertEqual(
            (state["status"], state["ready"]),
            ("serving_unverified", None),
        )
        self.assertIs(state["details"]["functional_ready"], False)

    def test_gateway_heartbeat_rechecks_expired_functional_proof(self) -> None:
        probes = 0

        async def fake_status(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"ok": True, "stdout": "Active: active (running)", "stderr": ""}

        async def fake_probe(*_args: Any, **kwargs: Any) -> dict[str, Any]:
            nonlocal probes
            probes += 1
            self.assertEqual(kwargs["service_main_pid"], 42)
            self.assertGreater(kwargs["since_unix"], 0)
            return {
                "functionally_ready": True,
                "telegram_evidence": "journal",
            }

        generation = {
            "manager": "user",
            "load_state": "loaded",
            "active_state": "active",
            "sub_state": "running",
            "main_pid": 42,
            "invocation_id": "invocation-1",
        }
        discovery = {
            "ok": True,
            "reason": "gateway_service_owner_found",
            "owner": {"manager": "user", "systemctl": "/bin/systemctl"},
            "generation": generation,
        }
        previous = {
            "status": "serving",
            "ready": True,
            "details": {
                "functional_ready": True,
                "functional_verified_at_unix": int(time.time()) - 61,
                "service_generation": generation,
                "telegram_evidence": "journal",
            },
        }

        with (
            patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "token"}, clear=True),
            patch(
                "hermes_runtime.main.find_hermes_binary",
                return_value=Path("/usr/local/bin/hermes"),
            ),
            patch("hermes_runtime.main.run_process", fake_status),
            patch(
                "hermes_runtime.main.discover_gateway_service",
                return_value=discovery,
            ),
            patch("hermes_runtime.main.probe_functional_readiness", fake_probe),
        ):
            state = asyncio.run(_inspect_gateway_state(previous))

        self.assertEqual(probes, 1)
        self.assertEqual((state["status"], state["ready"]), ("serving", True))
        self.assertGreater(
            state["details"]["functional_verified_at_unix"],
            previous["details"]["functional_verified_at_unix"],
        )

    def test_assigned_heartbeat_starts_gateway_heal_when_telegram_configured(
        self,
    ) -> None:
        async def scenario() -> list[dict[str, Any]]:
            with tempfile.TemporaryDirectory() as tmp:
                platform = FakePlatform()
                platform.post_response = {"ok": True, "state": "assigned"}
                ctx = RuntimeContext(
                    platform=platform,
                    state_dir=Path(tmp),
                    started_at=0,
                    platform_state="ready",
                )
                started = asyncio.Event()
                commands: list[dict[str, Any]] = []

                async def fake_run_command(
                    _ctx: RuntimeContext, command: dict[str, Any]
                ) -> dict[str, Any]:
                    commands.append(command)
                    started.set()
                    return {"healthy": True}

                with (
                    patch.dict(
                        os.environ,
                        {
                            "HOME": str(Path(tmp) / "home"),
                            "TELEGRAM_BOT_TOKEN": "bot-token",
                            "TINYHAT_HERMES_HOME": str(Path(tmp) / "hermes-home"),
                        },
                        clear=True,
                    ),
                    patch(
                        "hermes_runtime.main.find_hermes_binary",
                        return_value=None,
                    ),
                    patch("hermes_runtime.main.run_command", fake_run_command),
                ):
                    await asyncio.wait_for(_heartbeat_once(ctx), timeout=0.2)
                    self.assertNotIn(
                        "gateway",
                        platform.posts[-1][1]["metrics"]["hermes_runtime"],
                    )
                    await asyncio.wait_for(started.wait(), timeout=0.2)
                    self.assertIsNotNone(ctx.gateway_reconcile_task)
                    await asyncio.wait_for(ctx.gateway_reconcile_task, timeout=0.2)

                    await asyncio.wait_for(_heartbeat_once(ctx), timeout=0.2)
                    gateway_state = platform.posts[-1][1]["metrics"][
                        "hermes_runtime"
                    ]["gateway"]
                    self.assertEqual(gateway_state["status"], "unknown")

                return commands

        commands = asyncio.run(scenario())

        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]["kind"], "heal_hermes")
        self.assertEqual(
            commands[0]["spec"]["reason"],
            "runtime_assigned_heartbeat_reconcile",
        )

    def test_assigned_heartbeat_skips_gateway_heal_without_telegram_config(
        self,
    ) -> None:
        async def scenario() -> RuntimeContext:
            with tempfile.TemporaryDirectory() as tmp:
                platform = FakePlatform()
                platform.post_response = {"ok": True, "state": "assigned"}
                ctx = RuntimeContext(
                    platform=platform,
                    state_dir=Path(tmp),
                    started_at=0,
                    platform_state="ready",
                )

                async def fake_run_command(
                    _ctx: RuntimeContext, command: dict[str, Any]
                ) -> dict[str, Any]:
                    raise AssertionError(f"unexpected command: {command}")

                with (
                    patch.dict(
                        os.environ,
                        {
                            "HOME": str(Path(tmp) / "home"),
                            "TELEGRAM_BOT_TOKEN": "",
                            "TINYHAT_HERMES_HOME": str(Path(tmp) / "hermes-home"),
                        },
                        clear=True,
                    ),
                    patch("hermes_runtime.main.run_command", fake_run_command),
                ):
                    await asyncio.wait_for(_heartbeat_once(ctx), timeout=0.2)
                return ctx

        ctx = asyncio.run(scenario())

        self.assertIsNone(ctx.gateway_reconcile_task)
        self.assertFalse(ctx.gateway_reconciled)

    def test_gateway_reconcile_runs_once_per_process(self) -> None:
        async def scenario() -> list[dict[str, Any]]:
            with tempfile.TemporaryDirectory() as tmp:
                platform = FakePlatform()
                platform.post_response = {"ok": True, "state": "assigned"}
                ctx = RuntimeContext(
                    platform=platform,
                    state_dir=Path(tmp),
                    started_at=0,
                    platform_state="assigned",
                )
                commands: list[dict[str, Any]] = []

                async def fake_run_command(
                    _ctx: RuntimeContext, command: dict[str, Any]
                ) -> dict[str, Any]:
                    commands.append(command)
                    return {"healthy": True, "reason": "gateway_healthy"}

                with (
                    patch.dict(
                        os.environ,
                        {
                            "HOME": str(Path(tmp) / "home"),
                            "TELEGRAM_BOT_TOKEN": "bot-token",
                        },
                        clear=True,
                    ),
                    patch("hermes_runtime.main.find_hermes_binary", return_value=None),
                    patch("hermes_runtime.main.run_command", fake_run_command),
                ):
                    await asyncio.wait_for(_heartbeat_once(ctx), timeout=0.2)
                    assert ctx.gateway_reconcile_task is not None
                    await asyncio.wait_for(ctx.gateway_reconcile_task, timeout=0.2)

                    # Later heartbeats must never start another reconcile:
                    # the bring-up runs at most once per runtime process.
                    await asyncio.wait_for(_heartbeat_once(ctx), timeout=0.2)
                    await asyncio.wait_for(_heartbeat_once(ctx), timeout=0.2)
                    self.assertTrue(ctx.gateway_reconciled)
                    self.assertIsNone(ctx.gateway_reconcile_task)

                    # The heartbeat still carries the observed gateway state.
                    gateway_state = platform.posts[-1][1]["metrics"][
                        "hermes_runtime"
                    ]["gateway"]
                    self.assertEqual(gateway_state["status"], "unknown")

                return commands

        commands = asyncio.run(scenario())

        self.assertEqual(len(commands), 1)

    def test_gateway_reconcile_defers_while_platform_command_runs(self) -> None:
        async def scenario() -> list[dict[str, Any]]:
            with tempfile.TemporaryDirectory() as tmp:
                platform = FakePlatform()
                platform.post_response = {
                    "ok": True,
                    "state": "assigned",
                    "command": {
                        "command_id": "cmd-configure",
                        "kind": "configure_telegram",
                        "spec": {},
                    },
                }
                ctx = RuntimeContext(
                    platform=platform,
                    state_dir=Path(tmp),
                    started_at=0,
                    platform_state="assigned",
                )
                heal_commands: list[dict[str, Any]] = []
                release = asyncio.Event()

                async def fake_run_command(
                    _ctx: RuntimeContext, command: dict[str, Any]
                ) -> dict[str, Any]:
                    if command.get("kind") == "heal_hermes":
                        heal_commands.append(command)
                        return {"healthy": True, "reason": "gateway_healthy"}
                    await release.wait()
                    return {"message": "done"}

                base_env = {"HOME": str(Path(tmp) / "home")}
                configured_env = {**base_env, "TELEGRAM_BOT_TOKEN": "bot-token"}
                with (
                    patch("hermes_runtime.main.find_hermes_binary", return_value=None),
                    patch("hermes_runtime.main.run_command", fake_run_command),
                ):
                    # Beat 1: telegram is not configured yet; the platform
                    # command starts and keeps running.
                    with patch.dict(os.environ, base_env, clear=True):
                        await asyncio.wait_for(_heartbeat_once(ctx), timeout=0.2)
                    assert ctx.command_task is not None

                    # Beat 2: telegram is configured now, but the command is
                    # still mid-flight -- the one-shot bring-up must defer.
                    platform.post_response = {"ok": True, "state": "assigned"}
                    with patch.dict(os.environ, configured_env, clear=True):
                        await asyncio.wait_for(_heartbeat_once(ctx), timeout=0.2)
                    self.assertIsNone(ctx.gateway_reconcile_task)
                    self.assertFalse(ctx.gateway_reconciled)

                    release.set()
                    await asyncio.wait_for(ctx.command_task, timeout=0.2)

                    # Beat 3: the command settled; the one-shot fires now.
                    with patch.dict(os.environ, configured_env, clear=True):
                        await asyncio.wait_for(_heartbeat_once(ctx), timeout=0.2)
                        assert ctx.gateway_reconcile_task is not None
                        await asyncio.wait_for(
                            ctx.gateway_reconcile_task, timeout=0.2
                        )
                    self.assertTrue(ctx.gateway_reconciled)

                return heal_commands

        heal_commands = asyncio.run(scenario())

        self.assertEqual(len(heal_commands), 1)
        self.assertEqual(heal_commands[0]["kind"], "heal_hermes")

    def test_heartbeat_starts_any_command_without_blocking_loop(self) -> None:
        async def scenario() -> tuple[RuntimeContext, FakePlatform]:
            with tempfile.TemporaryDirectory() as tmp:
                platform = FakePlatform()
                platform.post_response = {
                    "ok": True,
                    "state": "ready",
                    "command": {
                        "command_id": "cmd-slow",
                        "idempotency_key": "idem-slow",
                        "kind": "install_hermes",
                        "spec": {},
                    },
                }
                ctx = RuntimeContext(
                    platform=platform,
                    state_dir=Path(tmp),
                    started_at=0,
                    platform_state="ready",
                )
                started = asyncio.Event()
                release = asyncio.Event()

                async def fake_run_command(
                    _ctx: RuntimeContext, command: dict[str, Any]
                ) -> dict[str, str]:
                    self.assertEqual(command["command_id"], "cmd-slow")
                    started.set()
                    await release.wait()
                    return {"message": "done"}

                with patch("hermes_runtime.main.run_command", fake_run_command):
                    await asyncio.wait_for(_heartbeat_once(ctx), timeout=0.2)
                    await asyncio.wait_for(started.wait(), timeout=0.2)

                    self.assertIsNotNone(ctx.command_task)
                    self.assertFalse(ctx.command_task.done())
                    self.assertEqual(ctx.command_id, "cmd-slow")
                    self.assertEqual(ctx.command_kind, "install_hermes")
                    self.assertFalse(
                        any(
                            path.endswith("/runtime-command/result")
                            for path, _payload in platform.posts
                        )
                    )

                    platform.post_response = {"ok": True, "state": "ready"}
                    await asyncio.wait_for(_heartbeat_once(ctx), timeout=0.2)
                    heartbeat = platform.posts[-1][1]["metrics"]["hermes_runtime"]
                    self.assertEqual(
                        heartbeat["active_command"],
                        {
                            "command_id": "cmd-slow",
                            "kind": "install_hermes",
                            "status": "running",
                        },
                    )

                    release.set()
                    await asyncio.wait_for(ctx.command_task, timeout=0.2)
                    await asyncio.wait_for(_heartbeat_once(ctx), timeout=0.2)

                self.assertIsNone(ctx.command_task)
                self.assertIsNone(ctx.command_id)
                self.assertIsNone(ctx.command_kind)
                result_posts = [
                    payload
                    for path, payload in platform.posts
                    if path.endswith("/runtime-command/result")
                ]
                self.assertEqual(len(result_posts), 1)
                self.assertEqual(result_posts[0]["result"]["status"], "applied")
                self.assertEqual(
                    result_posts[0]["result"]["result"], {"message": "done"}
                )
                return ctx, platform

        asyncio.run(scenario())

    def test_heartbeat_ignores_new_command_while_one_is_running(self) -> None:
        async def scenario() -> tuple[RuntimeContext, FakePlatform]:
            with tempfile.TemporaryDirectory() as tmp:
                platform = FakePlatform()
                platform.post_response = {
                    "ok": True,
                    "state": "ready",
                    "command": {
                        "command_id": "cmd-a",
                        "idempotency_key": "idem-a",
                        "kind": "install_hermes",
                        "spec": {},
                    },
                }
                ctx = RuntimeContext(
                    platform=platform,
                    state_dir=Path(tmp),
                    started_at=0,
                    platform_state="ready",
                )
                release = asyncio.Event()
                started = asyncio.Event()
                started_commands: list[str] = []

                async def fake_run_command(
                    _ctx: RuntimeContext, command: dict[str, Any]
                ) -> dict[str, str]:
                    started_commands.append(str(command["command_id"]))
                    started.set()
                    await release.wait()
                    return {"message": f"done:{command['command_id']}"}

                with patch("hermes_runtime.main.run_command", fake_run_command):
                    await asyncio.wait_for(_heartbeat_once(ctx), timeout=0.2)
                    await asyncio.wait_for(started.wait(), timeout=0.2)
                    self.assertEqual(started_commands, ["cmd-a"])

                    platform.post_response = {
                        "ok": True,
                        "state": "ready",
                        "command": {
                            "command_id": "cmd-b",
                            "idempotency_key": "idem-b",
                            "kind": "setup_snapshot",
                            "spec": {},
                        },
                    }
                    await asyncio.wait_for(_heartbeat_once(ctx), timeout=0.2)

                    self.assertEqual(started_commands, ["cmd-a"])
                    self.assertEqual(ctx.command_id, "cmd-a")
                    self.assertEqual(ctx.command_kind, "install_hermes")
                    self.assertFalse(
                        any(
                            path.endswith("/runtime-command/result")
                            for path, _payload in platform.posts
                        )
                    )

                    platform.post_response = {"ok": True, "state": "ready"}
                    release.set()
                    await asyncio.wait_for(ctx.command_task, timeout=0.2)
                    await asyncio.wait_for(_heartbeat_once(ctx), timeout=0.2)

                result_posts = [
                    payload
                    for path, payload in platform.posts
                    if path.endswith("/runtime-command/result")
                ]
                self.assertEqual(len(result_posts), 1)
                self.assertEqual(result_posts[0]["result"]["command_id"], "cmd-a")
                self.assertEqual(
                    result_posts[0]["result"]["result"], {"message": "done:cmd-a"}
                )
                return ctx, platform

        asyncio.run(scenario())

    def test_restart_waits_for_async_command_result_report(self) -> None:
        class SlowResultPlatform(FakePlatform):
            def __init__(self) -> None:
                super().__init__()
                self.heartbeat_count = 0
                self.result_started = asyncio.Event()
                self.result_finished = asyncio.Event()

            async def post_json(self, path: str, payload: dict) -> dict:
                if path.endswith("/runtime-command/result"):
                    self.result_started.set()
                    await asyncio.sleep(0.2)
                    self.posts.append((path, payload))
                    self.result_finished.set()
                    return {"ok": True}
                self.posts.append((path, payload))
                self.heartbeat_count += 1
                if self.heartbeat_count == 1:
                    return {
                        "ok": True,
                        "state": "ready",
                        "command": {
                            "command_id": "cmd-restart",
                            "idempotency_key": "idem-restart",
                            "kind": "restart_runtime_service",
                            "spec": {},
                        },
                    }
                return {"ok": True, "state": "ready"}

        async def scenario() -> SlowResultPlatform:
            platform = SlowResultPlatform()

            async def fake_run_command(
                ctx: RuntimeContext, _command: dict[str, Any]
            ) -> dict[str, str]:
                ctx.restart_requested = True
                return {"effect": "after_command_result"}

            with (
                tempfile.TemporaryDirectory() as tmp,
                patch.dict(
                    os.environ,
                    {
                        "TINYHAT_PLATFORM_URL": "http://platform.test",
                        "TINYHAT_LOCAL_DEV_TOKEN": "dev-token",
                        "TINYHAT_COMPUTER_ID": "local-dev",
                        "TINYHAT_RUNTIME_STATE_DIR": tmp,
                        "TINYHAT_HEARTBEAT_INTERVAL_SECONDS": "0.1",
                    },
                    clear=True,
                ),
                patch("hermes_runtime.main.PlatformClient", return_value=platform),
                patch(
                    "hermes_runtime.main._safe_activate_staged_on_startup",
                    return_value=None,
                ),
                patch("hermes_runtime.main.run_command", fake_run_command),
            ):
                result = await asyncio.wait_for(run(), timeout=2)

            self.assertEqual(result, 0)
            self.assertTrue(platform.result_started.is_set())
            self.assertTrue(platform.result_finished.is_set())
            result_posts = [
                payload
                for path, payload in platform.posts
                if path.endswith("/runtime-command/result")
            ]
            self.assertEqual(len(result_posts), 1)
            self.assertEqual(result_posts[0]["result"]["command_id"], "cmd-restart")
            self.assertGreaterEqual(platform.heartbeat_count, 2)
            return platform

        asyncio.run(scenario())

    def test_heartbeat_interval_is_fast_until_assigned(self) -> None:
        ctx = SimpleNamespace(platform_state="ready")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_heartbeat_interval_seconds(ctx), 1.0)

        ctx.platform_state = "assigned"
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_heartbeat_interval_seconds(ctx), 10.0)

        ctx.platform_state = "active"
        with patch.dict(
            os.environ,
            {"TINYHAT_ASSIGNED_HEARTBEAT_INTERVAL_SECONDS": "12"},
            clear=True,
        ):
            self.assertEqual(_heartbeat_interval_seconds(ctx), 12.0)

        with patch.dict(
            os.environ,
            {"TINYHAT_HEARTBEAT_INTERVAL_SECONDS": "30"},
            clear=True,
        ):
            self.assertEqual(_heartbeat_interval_seconds(ctx), 30.0)

    def test_heartbeat_interval_falls_back_on_malformed_env(self) -> None:
        ctx = SimpleNamespace(platform_state="assigned")
        with patch.dict(
            os.environ,
            {"TINYHAT_HEARTBEAT_INTERVAL_SECONDS": "not-a-number"},
            clear=True,
        ):
            self.assertEqual(_heartbeat_interval_seconds(ctx), 10.0)

        with patch.dict(
            os.environ,
            {"TINYHAT_ASSIGNED_HEARTBEAT_INTERVAL_SECONDS": "12,5"},
            clear=True,
        ):
            self.assertEqual(_heartbeat_interval_seconds(ctx), 10.0)

        ctx.platform_state = "ready"
        with patch.dict(
            os.environ,
            {"TINYHAT_UNASSIGNED_HEARTBEAT_INTERVAL_SECONDS": "oops"},
            clear=True,
        ):
            self.assertEqual(_heartbeat_interval_seconds(ctx), 1.0)

    def test_update_is_staged_then_marked_for_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            source_root = state_dir / "source"
            (source_root / "hermes_runtime" / "commands").mkdir(parents=True)
            (source_root / "hermes_runtime" / "__init__.py").write_text(
                '__version__ = "0.20.0-dev"\n',
                encoding="utf-8",
            )
            (source_root / "hermes_runtime" / "commands" / "__init__.py").write_text(
                "COMMAND_MODULES = {}\n",
                encoding="utf-8",
            )
            (source_root / "tinyhat_hermes_runtime_bootstrap.py").write_text(
                "BOOTSTRAP = True\n",
                encoding="utf-8",
            )
            ctx = SimpleNamespace(
                state_dir=state_dir,
                restart_requested=False,
                staged_version_file=state_dir / "staged" / "VERSION",
                activation_marker=state_dir / "ACTIVATE_ON_RESTART",
                staged_version=lambda: (
                    (state_dir / "staged" / "VERSION")
                    .read_text(encoding="utf-8")
                    .strip()
                    if (state_dir / "staged" / "VERSION").exists()
                    else None
                ),
            )
            with patch.dict(
                "os.environ",
                {"TINYHAT_RUNTIME_UPDATE_SOURCE_DIR": str(source_root)},
            ):
                staged = asyncio.run(
                    run_command(
                        ctx,
                        {
                            "kind": "stage_update",
                            "spec": {
                                "target_ref": "v0.20.0-dev.20260625T173000Z.smoke",
                                "channel": "custom",
                            },
                        },
                    )
                )
            self.assertEqual(
                staged["target_ref"], "v0.20.0-dev.20260625T173000Z.smoke"
            )
            self.assertEqual(staged["activation"], "requires_activate_update")
            self.assertTrue(staged["code_staged"])
            self.assertTrue((staged_package_dir(state_dir) / "__init__.py").is_file())
            self.assertEqual(
                ctx.staged_version(), "v0.20.0-dev.20260625T173000Z.smoke"
            )

            activated = asyncio.run(
                run_command(ctx, {"kind": "activate_update", "spec": {}})
            )
            self.assertEqual(
                activated["target_version"], "v0.20.0-dev.20260625T173000Z.smoke"
            )
            self.assertTrue(ctx.restart_requested)
            self.assertEqual(
                ctx.activation_marker.read_text().strip(),
                "v0.20.0-dev.20260625T173000Z.smoke",
            )

    def test_activation_swaps_staged_runtime_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            install_prefix = root / "install"
            source_root = root / "source"
            (install_prefix / "hermes_runtime").mkdir(parents=True)
            (install_prefix / "hermes_runtime" / "old_only.py").write_text(
                "OLD = True\n",
                encoding="utf-8",
            )
            (source_root / "hermes_runtime" / "commands").mkdir(parents=True)
            (source_root / "hermes_runtime" / "__init__.py").write_text(
                '__version__ = "0.0.3"\n',
                encoding="utf-8",
            )
            (source_root / "hermes_runtime" / "new_command.py").write_text(
                "NEW = True\n",
                encoding="utf-8",
            )
            (source_root / "hermes_runtime" / "commands" / "__init__.py").write_text(
                "COMMAND_MODULES = {}\n",
                encoding="utf-8",
            )
            (source_root / "tinyhat_hermes_runtime_bootstrap.py").write_text(
                "BOOTSTRAP = True\n",
                encoding="utf-8",
            )
            ctx = RuntimeContext(
                platform=FakePlatform(),
                state_dir=state_dir,
                started_at=0,
            )
            with patch.dict(
                "os.environ",
                {
                    "TINYHAT_RUNTIME_PREFIX": str(install_prefix),
                    "TINYHAT_RUNTIME_UPDATE_SOURCE_DIR": str(source_root),
                },
            ):
                asyncio.run(
                    run_command(
                        ctx,
                        {
                            "kind": "stage_update",
                            "spec": {"target_ref": "v0.0.3", "channel": "custom"},
                        },
                    )
                )
                asyncio.run(run_command(ctx, {"kind": "activate_update"}))
                activated = ctx.activate_staged_on_startup()

            self.assertEqual(activated, {"version": "v0.0.3", "code_swapped": True})
            self.assertFalse((install_prefix / "hermes_runtime" / "old_only.py").exists())
            self.assertTrue(
                (install_prefix / "hermes_runtime" / "new_command.py").is_file()
            )
            self.assertTrue(
                (install_prefix / "tinyhat_hermes_runtime_bootstrap.py").is_file()
            )
            self.assertEqual(ctx.current_version(), "v0.0.3")
            self.assertFalse(staged_package_dir(state_dir).exists())

    def test_activation_recovers_interrupted_package_swap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_prefix = root / "install"
            state_dir = root / "state"
            previous_package = install_prefix / "hermes_runtime.previous"
            previous_package.mkdir(parents=True)
            (previous_package / "old_runtime.py").write_text(
                "OLD = True\n",
                encoding="utf-8",
            )

            with patch.dict(
                "os.environ",
                {"TINYHAT_RUNTIME_PREFIX": str(install_prefix)},
            ):
                activated = activate_staged_runtime_code(state_dir=state_dir)

            self.assertFalse(activated)
            self.assertTrue(
                (install_prefix / "hermes_runtime" / "old_runtime.py").is_file()
            )
            self.assertFalse(previous_package.exists())

    def test_external_bootstrap_recovers_before_package_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_prefix = root / "install"
            next_package = install_prefix / "hermes_runtime.next"
            next_package.mkdir(parents=True)
            (next_package / "new_runtime.py").write_text(
                "NEW = True\n",
                encoding="utf-8",
            )

            bootstrap_recover_interrupted_package_swap(install_prefix)

            self.assertTrue(
                (install_prefix / "hermes_runtime" / "new_runtime.py").is_file()
            )
            self.assertFalse(next_package.exists())

    def test_safe_tarball_extraction_rejects_escape_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive_path = root / "runtime.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                for name in (
                    "repo-good/hermes_runtime/__init__.py",
                    "../outside.py",
                ):
                    payload = b"VALUE = True\n"
                    item = tarfile.TarInfo(name)
                    item.size = len(payload)
                    archive.addfile(item, io.BytesIO(payload))

            with self.assertRaisesRegex(ValueError, "unsafe tarball path"):
                _safe_extract_tarball(archive_path, root / "extract")

            absolute_archive = root / "absolute.tar.gz"
            with tarfile.open(absolute_archive, "w:gz") as archive:
                payload = b"VALUE = True\n"
                item = tarfile.TarInfo("/tmp/outside.py")
                item.size = len(payload)
                archive.addfile(item, io.BytesIO(payload))

            with self.assertRaisesRegex(ValueError, "unsafe tarball path"):
                _safe_extract_tarball(absolute_archive, root / "absolute-extract")

    def test_prepare_staged_runtime_downloads_target_sha_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            target_sha = "a" * 40
            downloaded_refs: list[str] = []

            def fake_download(download_ref: str, destination: Path) -> None:
                downloaded_refs.append(download_ref)
                (destination / "hermes_runtime").mkdir(parents=True)
                (destination / "hermes_runtime" / "__init__.py").write_text(
                    '__version__ = "0.0.3"\n',
                    encoding="utf-8",
                )

            with patch.dict(
                "os.environ",
                {"TINYHAT_RUNTIME_UPDATE_SOURCE_DIR": ""},
            ), patch(
                "hermes_runtime.update_artifacts._download_source_ref",
                side_effect=fake_download,
            ):
                staged = prepare_staged_runtime(
                    state_dir=state_dir,
                    target_ref="channels/lts",
                    target_sha=target_sha,
                )

            self.assertEqual(downloaded_refs, [target_sha])
            self.assertEqual(staged["source"]["ref"], "channels/lts")
            self.assertEqual(staged["source"]["download_ref"], target_sha)
            self.assertEqual(staged["source"]["target_sha"], target_sha)
            self.assertTrue((staged_package_dir(state_dir) / "__init__.py").is_file())

    def test_prepare_staged_runtime_rejects_unsafe_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            with self.assertRaisesRegex(ValueError, "unsafe path"):
                prepare_staged_runtime(
                    state_dir=state_dir,
                    target_ref="../main",
                    target_sha=None,
                )
            with self.assertRaisesRegex(ValueError, "target_sha"):
                prepare_staged_runtime(
                    state_dir=state_dir,
                    target_ref="v0.0.3",
                    target_sha="not a sha",
                )

    def test_prepare_staged_runtime_stages_bootstrap_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            source_root = root / "source"
            (source_root / "hermes_runtime").mkdir(parents=True)
            (source_root / "hermes_runtime" / "__init__.py").write_text(
                '__version__ = "0.0.3"\n',
                encoding="utf-8",
            )
            (source_root / "tinyhat_hermes_runtime_bootstrap.py").write_text(
                "BOOTSTRAP = True\n",
                encoding="utf-8",
            )

            with patch.dict(
                "os.environ",
                {"TINYHAT_RUNTIME_UPDATE_SOURCE_DIR": str(source_root)},
            ):
                staged = prepare_staged_runtime(
                    state_dir=state_dir,
                    target_ref="v0.0.3",
                    target_sha=None,
                )

            self.assertTrue(staged["bootstrap_staged"])
            self.assertEqual(
                Path(staged["bootstrap_file"]),
                staged_bootstrap_file(state_dir),
            )

    def test_restart_runtime_service_requests_restart(self) -> None:
        ctx = SimpleNamespace(restart_requested=False)

        result = asyncio.run(
            run_command(ctx, {"kind": "restart_runtime_service", "spec": {}})
        )

        self.assertTrue(ctx.restart_requested)
        self.assertEqual(result["message"], "runtime service restart requested")
        self.assertEqual(
            result["restart_target"], "tinyhat-hermes-runtime.service"
        )
        self.assertEqual(result["effect"], "after_command_result")

    def test_update_status_reports_current_and_staged_versions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            ctx = SimpleNamespace(
                state_dir=state_dir,
                staged_version_file=state_dir / "staged" / "VERSION",
                staged_metadata_file=state_dir / "staged" / "metadata.json",
                activation_marker=state_dir / "ACTIVATE_ON_RESTART",
                current_version=lambda: "v0.0.1",
                current_commit_sha=lambda: None,
                staged_version=lambda: "v0.20.0-dev.20260625T173000Z.next",
            )
            ctx.staged_metadata_file.parent.mkdir(parents=True)
            ctx.staged_metadata_file.write_text(
                '{"target_ref":"v0.20.0-dev.20260625T173000Z.next","channel":"custom"}\n',
                encoding="utf-8",
            )

            status = asyncio.run(run_command(ctx, {"kind": "update_status"}))

            self.assertEqual(status["current_version"], "v0.0.1")
            self.assertEqual(
                status["ready_updates"][0]["version"],
                "v0.20.0-dev.20260625T173000Z.next",
            )
            self.assertEqual(
                status["ready_updates"][0]["activation"],
                "requires_activate_update",
            )

            ctx.activation_marker.write_text(
                "v0.20.0-dev.20260625T173000Z.next\n",
                encoding="utf-8",
            )
            status = asyncio.run(run_command(ctx, {"kind": "update_status"}))
            self.assertEqual(
                status["ready_updates"][0]["activation"],
                "after_runtime_restart",
            )

    def test_update_status_marks_cached_check_stale_after_current_version_changes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "updates").mkdir()
            (state_dir / "updates" / "last_check.json").write_text(
                json.dumps(
                    {
                        "schema": "tinyhat_hermes_update_check_v1",
                        "status": "dev_ref_check",
                        "channel": "lts",
                        "target_ref": "v0.0.3",
                        "current_version": "v0.0.2",
                        "current_sha": None,
                        "update_available": True,
                    }
                ),
                encoding="utf-8",
            )
            ctx = SimpleNamespace(
                state_dir=state_dir,
                staged_metadata_file=state_dir / "staged" / "metadata.json",
                activation_marker=state_dir / "ACTIVATE_ON_RESTART",
                current_version=lambda: "v0.0.3",
                current_commit_sha=lambda: None,
                staged_version=lambda: None,
            )

            status = asyncio.run(run_command(ctx, {"kind": "update_status"}))

            last_check = status["last_update_check"]
            self.assertEqual(status["current_version"], "v0.0.3")
            self.assertTrue(last_check["stale"])
            self.assertEqual(
                last_check["stale_reason"],
                "current_version_changed_since_check",
            )
            self.assertEqual(last_check["checked_current_version"], "v0.0.2")
            self.assertEqual(last_check["live_current_version"], "v0.0.3")
            self.assertTrue(last_check["previous_update_available"])
            self.assertIsNone(last_check["update_available"])

    def test_update_status_keeps_cached_check_when_final_versions_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "updates").mkdir()
            (state_dir / "updates" / "last_check.json").write_text(
                json.dumps(
                    {
                        "schema": "tinyhat_hermes_update_check_v1",
                        "status": "dev_ref_check",
                        "channel": "lts",
                        "target_ref": "v0.0.3",
                        "current_version": "0.0.3",
                        "current_sha": None,
                        "update_available": False,
                    }
                ),
                encoding="utf-8",
            )
            ctx = SimpleNamespace(
                state_dir=state_dir,
                staged_metadata_file=state_dir / "staged" / "metadata.json",
                activation_marker=state_dir / "ACTIVATE_ON_RESTART",
                current_version=lambda: "v0.0.3",
                current_commit_sha=lambda: None,
                staged_version=lambda: None,
            )

            status = asyncio.run(run_command(ctx, {"kind": "update_status"}))

            last_check = status["last_update_check"]
            self.assertNotIn("stale", last_check)
            self.assertFalse(last_check["update_available"])

    def test_update_status_marks_legacy_cached_check_without_state_stale(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "updates").mkdir()
            (state_dir / "updates" / "last_check.json").write_text(
                json.dumps(
                    {
                        "schema": "tinyhat_hermes_update_check_v1",
                        "status": "dev_ref_check",
                        "channel": "lts",
                        "target_ref": "v0.0.4",
                        "update_available": True,
                    }
                ),
                encoding="utf-8",
            )
            ctx = SimpleNamespace(
                state_dir=state_dir,
                staged_metadata_file=state_dir / "staged" / "metadata.json",
                activation_marker=state_dir / "ACTIVATE_ON_RESTART",
                current_version=lambda: "v0.0.5",
                current_commit_sha=lambda: None,
                staged_version=lambda: None,
            )

            status = asyncio.run(run_command(ctx, {"kind": "update_status"}))

            last_check = status["last_update_check"]
            self.assertTrue(last_check["stale"])
            self.assertEqual(
                last_check["stale_reason"],
                "cached_check_missing_current_state",
            )
            self.assertIsNone(last_check["checked_current_version"])
            self.assertEqual(last_check["live_current_version"], "v0.0.5")
            self.assertTrue(last_check["previous_update_available"])
            self.assertIsNone(last_check["update_available"])

    def test_update_status_cleans_live_version_before_cache_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "updates").mkdir()
            (state_dir / "updates" / "last_check.json").write_text(
                json.dumps(
                    {
                        "schema": "tinyhat_hermes_update_check_v1",
                        "status": "dev_ref_check",
                        "channel": "lts",
                        "target_ref": "v0.0.5",
                        "current_version": "v0.0.5",
                        "current_sha": None,
                        "update_available": False,
                    }
                ),
                encoding="utf-8",
            )
            ctx = SimpleNamespace(
                state_dir=state_dir,
                staged_metadata_file=state_dir / "staged" / "metadata.json",
                activation_marker=state_dir / "ACTIVATE_ON_RESTART",
                current_version=lambda: " 0.0.5\n",
                current_commit_sha=lambda: None,
                staged_version=lambda: None,
            )

            status = asyncio.run(run_command(ctx, {"kind": "update_status"}))

            last_check = status["last_update_check"]
            self.assertNotIn("stale", last_check)
            self.assertFalse(last_check["update_available"])

    def test_running_version_reads_imported_runtime_code(self) -> None:
        result = asyncio.run(run_command(SimpleNamespace(), {"kind": "running_version"}))

        self.assertEqual(result["schema"], "tinyhat_hermes_running_version_v1")
        self.assertEqual(result["code_version"], __version__)
        self.assertTrue(result["module_file"].endswith("hermes_runtime/__init__.py"))
        self.assertIn("imported by the Python process", result["proof"])

    def test_recent_commands_returns_local_ledger_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            append_entry(
                state_dir=state_dir,
                command={
                    "command_id": "cmd-1",
                    "kind": "ping",
                    "spec": {},
                    "created_at": "2026-06-25T10:00:00Z",
                },
                status="applied",
                phase="ping",
                result={"message": "pong"},
                started_at="2026-06-25T10:00:01Z",
                completed_at="2026-06-25T10:00:02Z",
            )
            ctx = SimpleNamespace(state_dir=state_dir)

            result = asyncio.run(
                run_command(ctx, {"kind": "recent_commands", "spec": {"limit": 5}})
            )

            self.assertEqual(result["count"], 1)
            self.assertEqual(result["commands"][0]["command_id"], "cmd-1")
            self.assertEqual(report(state_dir=state_dir)["commands"][0]["kind"], "ping")

    def test_setup_snapshot_reports_install_and_service_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_root = root / "opt" / "tinyhat-hermes-runtime"
            state_dir = root / "var" / "lib" / "tinyhat-hermes-runtime"
            (install_root / "env").mkdir(parents=True)
            (state_dir / "current").mkdir(parents=True)
            (install_root / "INSTALL_REF").write_text("channels/lts\n", encoding="utf-8")
            (install_root / "env" / "runtime.env").write_text(
                "TINYHAT_LOCAL_DEV_TOKEN=secret\n",
                encoding="utf-8",
            )
            (state_dir / "current" / "VERSION").write_text("0.0.1\n", encoding="utf-8")
            (state_dir / "current" / "COMMIT_SHA").write_text(
                "a" * 40 + "\n",
                encoding="utf-8",
            )
            ctx = SimpleNamespace(state_dir=state_dir)

            def fake_systemctl(args: list[str]) -> dict:
                if args[0] == "show":
                    return {
                        "systemctl_available": True,
                        "ok": True,
                        "stdout": (
                            "ActiveState=active\n"
                            "Restart=always\n"
                            "Nice=-5\n"
                            "OOMScoreAdjust=-900\n"
                        ),
                        "stderr": "",
                    }
                return {
                    "systemctl_available": True,
                    "ok": True,
                    "stdout": "[Service]\nRestart=always\n",
                    "stderr": "",
                }

            with patch.dict(
                "os.environ",
                {"TINYHAT_RUNTIME_PREFIX": str(install_root)},
            ), patch(
                "hermes_runtime.commands.setup_snapshot._run_systemctl",
                side_effect=fake_systemctl,
            ):
                snapshot = asyncio.run(
                    run_command(ctx, {"kind": "setup_snapshot", "spec": {}})
                )

            self.assertEqual(snapshot["schema"], "tinyhat_hermes_setup_snapshot_v1")
            self.assertEqual(
                snapshot["install"]["install_ref"]["value"],
                "channels/lts",
            )
            self.assertEqual(
                snapshot["state"]["current_version"]["value"],
                "0.0.1",
            )
            self.assertEqual(
                snapshot["service"]["properties"]["OOMScoreAdjust"],
                "-900",
            )
            self.assertEqual(snapshot["warnings"], [])
            self.assertNotIn("secret", str(snapshot))

    def test_ledger_write_failure_still_reports_command_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            platform = FakePlatform()
            ctx = SimpleNamespace(
                platform=platform,
                state_dir=Path(tmp),
                computer_id="local-dev",
            )

            with patch(
                "hermes_runtime.main.append_entry",
                side_effect=OSError("ledger unavailable"),
            ):
                asyncio.run(
                    _run_one_command(
                        ctx,
                        {
                            "command_id": "cmd-ledger-fails",
                            "idempotency_key": "idem-ledger-fails",
                            "kind": "ping",
                            "spec": {},
                        },
                    )
                )

            self.assertEqual(
                platform.posts[0][0],
                "/hapi/v1/computers/local-dev/runtime-command/result",
            )
            reported = platform.posts[0][1]["result"]
            self.assertEqual(reported["status"], "applied")
            self.assertEqual(reported["result"], {"message": "pong"})

    def test_check_update_writes_last_result_without_staging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            platform = FakePlatform()
            ctx = SimpleNamespace(
                platform=platform,
                state_dir=state_dir,
                current_version=lambda: "v0.20.0-dev.20260625T173000Z.old",
                current_commit_sha=lambda: None,
            )

            with patch(
                "hermes_runtime.update_check._fetch_github_commit",
                return_value={
                    "ok": True,
                    "status": "ok",
                    "sha": "a" * 40,
                    "html_url": "https://github.com/tinyloophub/tinyhat--runtimes--hermes/commit/"
                    + "a" * 40,
                },
            ):
                checked = asyncio.run(
                    run_command(
                        ctx,
                        {
                            "kind": "check_update",
                            "spec": {
                                "channel": "custom",
                                "target_ref": "v0.20.0-dev.20260625T173000Z.next",
                            },
                        },
                    )
                )

            self.assertEqual(checked["status"], "ok")
            self.assertTrue(checked["update_available"])
            self.assertEqual(
                checked["target_ref"], "v0.20.0-dev.20260625T173000Z.next"
            )
            self.assertTrue((state_dir / "updates" / "last_check.json").is_file())
            self.assertFalse((state_dir / "staged" / "VERSION").exists())
            self.assertEqual(
                platform.posts[0][0],
                "/hapi/v1/computers/local-dev/update-check-results/v1",
            )
            self.assertEqual(
                platform.posts[0][1]["result"]["target_ref"],
                "v0.20.0-dev.20260625T173000Z.next",
            )
            self.assertNotIn("run_id", checked)

    def test_check_update_is_not_available_when_current_sha_matches_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            platform = FakePlatform()
            target_sha = "a" * 40
            ctx = SimpleNamespace(
                platform=platform,
                state_dir=state_dir,
                computer_id="987",
                current_version=lambda: "channels/lts",
                current_commit_sha=lambda: target_sha,
            )

            with patch(
                "hermes_runtime.update_check._resolve_channel_final_target",
                return_value={
                    "ok": True,
                    "status": "ok",
                    "target_ref": "v0.0.44",
                    "sha": target_sha,
                    "html_url": "https://github.com/tinyloophub/tinyhat--runtimes--hermes/commit/"
                    + target_sha,
                },
            ):
                checked = asyncio.run(
                    run_command(
                        ctx,
                        {
                            "kind": "check_update",
                            "spec": {
                                "channel": "lts",
                                "target_ref": "channels/lts",
                            },
                        },
                    )
                )

            self.assertFalse(checked["update_available"])
            self.assertEqual(checked["current_sha"], target_sha)
            self.assertEqual(checked["requested_target_ref"], "channels/lts")
            self.assertEqual(checked["target_ref"], "v0.0.44")
            self.assertEqual(
                platform.posts[0][0],
                "/hapi/v1/computers/local-dev/update-check-results/v1",
            )

    def test_check_update_is_not_available_when_current_ref_matches_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            platform = FakePlatform()
            current_ref = "v0.20.0-dev.20260625T153259Z.local-dev-check"
            ctx = SimpleNamespace(
                platform=platform,
                state_dir=state_dir,
                current_version=lambda: current_ref,
                current_commit_sha=lambda: None,
            )

            with patch(
                "hermes_runtime.update_check._fetch_github_commit",
                return_value={
                    "ok": True,
                    "status": "ok",
                    "sha": "b" * 40,
                    "html_url": "https://github.com/tinyloophub/tinyhat--runtimes--hermes/commit/"
                    + "b" * 40,
                },
            ):
                checked = asyncio.run(
                    run_command(
                        ctx,
                        {
                            "kind": "check_update",
                            "spec": {
                                "channel": "custom",
                                "target_ref": current_ref,
                            },
                        },
                    )
                )

            self.assertFalse(checked["update_available"])
            self.assertEqual(checked["target_ref"], current_ref)

    def test_check_update_uses_code_version_when_current_ref_is_channel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            platform = FakePlatform()
            target_sha = "6" * 40
            ctx = SimpleNamespace(
                platform=platform,
                state_dir=state_dir,
                computer_id="987",
                current_version=lambda: "channels/lts",
                current_commit_sha=lambda: "4" * 40,
            )

            with patch(
                "hermes_runtime.commands.check_update.__version__",
                "0.0.39",
            ), patch(
                "hermes_runtime.update_check._fetch_github_commit",
                return_value={
                    "ok": True,
                    "status": "ok",
                    "sha": target_sha,
                    "html_url": "https://github.com/tinyloophub/tinyhat--runtimes--hermes/commit/"
                    + target_sha,
                },
            ):
                checked = asyncio.run(
                    run_command(
                        ctx,
                        {
                            "kind": "check_update",
                            "spec": {
                                "channel": "lts",
                                "target_ref": "v0.0.40",
                            },
                        },
                    )
                )

            self.assertEqual(checked["current_version"], "channels/lts")
            self.assertEqual(checked["current_code_version"], "0.0.39")
            self.assertEqual(checked["target_ref"], "v0.0.40")
            self.assertEqual(checked["target_sha"], target_sha)
            self.assertFalse(checked["current_matches_target"])
            self.assertTrue(checked["target_final_version_is_newer"])
            self.assertEqual(checked["decision"], "newer_final_release")
            self.assertTrue(checked["update_available"])
            self.assertTrue(platform.posts[0][1]["result"]["update_available"])

    def test_check_update_uses_dev_ref_check_for_local_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            platform = FakePlatform()
            ctx = SimpleNamespace(
                platform=platform,
                state_dir=state_dir,
                current_version=lambda: "v0.0.1",
                current_commit_sha=lambda: None,
            )

            with patch.dict("os.environ", {"TINYHAT_LOCAL_DEV_TOKEN": "dev-token"}):
                with patch(
                    "hermes_runtime.update_check._fetch_github_commit",
                    side_effect=AssertionError("local dev must not call GitHub"),
                ):
                    checked = asyncio.run(
                        run_command(
                            ctx,
                            {
                                "kind": "check_update",
                                "spec": {
                                    "channel": "lts",
                                    "target_ref": "v0.0.1",
                                },
                            },
                        )
                    )

            self.assertEqual(checked["status"], "dev_ref_check")
            self.assertFalse(checked["update_available"])
            self.assertEqual(checked["target_sha"], None)
            self.assertIn("Local dev", checked["detail"])

    def test_check_update_treats_bare_channel_selector_as_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            platform = FakePlatform()
            ctx = SimpleNamespace(
                platform=platform,
                state_dir=state_dir,
                current_version=lambda: "v0.0.7",
                current_commit_sha=lambda: None,
            )

            with patch.dict("os.environ", {"TINYHAT_LOCAL_DEV_TOKEN": "dev-token"}):
                with patch(
                    "hermes_runtime.update_check._fetch_github_commit",
                    side_effect=AssertionError("local dev must not call GitHub"),
                ):
                    checked = asyncio.run(
                        run_command(
                            ctx,
                            {
                                "kind": "check_update",
                                "spec": {
                                    "channel": "lts",
                                    "target_ref": "channels/lts",
                                },
                            },
                        )
                    )

            self.assertTrue(checked["channel_eligible"])
            self.assertIsNone(checked["target_final_version_is_newer"])
            self.assertFalse(checked["update_available"])
            self.assertEqual(
                checked["decision"], "channel_selector_needs_concrete_release"
            )

    def test_check_update_resolves_channel_version_to_concrete_final_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            platform = FakePlatform()
            release_tag_sha = "7" * 40
            ctx = SimpleNamespace(
                platform=platform,
                state_dir=state_dir,
                current_version=lambda: "v0.0.7",
                current_commit_sha=lambda: "6" * 40,
            )

            with patch(
                "hermes_runtime.update_check._resolve_channel_final_target",
                return_value={
                    "ok": True,
                    "status": "ok",
                    "target_ref": "v0.0.8",
                    "sha": release_tag_sha,
                    "html_url": "https://github.com/tinyloophub/tinyhat--runtimes--hermes/commit/"
                    + release_tag_sha,
                },
            ):
                checked = asyncio.run(
                    run_command(
                        ctx,
                        {
                            "kind": "check_update",
                            "spec": {
                                "channel": "lts",
                                "target_ref": "channels/lts",
                            },
                        },
                    )
                )

            self.assertTrue(checked["channel_eligible"])
            self.assertEqual(checked["current_version"], "v0.0.7")
            self.assertEqual(checked["requested_target_ref"], "channels/lts")
            self.assertEqual(checked["target_ref"], "v0.0.8")
            self.assertEqual(checked["target_sha"], release_tag_sha)
            self.assertTrue(checked["target_final_version_is_newer"])
            self.assertFalse(checked["current_matches_target"])
            self.assertTrue(checked["update_available"])
            self.assertEqual(checked["decision"], "newer_final_release")

    def test_check_update_reports_explicit_channel_version_resolution_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            platform = FakePlatform()
            ctx = SimpleNamespace(
                platform=platform,
                state_dir=state_dir,
                current_version=lambda: "v0.0.7",
                current_commit_sha=lambda: "6" * 40,
            )

            with patch(
                "hermes_runtime.update_check._resolve_channel_final_target",
                return_value={
                    "ok": False,
                    "status": "channel_version_invalid",
                    "message": "Channel VERSION is not a final release",
                },
            ):
                checked = asyncio.run(
                    run_command(
                        ctx,
                        {
                            "kind": "check_update",
                            "spec": {
                                "channel": "lts",
                                "target_ref": "channels/lts",
                            },
                        },
                    )
                )

            self.assertEqual(checked["status"], "channel_version_invalid")
            self.assertEqual(checked["requested_target_ref"], "channels/lts")
            self.assertEqual(checked["target_ref"], "channels/lts")
            self.assertEqual(checked["decision"], "target_unavailable")
            self.assertFalse(checked["update_available"])

    def test_check_update_matches_final_versions_with_or_without_v_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            platform = FakePlatform()
            ctx = SimpleNamespace(
                platform=platform,
                state_dir=state_dir,
                current_version=lambda: "0.0.7",
                current_commit_sha=lambda: None,
            )

            with patch.dict("os.environ", {"TINYHAT_LOCAL_DEV_TOKEN": "dev-token"}):
                checked = asyncio.run(
                    run_command(
                        ctx,
                        {
                            "kind": "check_update",
                            "spec": {
                                "channel": "lts",
                                "target_ref": "v0.0.7",
                            },
                        },
                    )
                )

            self.assertTrue(checked["channel_eligible"])
            self.assertFalse(checked["update_available"])
            self.assertEqual(checked["decision"], "current_matches_target")

    def test_lts_check_does_not_treat_dev_tag_as_available_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            platform = FakePlatform()
            ctx = SimpleNamespace(
                platform=platform,
                state_dir=state_dir,
                current_version=lambda: "v0.0.1",
                current_commit_sha=lambda: None,
            )

            with patch.dict("os.environ", {"TINYHAT_LOCAL_DEV_TOKEN": "dev-token"}):
                checked = asyncio.run(
                    run_command(
                        ctx,
                        {
                            "kind": "check_update",
                            "spec": {
                                "channel": "lts",
                                "target_ref": "v0.0.2-dev.20260625T173000Z.smoke",
                            },
                        },
                    )
                )

            self.assertFalse(checked["channel_eligible"])
            self.assertFalse(checked["update_available"])
            self.assertEqual(
                checked["target_ref"], "v0.0.2-dev.20260625T173000Z.smoke"
            )

    def test_check_update_rejects_older_final_tag_for_bare_current_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            platform = FakePlatform()
            ctx = SimpleNamespace(
                platform=platform,
                state_dir=state_dir,
                current_version=lambda: "0.0.2",
                current_commit_sha=lambda: None,
            )

            with patch.dict("os.environ", {"TINYHAT_LOCAL_DEV_TOKEN": "dev-token"}):
                checked = asyncio.run(
                    run_command(
                        ctx,
                        {
                            "kind": "check_update",
                            "spec": {
                                "channel": "latest",
                                "target_ref": "v0.0.1",
                            },
                        },
                    )
                )

            self.assertTrue(checked["channel_eligible"])
            self.assertFalse(checked["target_final_version_is_newer"])
            self.assertFalse(checked["update_available"])

    def test_check_update_platform_report_failure_is_nonfatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            platform = FakePlatform()
            platform.fail_posts = True
            ctx = SimpleNamespace(
                platform=platform,
                state_dir=state_dir,
                current_version=lambda: "v0.0.1",
                current_commit_sha=lambda: None,
            )

            with patch.dict("os.environ", {"TINYHAT_LOCAL_DEV_TOKEN": "dev-token"}):
                checked = asyncio.run(
                    run_command(
                        ctx,
                        {
                            "kind": "check_update",
                            "spec": {
                                "channel": "lts",
                                "target_ref": "v0.0.2",
                            },
                        },
                    )
                )

            self.assertEqual(checked["message"], "update check complete")
            self.assertTrue(checked["update_available"])
            self.assertFalse(checked["report_delivered"])
            self.assertEqual(checked["report_error"], "post failed")
            self.assertTrue((state_dir / "updates" / "last_check.json").is_file())

    def test_heartbeat_metrics_do_not_embed_update_check_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "updates").mkdir()
            (state_dir / "updates" / "last_check.json").write_text(
                '{"target_ref":"v0.20.0-dev.20260625T173000Z.next"}',
                encoding="utf-8",
            )
            ctx = SimpleNamespace(
                state_dir=state_dir,
                started_at=0.0,
                current_version=lambda: "v0.20.0-dev.20260625T173000Z.old",
                staged_version=lambda: None,
            )

            metrics = _heartbeat_metrics(ctx, status="running")

            self.assertNotIn("update_check", metrics["hermes_runtime"])

    def test_startup_activation_failure_is_recorded_for_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)

            class BrokenActivationContext(SimpleNamespace):
                @property
                def activation_error_file(self) -> Path:
                    return state_dir / "updates" / "last_activation_error.json"

                def activate_staged_on_startup(self) -> dict[str, Any] | None:
                    raise RuntimeError("copy failed")

            ctx = BrokenActivationContext(
                state_dir=state_dir,
                started_at=0.0,
                current_version=lambda: "v0.0.2",
                current_commit_sha=lambda: None,
                staged_version=lambda: "v0.0.3",
            )

            activated = _safe_activate_staged_on_startup(ctx)
            metrics = _heartbeat_metrics(ctx, status="running")

            self.assertIsNone(activated)
            self.assertEqual(
                metrics["hermes_runtime"]["startup_activation_error"]["failure_code"],
                "RuntimeError",
            )

    def test_reexec_after_code_swap_uses_bootstrap_when_configured(self) -> None:
        calls: list[tuple[str, list[str]]] = []

        def fake_execv(executable: str, args: list[str]) -> None:
            calls.append((executable, args))
            raise RuntimeError("stop")

        with patch.dict(
            "os.environ",
            {"TINYHAT_RUNTIME_BOOTSTRAP": "/opt/tinyhat-hermes-runtime/bootstrap.py"},
        ), patch("hermes_runtime.main.os.execv", side_effect=fake_execv):
            with self.assertRaisesRegex(RuntimeError, "stop"):
                _reexec_after_code_swap({"version": "v0.0.3", "code_swapped": True})

        self.assertEqual(calls[0][1][1], "/opt/tinyhat-hermes-runtime/bootstrap.py")

    def test_scheduled_update_check_due_once_after_configured_local_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            config_dir = state_dir / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "update_check_time").write_text("02:35\n")
            (config_dir / "update_check_timezone").write_text("America/Los_Angeles\n")
            now = datetime(2026, 6, 25, 10, 0, tzinfo=timezone.utc)

            due, config, date_key = scheduled_check_due(
                state_dir=state_dir,
                now_utc=now,
            )

            self.assertTrue(due)
            self.assertEqual(config.local_time, "02:35")
            self.assertEqual(config.timezone, "America/Los_Angeles")
            self.assertEqual(date_key, "2026-06-25")
            (state_dir / "updates").mkdir()
            (state_dir / "updates" / "last_scheduled_check_date").write_text(
                date_key + "\n"
            )
            due_again, _config, _date_key = scheduled_check_due(
                state_dir=state_dir,
                now_utc=now,
            )
            self.assertFalse(due_again)

    def test_atomic_update_state_write_does_not_close_owned_descriptor_twice(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "updates" / "last_check.json"
            with patch("hermes_runtime.update_check.os.close") as close:
                _write_text_atomic(path, '{"status":"ok"}\n')

            self.assertEqual(path.read_text(encoding="utf-8"), '{"status":"ok"}\n')
            close.assert_not_called()

    def test_scheduled_update_check_posts_result_then_marks_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            config_dir = state_dir / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "update_check_time").write_text("00:00\n")
            (config_dir / "update_check_timezone").write_text("UTC\n")
            (config_dir / "update_check_ref").write_text("v0.0.40\n")
            platform = FakePlatform()
            ctx = SimpleNamespace(
                platform=platform,
                state_dir=state_dir,
                computer_id="scheduled",
                current_version=lambda: "channels/lts",
                current_commit_sha=lambda: "b" * 40,
            )

            with (
                patch("hermes_runtime.main.__version__", "0.0.39"),
                patch(
                    "hermes_runtime.update_check._fetch_github_commit",
                    return_value={
                        "ok": True,
                        "status": "ok",
                        "sha": "c" * 40,
                        "html_url": "https://github.com/tinyloophub/tinyhat--runtimes--hermes/commit/"
                        + "c" * 40,
                    },
                ),
                patch(
                    "hermes_runtime.update_check.tinyhat_plugin_status",
                    fake_plugin_update_status,
                ),
            ):
                result = asyncio.run(_scheduled_update_check(ctx))

            self.assertEqual(result["reason"], "scheduled")
            self.assertEqual(result["current_version"], "channels/lts")
            self.assertEqual(result["current_code_version"], "0.0.39")
            self.assertEqual(result["decision"], "newer_final_release")
            self.assertTrue(result["update_available"])
            self.assertEqual(
                platform.posts[0][0],
                "/hapi/v1/computers/local-dev/update-check-results/v1",
            )
            self.assertTrue(
                (state_dir / "updates" / "last_scheduled_check_date").is_file()
            )
            scheduled_date = (
                state_dir / "updates" / "last_scheduled_check_date"
            ).read_text(encoding="utf-8").strip()
            self.assertEqual(result["run_id"], f"scheduled:{scheduled_date}")
            self.assertEqual(result["scheduled_local_date"], scheduled_date)
            self.assertEqual(
                result["plugin_update_check"]["schema"],
                "tinyhat_hermes_plugin_update_check_v2",
            )
            installed_plugin = result["plugin_update_check"].get("installed", {})
            self.assertNotIn("plugin_dir", installed_plugin)
            self.assertNotIn("manifest", installed_plugin)
            self.assertFalse(
                (state_dir / "updates" / PENDING_SCHEDULED_RESULT_FILE).exists()
            )

    def test_scheduled_run_id_is_stable_for_retry_and_changes_next_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            with (
                patch.dict(
                    os.environ,
                    {"TINYHAT_LOCAL_DEV_TOKEN": "dev-token"},
                    clear=True,
                ),
                patch(
                    "hermes_runtime.update_check.tinyhat_plugin_status",
                    fake_plugin_update_status,
                ),
            ):
                first = asyncio.run(
                    run_update_check(
                        state_dir=state_dir,
                        current_version="v0.0.43",
                        reason="scheduled",
                        scheduled_local_date="2026-07-13",
                    )
                )
                retry = asyncio.run(
                    run_update_check(
                        state_dir=state_dir,
                        current_version="v0.0.43",
                        reason="scheduled",
                        scheduled_local_date="2026-07-13",
                    )
                )
                next_day = asyncio.run(
                    run_update_check(
                        state_dir=state_dir,
                        current_version="v0.0.43",
                        reason="scheduled",
                        scheduled_local_date="2026-07-14",
                    )
                )

        self.assertEqual(first["run_id"], "scheduled:2026-07-13")
        self.assertEqual(retry["run_id"], first["run_id"])
        self.assertNotEqual(next_day["run_id"], first["run_id"])
        self.assertLessEqual(len(first["run_id"]), 64)

    def test_scheduled_plugin_report_strips_repo_credentials_and_query(self) -> None:
        async def credentialed_plugin_status(
            _command: dict[str, Any],
        ) -> dict[str, Any]:
            return {
                "plugin_repo_url": (
                    "https://build-user:password-secret@github.com/"
                    "tinyhat-ai/tinyhat.git?token=query-secret#fragment-secret"
                ),
                "plugin_ref": "channels/lts",
                "installed": {
                    "installed": True,
                    "plugin_dir": "/private/plugin",
                    "manifest": "/private/plugin/plugin.yaml",
                    "source": {
                        "repo_url": (
                            "https://github.com/tinyhat-ai/tinyhat.git"
                            "?token=nested-secret"
                        ),
                        "ref": "channels/lts",
                        "commit": "a" * 40,
                        "credential": "raw-source-secret",
                    },
                },
                "installed_commit": "a" * 40,
                "target_commit": "b" * 40,
                "update_available": True,
                "decision": "target_ref_changed",
            }

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            with (
                patch(
                    "hermes_runtime.update_check._resolve_channel_final_target",
                    return_value={
                        "ok": True,
                        "status": "ok",
                        "target_ref": "v0.0.44",
                        "sha": "c" * 40,
                    },
                ),
                patch(
                    "hermes_runtime.update_check.tinyhat_plugin_status",
                    credentialed_plugin_status,
                ),
            ):
                result = asyncio.run(
                    run_update_check(
                        state_dir=state_dir,
                        current_version="v0.0.43",
                        reason="scheduled",
                        scheduled_local_date="2026-07-13",
                    )
                )

        plugin_result = result["plugin_update_check"]
        self.assertEqual(
            plugin_result["plugin_repo_url"],
            "https://github.com/tinyhat-ai/tinyhat.git",
        )
        self.assertNotIn("password-secret", repr(result))
        self.assertNotIn("query-secret", repr(result))
        self.assertNotIn("fragment-secret", repr(result))
        self.assertNotIn("nested-secret", repr(result))
        self.assertNotIn("raw-source-secret", repr(result))
        self.assertNotIn("/private/plugin", repr(result))
        self.assertEqual(
            set(plugin_result["installed"]["source"]),
            {"repo_url", "ref", "commit"},
        )

    def test_manual_plugin_check_keeps_sanitized_error_detail(self) -> None:
        private_repo = "https://build-user:secret@github.com/example/plugin.git"
        private_path = str(Path.home() / "private-plugin")

        async def failed_plugin_status(
            _command: dict[str, Any],
        ) -> dict[str, Any]:
            raise RuntimeError(f"checkout failed for {private_repo} at {private_path}")

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            with (
                patch.dict(
                    os.environ,
                    {
                        "TINYHAT_LOCAL_DEV_TOKEN": "dev-token",
                        "TINYHAT_PLUGIN_REPO_URL": private_repo,
                    },
                    clear=True,
                ),
                patch(
                    "hermes_runtime.update_check.tinyhat_plugin_status",
                    failed_plugin_status,
                ),
            ):
                manual = asyncio.run(
                    run_update_check(
                        state_dir=state_dir,
                        current_version="v0.0.43",
                        reason="admin_check_update",
                    )
                )
                scheduled = asyncio.run(
                    run_update_check(
                        state_dir=state_dir,
                        current_version="v0.0.43",
                        reason="scheduled",
                        scheduled_local_date="2026-07-13",
                    )
                )

        manual_error = manual["plugin_update_check"]["error"]
        self.assertIn("checkout failed", manual_error)
        self.assertIn("<plugin-repo>", manual_error)
        self.assertIn("<local-path>", manual_error)
        self.assertNotIn("secret", manual_error)
        self.assertNotIn(private_path, manual_error)
        self.assertEqual(
            scheduled["plugin_update_check"]["error"],
            "RuntimeError: plugin update check failed",
        )

    def test_failed_scheduled_result_delivery_survives_manual_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            config_dir = state_dir / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "update_check_time").write_text("00:00\n")
            (config_dir / "update_check_timezone").write_text("UTC\n")
            platform = FakePlatform()
            platform.fail_posts = True
            ctx = SimpleNamespace(
                platform=platform,
                state_dir=state_dir,
                current_version=lambda: "channels/lts",
                current_commit_sha=lambda: None,
            )

            with (
                patch(
                    "hermes_runtime.update_check._resolve_channel_final_target",
                    return_value={
                        "ok": True,
                        "status": "ok",
                        "target_ref": "v0.0.44",
                        "sha": "c" * 40,
                        "html_url": "https://github.com/tinyloophub/tinyhat--runtimes--hermes/commit/"
                        + "c" * 40,
                    },
                ),
                patch(
                    "hermes_runtime.update_check.tinyhat_plugin_status",
                    fake_plugin_update_status,
                ),
            ):
                with self.assertRaises(RuntimeError):
                    asyncio.run(_scheduled_update_check(ctx))

            self.assertFalse(
                (state_dir / "updates" / "last_scheduled_check_date").exists()
            )
            pending_path = state_dir / "updates" / PENDING_SCHEDULED_RESULT_FILE
            saved_result = json.loads(pending_path.read_text(encoding="utf-8"))

            with (
                patch.dict(
                    os.environ,
                    {"TINYHAT_LOCAL_DEV_TOKEN": "dev-token"},
                    clear=True,
                ),
                patch(
                    "hermes_runtime.update_check.tinyhat_plugin_status",
                    fake_plugin_update_status,
                ),
            ):
                manual_result = asyncio.run(
                    run_update_check(
                        state_dir=state_dir,
                        current_version="channels/lts",
                        spec={"channel": "custom", "target_ref": "manual-check"},
                        reason="admin_check_update",
                    )
                )
            latest_result = json.loads(
                (state_dir / "updates" / "last_check.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(latest_result, manual_result)
            self.assertEqual(
                manual_result["plugin_update_check"]["schema"],
                "tinyhat_hermes_plugin_update_check_v1",
            )
            self.assertNotEqual(latest_result["reason"], saved_result["reason"])
            self.assertEqual(
                json.loads(pending_path.read_text(encoding="utf-8")),
                saved_result,
            )

            platform.fail_posts = False
            with (
                patch(
                    "hermes_runtime.main.mark_scheduled_check_started",
                    side_effect=OSError("date marker unavailable"),
                ),
                patch(
                    "hermes_runtime.update_check._fetch_github_commit",
                    side_effect=AssertionError("runtime target was re-resolved"),
                ),
                patch(
                    "hermes_runtime.update_check.tinyhat_plugin_status",
                    side_effect=AssertionError("plugin target was re-resolved"),
                ),
            ):
                with self.assertRaisesRegex(OSError, "date marker unavailable"):
                    asyncio.run(_scheduled_update_check(ctx))

            self.assertTrue(pending_path.is_file())
            self.assertFalse(
                (state_dir / "updates" / "last_scheduled_check_date").exists()
            )
            with (
                patch(
                    "hermes_runtime.update_check._fetch_github_commit",
                    side_effect=AssertionError("runtime target was re-resolved"),
                ),
                patch(
                    "hermes_runtime.update_check.tinyhat_plugin_status",
                    side_effect=AssertionError("plugin target was re-resolved"),
                ),
            ):
                retried = asyncio.run(_scheduled_update_check(ctx))

            self.assertEqual(retried, saved_result)
            self.assertEqual(platform.posts[-1][1]["result"], saved_result)
            self.assertTrue(
                (state_dir / "updates" / "last_scheduled_check_date").is_file()
            )
            self.assertFalse(pending_path.exists())

    def test_unknown_command_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            asyncio.run(run_command(SimpleNamespace(), {"kind": "shell"}))


if __name__ == "__main__":
    import unittest

    unittest.main()
