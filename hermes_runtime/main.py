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
from hermes_runtime.commands.configure_telegram import (
    _active_gateway_foreground_generation,
    _compact_process,
    _gateway_log_path,
    _gateway_status_is_healthy,
)
from hermes_runtime.gateway_readiness import (
    gateway_status_reports_telegram_fatal,
    probe_functional_readiness,
    public_gateway_foreground_generation,
    public_gateway_runtime_generation,
    public_gateway_runtime_generation_same,
    read_gateway_runtime_generation,
)
from hermes_runtime.gateway_service import (
    GATEWAY_SERVICE_UNOWNED_REASONS,
    discover_gateway_service,
    public_gateway_generation,
)
from hermes_runtime.hermes_cli import find_hermes_binary, run_process
from hermes_runtime.local_ledger import append_entry, utc_now_iso
from hermes_runtime.platform_paths import context_computer_api_path
from hermes_runtime.runtime_env import env_file_candidates, read_env_values
from hermes_runtime.update_check import (
    clear_scheduled_result_for_retry,
    mark_scheduled_check_started,
    read_scheduled_result_for_retry,
    run_update_check,
    scheduled_check_due,
)
from hermes_runtime.update_artifacts import activate_staged_runtime_code
from hermes_runtime.update_artifacts import BOOTSTRAP_FILENAME
from hermes_runtime.update_artifacts import staged_runtime_dir

STATE_SCHEMA = "tinyhat_hermes_runtime_v1"
RESULT_SCHEMA = "tiny_runtime_command_result_v1"
GATEWAY_STATE_SCHEMA = "tinyhat_hermes_gateway_state_v1"
DEFAULT_STATE_DIR = "/var/lib/tinyhat-hermes-runtime"
DEFAULT_CURRENT_VERSION = "0.0.1"
DEFAULT_UNASSIGNED_HEARTBEAT_INTERVAL_SECONDS = 1.0
DEFAULT_ASSIGNED_HEARTBEAT_INTERVAL_SECONDS = 10.0
GATEWAY_STATE_PROBE_TIMEOUT_SECONDS = 15
GATEWAY_FUNCTIONAL_RECHECK_SECONDS = 60
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
    gateway_reconcile_task: asyncio.Task[None] | None = None
    gateway_reconciled: bool = False
    gateway_state: dict[str, Any] | None = None
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


_warned_invalid_interval_envs: set[str] = set()


def _interval_env_seconds(name: str, default: float | None) -> float | None:
    """Parse a float interval env var; a malformed value logs once and
    falls back to ``default`` instead of raising (the heartbeat loop must
    never die on operator-supplied configuration)."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        if name not in _warned_invalid_interval_envs:
            _warned_invalid_interval_envs.add(name)
            print(
                f"invalid {name}={raw[:50]!r}; falling back to default",
                file=sys.stderr,
                flush=True,
            )
        return default


def _heartbeat_interval_seconds(ctx: RuntimeContext) -> float:
    legacy_override = _interval_env_seconds("TINYHAT_HEARTBEAT_INTERVAL_SECONDS", None)
    if legacy_override is not None:
        return max(0.1, legacy_override)

    assigned_interval = _interval_env_seconds(
        "TINYHAT_ASSIGNED_HEARTBEAT_INTERVAL_SECONDS",
        DEFAULT_ASSIGNED_HEARTBEAT_INTERVAL_SECONDS,
    )
    unassigned_interval = _interval_env_seconds(
        "TINYHAT_UNASSIGNED_HEARTBEAT_INTERVAL_SECONDS",
        DEFAULT_UNASSIGNED_HEARTBEAT_INTERVAL_SECONDS,
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
    gateway_state = getattr(ctx, "gateway_state", None)
    if isinstance(gateway_state, dict) and gateway_state:
        runtime["gateway"] = gateway_state
    return {
        "runtime_generation": "tiny_runtime",
        "hermes_runtime": runtime,
    }


def _telegram_env_configured() -> bool:
    values = read_env_values(env_file_candidates(), names=["TELEGRAM_BOT_TOKEN"])
    return bool(
        (
            os.getenv("TELEGRAM_BOT_TOKEN")
            or values.get("TELEGRAM_BOT_TOKEN")
            or ""
        ).strip()
    )


def _gateway_state_payload(
    *,
    status: str,
    ready: bool | None,
    reason: str,
    observed_at_unix: int | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema": GATEWAY_STATE_SCHEMA,
        "status": status,
        "ready": ready,
        "reason": reason,
        "observed_at_unix": observed_at_unix or int(time.time()),
        "details": details or {},
    }


def _gateway_state_from_heal_result(result: dict[str, Any]) -> dict[str, Any]:
    ready = bool(result.get("healthy"))
    reason = str(result.get("reason") or ("gateway_ready" if ready else "gateway_unhealthy"))
    details: dict[str, Any] = {
        "source": "heal_hermes",
        "healed": bool(result.get("healed")),
    }
    restart = result.get("restart")
    if isinstance(restart, dict):
        details["functional_ready"] = (
            restart.get("functionally_verified")
            if isinstance(restart.get("functionally_verified"), bool)
            else None
        )
        generation = restart.get("generation")
        if isinstance(generation, dict):
            if generation.get("owner") in {
                "foreground",
                "hermes_supervisor",
            }:
                details["runtime_generation"] = generation.get("after")
            else:
                details["service_generation"] = generation.get("after")
        if details.get("functional_ready") is True:
            details["functional_verified_at_unix"] = int(time.time())
    gateway = result.get("gateway")
    if isinstance(gateway, dict):
        details["gateway"] = gateway
    return _gateway_state_payload(
        status="ready" if ready else "not_ready",
        ready=ready,
        reason=reason,
        details=details,
    )


def _service_generation_started_unix(generation: dict[str, Any]) -> float:
    """Convert systemd's monotonic service-start timestamp to Unix time.

    Invalid or missing values return the current Unix time. Readiness then
    fails closed until Hermes writes a fresh adapter state.
    """
    now_unix = time.time()
    now_monotonic = time.monotonic()
    try:
        started_monotonic = (
            int(generation.get("exec_main_start_timestamp_monotonic") or 0)
            / 1_000_000
        )
    except (TypeError, ValueError):
        return now_unix
    if not 0 < started_monotonic <= now_monotonic:
        return now_unix
    return now_unix - (now_monotonic - started_monotonic)


async def _inspect_gateway_state(
    previous_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not _telegram_env_configured():
        return _gateway_state_payload(
            status="not_configured",
            ready=False,
            reason="telegram_not_configured",
        )
    hermes_bin = find_hermes_binary()
    if hermes_bin is None:
        return _gateway_state_payload(
            status="unknown",
            ready=None,
            reason="hermes_cli_missing",
        )
    status = await run_process(
        [str(hermes_bin), "gateway", "status"],
        timeout_seconds=GATEWAY_STATE_PROBE_TIMEOUT_SECONDS,
    )
    text = f"{status.get('stdout') or ''}\n{status.get('stderr') or ''}".lower()
    draining = any(
        marker in text
        for marker in (
            "gateway draining",
            "draining for restart",
            "restart in progress",
            "gateway restarting",
            "active: deactivating",
            "active: reloading",
        )
    )
    known_non_serving = any(
        marker in text
        for marker in (
            "active: inactive",
            "active: failed",
            "inactive (dead)",
            "not running",
            "service not found",
            "could not be found",
            "not installed",
        )
    )
    telegram_fatal = gateway_status_reports_telegram_fatal(status)
    details: dict[str, Any] = {"gateway_status": _compact_process(status)}
    if telegram_fatal:
        lifecycle = "non_serving"
        ready = False
        reason = "gateway_status_telegram_fatal"
        details["functional_ready"] = False
    elif draining:
        lifecycle = "draining_restarting"
        ready: bool | None = False
        reason = "gateway_status_draining_restarting"
    elif _gateway_status_is_healthy(status):
        discovery = await discover_gateway_service()
        discovery_reason = str(discovery.get("reason") or "service_unknown")
        generation = (
            discovery.get("generation") if discovery.get("ok") else None
        )
        owner = discovery.get("owner") if discovery.get("ok") else None
        invocation_id = (
            str(generation.get("invocation_id") or "").strip()
            if isinstance(generation, dict)
            else ""
        )
        runtime_generation = (
            read_gateway_runtime_generation()
            if not discovery.get("ok")
            and discovery_reason in GATEWAY_SERVICE_UNOWNED_REASONS
            else None
        )
        foreground_generation = (
            _active_gateway_foreground_generation(hermes_bin)
            if isinstance(runtime_generation, dict)
            else None
        )
        if not (
            isinstance(runtime_generation, dict)
            and isinstance(foreground_generation, dict)
            and foreground_generation.get("pid")
            == runtime_generation.get("pid")
            and foreground_generation.get("process_start_time")
            == runtime_generation.get("start_time")
            and foreground_generation.get("argv")
            == runtime_generation.get("argv")
        ):
            foreground_generation = None
        previous_details = (
            previous_state.get("details")
            if isinstance(previous_state, dict)
            and isinstance(previous_state.get("details"), dict)
            else {}
        )
        previous_generation = previous_details.get("service_generation")
        previous_invocation_id = (
            str(previous_generation.get("invocation_id") or "").strip()
            if isinstance(previous_generation, dict)
            else ""
        )
        previous_runtime_generation = previous_details.get("runtime_generation")
        if (
            public_gateway_runtime_generation_same(
                runtime_generation, previous_runtime_generation
            )
            and isinstance(runtime_generation, dict)
            and isinstance(previous_runtime_generation, dict)
            and isinstance(
                previous_runtime_generation.get("started_at_unix"), int | float
            )
        ):
            runtime_generation = {
                **runtime_generation,
                "started_at_unix": previous_runtime_generation[
                    "started_at_unix"
                ],
            }
        previous_verified_at = previous_details.get(
            "functional_verified_at_unix"
        )
        proof_fresh = bool(
            isinstance(previous_verified_at, int | float)
            and time.time() - float(previous_verified_at)
            <= GATEWAY_FUNCTIONAL_RECHECK_SECONDS
        )
        generation_matches = bool(
            (
                invocation_id
                and invocation_id == previous_invocation_id
            )
            or (
                isinstance(runtime_generation, dict)
                and public_gateway_runtime_generation_same(
                    runtime_generation, previous_runtime_generation
                )
            )
        )
        functional_ready = bool(
            generation_matches
            and previous_details.get("functional_ready") is True
            and proof_fresh
        )
        telegram_evidence = previous_details.get("telegram_evidence")
        functional_verified_at = (
            int(float(previous_verified_at)) if functional_ready else None
        )
        if (
            not functional_ready
            and isinstance(owner, dict)
            and isinstance(generation, dict)
            and invocation_id
        ):
            # systemd exposes the service start on CLOCK_MONOTONIC. Convert it
            # to the wall-clock domain used by Hermes' ISO runtime-state
            # timestamps so a heartbeat cannot trust a Telegram row inherited
            # from the prior gateway process. Missing/invalid evidence uses
            # "now" and therefore fails closed until a fresh row is written.
            service_started_unix = _service_generation_started_unix(generation)
            readiness = await probe_functional_readiness(
                hermes_bin,
                since_unix=service_started_unix,
                service_manager=str(owner.get("manager") or "user"),
                service_invocation_id=invocation_id,
                service_main_pid=int(generation.get("main_pid") or 0) or None,
            )
            functional_ready = readiness.get("functionally_ready") is True
            telegram_evidence = readiness.get("telegram_evidence")
            if functional_ready:
                functional_verified_at = int(time.time())
        elif not functional_ready and isinstance(runtime_generation, dict):
            # Non-systemd supervisors and foreground gateways have no systemd
            # invocation. Hermes' atomic state is bound to the live PID,
            # process-start fingerprint, and exact profile-preserving argv.
            # Only a matching Tinyhat-owned detached generation may also use
            # bytes appended after its persisted managed-log offset.
            readiness = await probe_functional_readiness(
                hermes_bin,
                since_unix=float(runtime_generation["started_at_unix"]),
                log_path=(
                    _gateway_log_path()
                    if isinstance(foreground_generation, dict)
                    else None
                ),
                log_offset=(
                    int(foreground_generation["log_offset"])
                    if isinstance(foreground_generation, dict)
                    else 0
                ),
                service_main_pid=int(runtime_generation["pid"]),
                expected_process_start_time=int(
                    runtime_generation["start_time"]
                ),
                expected_gateway_argv=list(runtime_generation["argv"]),
            )
            functional_ready = readiness.get("functionally_ready") is True
            telegram_evidence = readiness.get("telegram_evidence")
            if functional_ready:
                functional_verified_at = int(time.time())
        details.update(
            {
                "functional_ready": functional_ready,
                "telegram_evidence": telegram_evidence or "unavailable",
                "functional_verified_at_unix": functional_verified_at,
                "service_generation": public_gateway_generation(generation),
                "runtime_generation": public_gateway_runtime_generation(
                    runtime_generation
                ),
                "foreground_generation": public_gateway_foreground_generation(
                    foreground_generation,
                    matches_runtime=isinstance(foreground_generation, dict),
                ),
                "service_discovery_reason": discovery_reason,
            }
        )
        if functional_ready:
            lifecycle = "serving"
            ready = True
            reason = "gateway_functionally_ready"
        else:
            lifecycle = "serving_unverified"
            ready = None
            reason = "gateway_functional_readiness_unverified"
    elif status.get("ok") or known_non_serving:
        lifecycle = "non_serving"
        ready = False
        reason = "gateway_status_not_ready"
    else:
        lifecycle = "unknown"
        ready = None
        reason = "gateway_status_unavailable"
    return _gateway_state_payload(
        status=lifecycle,
        ready=ready,
        reason=reason,
        details=details,
    )


async def _refresh_gateway_state(ctx: RuntimeContext) -> None:
    state = (getattr(ctx, "platform_state", "") or "").strip().lower()
    if state not in ASSIGNED_PLATFORM_STATES:
        return
    try:
        ctx.gateway_state = await _inspect_gateway_state(ctx.gateway_state)
    except Exception as exc:  # noqa: BLE001 - heartbeat must remain best-effort.
        ctx.gateway_state = _gateway_state_payload(
            status="unknown",
            ready=None,
            reason="gateway_probe_failed",
            details={"error": str(exc)[:200]},
        )


async def _run_gateway_reconcile(ctx: RuntimeContext) -> dict[str, Any]:
    result = await run_command(
        ctx,
        {
            "kind": "heal_hermes",
            "spec": {"reason": "runtime_assigned_heartbeat_reconcile"},
        },
    )
    ctx.gateway_state = _gateway_state_from_heal_result(result)
    if result.get("healthy"):
        print("gateway reconcile complete: healthy", flush=True)
    else:
        print(
            f"gateway reconcile incomplete: {result.get('reason') or 'unknown'}",
            file=sys.stderr,
            flush=True,
        )
    return result


def _consume_gateway_reconcile_task(ctx: RuntimeContext) -> None:
    task = ctx.gateway_reconcile_task
    if task is None or not task.done():
        return
    ctx.gateway_reconcile_task = None
    try:
        task.result()
    except Exception as exc:  # noqa: BLE001 - reconciliation must not stop heartbeat.
        print(f"gateway reconcile failed: {exc}", file=sys.stderr, flush=True)


def _maybe_start_gateway_reconcile(ctx: RuntimeContext) -> None:
    """Start the one-shot assignment-time gateway bring-up reconcile.

    This runs at most once per runtime process. The runtime never initiates
    gateway mutations on its own beyond this single bring-up: recovery policy
    belongs to the platform, which queues explicit commands (for example
    ``heal_hermes`` with ``spec.restart=true``).
    """
    _consume_gateway_reconcile_task(ctx)
    if ctx.gateway_reconciled or ctx.gateway_reconcile_task is not None:
        return
    command_task = getattr(ctx, "command_task", None)
    if command_task is not None and not command_task.done():
        # Never kick the bring-up while a platform command is mid-flight
        # (for example a configure_telegram that is itself about to start
        # the gateway); the one-shot fires on a later beat instead.
        return
    state = (getattr(ctx, "platform_state", "") or "").strip().lower()
    if state not in ASSIGNED_PLATFORM_STATES:
        return
    if not _telegram_env_configured():
        return
    ctx.gateway_reconciled = True
    ctx.gateway_reconcile_task = asyncio.create_task(_run_gateway_reconcile(ctx))


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
    result = read_scheduled_result_for_retry(
        state_dir=ctx.state_dir,
        date_key=date_key,
    )
    if result is None:
        result = await run_update_check(
            state_dir=ctx.state_dir,
            current_version=ctx.current_version(),
            current_code_version=__version__,
            current_sha=ctx.current_commit_sha(),
            reason="scheduled",
            scheduled_local_date=date_key,
        )
    await ctx.platform.post_json(
        context_computer_api_path(ctx, "update-check-results/v1"),
        {"result": result},
    )
    mark_scheduled_check_started(state_dir=ctx.state_dir, date_key=date_key)
    clear_scheduled_result_for_retry(
        state_dir=ctx.state_dir,
        date_key=date_key,
    )
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
    _consume_gateway_reconcile_task(ctx)
    _maybe_start_scheduled_update_check(ctx)
    await _refresh_gateway_state(ctx)
    response = await ctx.platform.post_json(
        context_computer_api_path(ctx, "heartbeat"),
        {"metrics": _heartbeat_metrics(ctx, status="running")},
    )
    platform_state = response.get("state")
    if isinstance(platform_state, str) and platform_state.strip():
        ctx.platform_state = platform_state.strip()
    _maybe_start_gateway_reconcile(ctx)
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
        _consume_gateway_reconcile_task(ctx)
        if ctx.restart_requested and ctx.command_task is None:
            print("restart requested after command settlement", flush=True)
            return 0
        await asyncio.sleep(_heartbeat_interval_seconds(ctx))


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
