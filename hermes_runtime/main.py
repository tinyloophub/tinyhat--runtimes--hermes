"""Heartbeat loop for the Tinyhat Hermes runtime foundation."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hermes_runtime import __version__
from hermes_runtime.client import (
    CachedGoogleIdentityToken,
    PlatformClient,
    PlatformError,
)
from hermes_runtime.commands import run_command
from hermes_runtime.local_ledger import append_entry, utc_now_iso
from hermes_runtime.platform_paths import context_computer_api_path
from hermes_runtime.update_check import (
    mark_scheduled_check_started,
    run_update_check,
    scheduled_check_due,
)
from hermes_runtime.update_artifacts import activate_staged_runtime_code
from hermes_runtime.update_artifacts import BOOTSTRAP_FILENAME
from hermes_runtime.update_artifacts import staged_runtime_dir

STATE_SCHEMA = "tinyhat_hermes_runtime_v1"
RESULT_SCHEMA = "tiny_runtime_command_result_v1"
DEFAULT_STATE_DIR = "/var/lib/tinyhat-hermes-runtime"
DEFAULT_CURRENT_VERSION = "0.0.1"
DEFAULT_UNASSIGNED_HEARTBEAT_INTERVAL_SECONDS = 1.0
DEFAULT_ASSIGNED_HEARTBEAT_INTERVAL_SECONDS = 10.0
ASSIGNED_PLATFORM_STATES = {"assigned", "active"}


@dataclass
class RuntimeContext:
    platform: PlatformClient
    state_dir: Path
    started_at: float
    computer_id: str = "local-dev"
    platform_auth: str = "local_dev"
    platform_state: str = "provisioning"
    restart_requested: bool = False
    update_check_task: asyncio.Task[dict[str, Any]] | None = None
    command_task: asyncio.Task[None] | None = None
    command_id: str | None = None
    command_kind: str | None = None

    @property
    def current_version_file(self) -> Path:
        return self.state_dir / "current" / "VERSION"

    @property
    def current_commit_file(self) -> Path:
        return self.state_dir / "current" / "COMMIT_SHA"

    @property
    def staged_version_file(self) -> Path:
        return self.state_dir / "staged" / "VERSION"

    @property
    def staged_metadata_file(self) -> Path:
        return self.state_dir / "staged" / "metadata.json"

    @property
    def activation_marker(self) -> Path:
        return self.state_dir / "ACTIVATE_ON_RESTART"

    @property
    def activation_error_file(self) -> Path:
        return self.state_dir / "updates" / "last_activation_error.json"

    def ensure_state(self) -> None:
        (self.state_dir / "current").mkdir(parents=True, exist_ok=True)
        (self.state_dir / "staged").mkdir(parents=True, exist_ok=True)
        if not self.current_version_file.exists():
            initial_version = (
                os.getenv("TINYHAT_RUNTIME_INITIAL_VERSION") or DEFAULT_CURRENT_VERSION
            ).strip()
            self.current_version_file.write_text(
                (initial_version or DEFAULT_CURRENT_VERSION) + "\n",
                encoding="utf-8",
            )

    def current_version(self) -> str:
        self.ensure_state()
        return self.current_version_file.read_text(encoding="utf-8").strip()

    def current_commit_sha(self) -> str | None:
        if not self.current_commit_file.exists():
            return None
        value = self.current_commit_file.read_text(encoding="utf-8").strip()
        return value or None

    def staged_version(self) -> str | None:
        if not self.staged_version_file.exists():
            return None
        value = self.staged_version_file.read_text(encoding="utf-8").strip()
        return value or None

    def activate_staged_on_startup(self) -> dict[str, Any] | None:
        self.ensure_state()
        if not self.activation_marker.exists():
            return None
        staged = self.staged_version()
        if not staged:
            self.activation_marker.unlink(missing_ok=True)
            return None
        code_swapped = activate_staged_runtime_code(state_dir=self.state_dir)
        self.current_version_file.write_text(staged + "\n", encoding="utf-8")
        staged_sha = None
        if self.staged_metadata_file.exists():
            try:
                metadata = json.loads(
                    self.staged_metadata_file.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                metadata = {}
            if isinstance(metadata, dict):
                staged_sha = str(metadata.get("target_sha") or "").strip() or None
        if staged_sha:
            self.current_commit_file.write_text(staged_sha + "\n", encoding="utf-8")
        else:
            self.current_commit_file.unlink(missing_ok=True)
        self.staged_version_file.unlink(missing_ok=True)
        self.staged_metadata_file.unlink(missing_ok=True)
        staged_runtime = staged_runtime_dir(self.state_dir)
        if staged_runtime.exists():
            shutil.rmtree(staged_runtime, ignore_errors=True)
        self.activation_marker.unlink(missing_ok=True)
        self.activation_error_file.unlink(missing_ok=True)
        return {"version": staged, "code_swapped": code_swapped}


def _env(name: str, default: str | None = None) -> str:
    value = (os.getenv(name) or default or "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _heartbeat_interval_seconds(ctx: RuntimeContext) -> float:
    legacy_override = (os.getenv("TINYHAT_HEARTBEAT_INTERVAL_SECONDS") or "").strip()
    if legacy_override:
        return max(0.1, float(legacy_override))

    assigned_interval = float(
        os.getenv("TINYHAT_ASSIGNED_HEARTBEAT_INTERVAL_SECONDS")
        or DEFAULT_ASSIGNED_HEARTBEAT_INTERVAL_SECONDS
    )
    unassigned_interval = float(
        os.getenv("TINYHAT_UNASSIGNED_HEARTBEAT_INTERVAL_SECONDS")
        or DEFAULT_UNASSIGNED_HEARTBEAT_INTERVAL_SECONDS
    )
    state = (getattr(ctx, "platform_state", "") or "").strip().lower()
    if state in ASSIGNED_PLATFORM_STATES:
        return max(0.1, assigned_interval)
    return max(0.1, unassigned_interval)


def _heartbeat_metrics(ctx: RuntimeContext, *, status: str) -> dict[str, Any]:
    staged = ctx.staged_version()
    current_commit_sha = (
        ctx.current_commit_sha() if hasattr(ctx, "current_commit_sha") else None
    )
    runtime = {
        "schema": STATE_SCHEMA,
        "mode": getattr(ctx, "platform_auth", "local_dev"),
        "status": status,
        "runtime_version": __version__,
        "current_version": ctx.current_version(),
        "current_commit_sha": current_commit_sha,
        "staged_version": staged,
        "pid": os.getpid(),
        "uptime_seconds": int(time.monotonic() - ctx.started_at),
        "updated_at_unix": int(time.time()),
    }
    command_task = getattr(ctx, "command_task", None)
    if command_task is not None and not command_task.done():
        runtime["active_command"] = {
            "command_id": getattr(ctx, "command_id", None),
            "kind": getattr(ctx, "command_kind", None),
            "status": "running",
        }
    activation_error = _read_activation_error(ctx)
    if activation_error:
        runtime["startup_activation_error"] = activation_error
    return {
        "runtime_generation": "tiny_runtime",
        "hermes_runtime": runtime,
    }


def _read_activation_error(ctx: RuntimeContext) -> dict[str, Any] | None:
    activation_error_file = getattr(ctx, "activation_error_file", None)
    if activation_error_file is None:
        return None
    try:
        payload = json.loads(activation_error_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _record_activation_error(ctx: RuntimeContext, exc: Exception) -> dict[str, Any]:
    payload = {
        "message": str(exc),
        "failure_code": exc.__class__.__name__,
        "recorded_at": utc_now_iso(),
        "traceback": traceback.format_exc(limit=3),
    }
    ctx.activation_error_file.parent.mkdir(parents=True, exist_ok=True)
    ctx.activation_error_file.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload


def _safe_activate_staged_on_startup(ctx: RuntimeContext) -> dict[str, Any] | None:
    try:
        return ctx.activate_staged_on_startup()
    except Exception as exc:  # noqa: BLE001 - startup must reach heartbeat.
        payload = _record_activation_error(ctx, exc)
        print(
            f"staged runtime activation failed: {payload['failure_code']}: {payload['message']}",
            file=sys.stderr,
            flush=True,
        )
        return None


def _exec_args_after_code_swap() -> list[str]:
    bootstrap = (os.getenv("TINYHAT_RUNTIME_BOOTSTRAP") or "").strip()
    if bootstrap:
        return [sys.executable, bootstrap]
    if sys.argv and Path(sys.argv[0]).name == BOOTSTRAP_FILENAME:
        return [sys.executable, sys.argv[0], *sys.argv[1:]]
    return [sys.executable, "-m", "hermes_runtime.main"]


def _reexec_after_code_swap(activated: dict[str, Any] | None) -> None:
    if not activated or not activated.get("code_swapped"):
        return
    print("runtime code updated; re-executing process", flush=True)
    os.execv(sys.executable, _exec_args_after_code_swap())


async def _report_command_result(
    ctx: RuntimeContext,
    *,
    command: dict[str, Any],
    status: str,
    phase: str,
    result: dict[str, Any],
    failure_code: str | None = None,
) -> None:
    payload = {
        "schema": RESULT_SCHEMA,
        "command_id": command.get("command_id"),
        "idempotency_key": command.get("idempotency_key"),
        "kind": command.get("kind"),
        "status": status,
        "phase": phase,
        "failure_code": failure_code,
        "result": result,
    }
    await ctx.platform.post_json(
        context_computer_api_path(ctx, "runtime-command/result"),
        {"result": payload},
    )


def _append_local_command_entry(
    ctx: RuntimeContext,
    *,
    command: dict[str, Any],
    status: str,
    phase: str,
    result: dict[str, Any],
    started_at: str,
    completed_at: str,
    failure_code: str | None = None,
) -> None:
    try:
        append_entry(
            state_dir=ctx.state_dir,
            command=command,
            status=status,
            phase=phase,
            failure_code=failure_code,
            result=result,
            started_at=started_at,
            completed_at=completed_at,
        )
    except Exception as exc:  # noqa: BLE001 - local diagnostics must be best-effort.
        print(
            f"local command ledger write failed: {exc}",
            file=sys.stderr,
            flush=True,
        )


async def _run_one_command(ctx: RuntimeContext, command: dict[str, Any]) -> None:
    kind = command.get("kind")
    started_at = utc_now_iso()
    try:
        result = await run_command(ctx, command)
    except Exception as exc:  # noqa: BLE001 - command failures must be reported.
        completed_at = utc_now_iso()
        failure_result = {
            "message": str(exc),
            "traceback": traceback.format_exc(limit=3),
        }
        _append_local_command_entry(
            ctx,
            command=command,
            status="failed",
            phase="execute",
            failure_code=exc.__class__.__name__,
            result=failure_result,
            started_at=started_at,
            completed_at=completed_at,
        )
        await _report_command_result(
            ctx,
            command=command,
            status="failed",
            phase="execute",
            failure_code=exc.__class__.__name__,
            result=failure_result,
        )
        return
    completed_at = utc_now_iso()
    _append_local_command_entry(
        ctx,
        command=command,
        status="applied",
        phase=str(kind or "execute"),
        result=result,
        started_at=started_at,
        completed_at=completed_at,
    )
    await _report_command_result(
        ctx,
        command=command,
        status="applied",
        phase=str(kind or "execute"),
        result=result,
    )


def _consume_command_task(ctx: RuntimeContext) -> None:
    task = ctx.command_task
    if task is None or not task.done():
        return
    ctx.command_task = None
    ctx.command_id = None
    ctx.command_kind = None
    try:
        task.result()
    except Exception as exc:  # noqa: BLE001 - command runner logs/report best effort.
        print(f"runtime command task failed: {exc}", file=sys.stderr, flush=True)


def _maybe_start_command(ctx: RuntimeContext, command: dict[str, Any]) -> None:
    _consume_command_task(ctx)
    if ctx.command_task is not None:
        return
    ctx.command_id = str(command.get("command_id") or "").strip() or None
    ctx.command_kind = str(command.get("kind") or "").strip() or None
    ctx.command_task = asyncio.create_task(_run_one_command(ctx, command))


def _consume_update_check_task(ctx: RuntimeContext) -> None:
    task = ctx.update_check_task
    if task is None or not task.done():
        return
    ctx.update_check_task = None
    try:
        result = task.result()
    except Exception as exc:  # noqa: BLE001 - background failures are reported later.
        print(f"scheduled update check failed: {exc}", file=sys.stderr, flush=True)
        return
    target = result.get("target_ref")
    status = result.get("status")
    print(f"scheduled update check complete: {status} {target}", flush=True)


async def _scheduled_update_check(ctx: RuntimeContext) -> dict[str, Any]:
    due, _config, date_key = scheduled_check_due(state_dir=ctx.state_dir)
    if not due:
        return {"status": "skipped", "reason": "not_due"}
    result = await run_update_check(
        state_dir=ctx.state_dir,
        current_version=ctx.current_version(),
        current_sha=ctx.current_commit_sha(),
        reason="scheduled",
    )
    await ctx.platform.post_json(
        context_computer_api_path(ctx, "update-check-results/v1"),
        {"result": result},
    )
    mark_scheduled_check_started(state_dir=ctx.state_dir, date_key=date_key)
    return result


def _maybe_start_scheduled_update_check(ctx: RuntimeContext) -> None:
    _consume_update_check_task(ctx)
    if ctx.update_check_task is not None:
        return
    due, _config, date_key = scheduled_check_due(state_dir=ctx.state_dir)
    if not due:
        return
    ctx.update_check_task = asyncio.create_task(_scheduled_update_check(ctx))


async def _heartbeat_once(ctx: RuntimeContext) -> None:
    _consume_command_task(ctx)
    _maybe_start_scheduled_update_check(ctx)
    response = await ctx.platform.post_json(
        context_computer_api_path(ctx, "heartbeat"),
        {"metrics": _heartbeat_metrics(ctx, status="running")},
    )
    platform_state = response.get("state")
    if isinstance(platform_state, str) and platform_state.strip():
        ctx.platform_state = platform_state.strip()
    envelope = response.get("command")
    if not isinstance(envelope, dict) or not envelope:
        return
    command = envelope.get("command") if envelope.get("type") else envelope
    if isinstance(command, dict) and command:
        _maybe_start_command(ctx, command)


async def run() -> int:
    platform_url = _env("TINYHAT_PLATFORM_URL")
    local_dev_token = (os.getenv("TINYHAT_LOCAL_DEV_TOKEN") or "").strip()
    if local_dev_token:
        platform = PlatformClient(base_url=platform_url, token=local_dev_token)
        platform_auth = "local_dev"
    else:
        audience = (
            os.getenv("TINYHAT_COMPUTER_TOKEN_AUDIENCE") or ""
        ).strip() or platform_url
        platform = PlatformClient(
            base_url=platform_url,
            token_provider=CachedGoogleIdentityToken(audience=audience),
        )
        platform_auth = "gcloud"
    state_dir = Path(os.getenv("TINYHAT_RUNTIME_STATE_DIR") or DEFAULT_STATE_DIR)
    computer_id = (os.getenv("TINYHAT_COMPUTER_ID") or "local-dev").strip() or "local-dev"
    ctx = RuntimeContext(
        platform=platform,
        state_dir=state_dir,
        started_at=time.monotonic(),
        computer_id=computer_id,
        platform_auth=platform_auth,
    )
    activated = _safe_activate_staged_on_startup(ctx)
    if activated:
        print(
            f"activated staged runtime version {activated['version']}",
            flush=True,
        )
        _reexec_after_code_swap(activated)

    while True:
        try:
            await _heartbeat_once(ctx)
        except PlatformError as exc:
            print(f"heartbeat failed: {exc}", file=sys.stderr, flush=True)
        _consume_command_task(ctx)
        if ctx.restart_requested and ctx.command_task is None:
            print("restart requested after command settlement", flush=True)
            return 0
        await asyncio.sleep(_heartbeat_interval_seconds(ctx))


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
