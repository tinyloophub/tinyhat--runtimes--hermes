"""Repair the local Hermes messaging runtime without fetching new secrets.

The command is intentionally bounded to already-configured Computers. It
reloads Tinyhat-managed env files, verifies that Telegram credentials are
present, and then either:

- default (``spec.restart`` absent/false): reuses the same durable gateway
  start path as ``start_hermes`` — a bounded start-only heal that no-ops on
  an already-healthy gateway.  For compatibility with the original admin
  button, an absent restart field plus the exact reason
  ``admin_heal_hermes`` requests a restart; explicit false remains start-only;
  or
- ``spec.restart=true``: reloads env files and runs the official ``hermes
  gateway restart`` command for a bounded grace period. When exactly one
  systemd unit owns the gateway, a stuck old generation is narrowly
  force-cycled and success requires a different active generation plus fresh
  Telegram evidence. When systemd does not own the gateway, the runtime avoids
  the blocking no-supervisor restart fallback: it proves Hermes' live
  kind/PID/process-start/argv generation, runs bounded documented gateway stop
  and start paths, and requires a different live generation plus connected
  Telegram state. If documented stop leaves that exact generation stuck, a
  Linux pidfd TERM/KILL fallback revalidates profile, process start, and argv
  before each signal. Other platforms fail closed and rely on the documented
  supervisor/stop path. A Tinyhat-owned detached generation may fall back to only
  the bytes it appended to the managed foreground log. Ambiguous, stale, or
  unavailable ownership still fails closed. Missing evidence never reports
  healed.

It does not call the Telegram setup endpoint, mint bot tokens, unassign the
Computer, or restart the Tinyhat runtime service. No secret values are logged
or returned; results carry env-variable *names* only.

Example input:
    {"kind": "heal_hermes", "spec": {"restart": true, "deadline_seconds": 90,
     "reason": "secret_saved_restart"}}

The result's ``restart`` object reports {requested, performed,
deadline_seconds, deadline_exceeded, telegram_evidence, milestones_ms}
where ``milestones_ms`` carries millisecond offsets from the restart start
for restart_started / restart_done / verified. Systemd uses the official
restart command plus a unit-scoped fallback; other supervisors use bounded
official stop/start commands.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from hermes_runtime.commands import start_hermes
from hermes_runtime.commands.configure_telegram import (
    _active_gateway_foreground_generation,
    _compact_process,
    _env_file_candidates,
    _gateway_log_path,
    _gateway_status_is_healthy,
    ensure_telegram_network_fallback_env,
)
from hermes_runtime.gateway_readiness import (
    gateway_runtime_generation_active,
    gateway_runtime_generation_same,
    gateway_log_size,
    probe_functional_readiness,
    public_gateway_runtime_generation,
    read_gateway_runtime_generation,
)
from hermes_runtime.gateway_service import (
    GATEWAY_SERVICE_UNOWNED_REASONS,
    discover_gateway_service,
    gateway_generation_active,
    gateway_generation_changed,
    gateway_generation_needs_force_kill,
    gateway_generation_same,
    public_gateway_generation,
    run_gateway_service_action,
    snapshot_gateway_service,
)
from hermes_runtime.hermes_cli import (
    find_hermes_binary,
    probe_hermes_status,
    run_process,
)
from hermes_runtime.runtime_env import load_env_files_into_process, read_env_values

TELEGRAM_ENV_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USERS",
    "TELEGRAM_HOME_CHANNEL",
)

DEFAULT_RESTART_DEADLINE_SECONDS = 90
MIN_RESTART_DEADLINE_SECONDS = 30
MAX_RESTART_DEADLINE_SECONDS = 300
RESTART_VERIFY_POLL_SECONDS = 2.0
OFFICIAL_RESTART_GRACE_SECONDS = 20
FORCE_FALLBACK_RESERVE_SECONDS = 15
SERVICE_ACTION_TIMEOUT_SECONDS = 5
MANAGED_STOP_COMMAND_TIMEOUT_SECONDS = 5
MANAGED_STOP_GRACE_SECONDS = 3
EXACT_TERM_GRACE_SECONDS = 3
EXACT_KILL_GRACE_SECONDS = 2
MANAGED_START_RESERVE_SECONDS = 10

# Module alias so tests can inject a fake clock without touching the global
# ``time`` module that asyncio itself relies on.
_monotonic = time.monotonic
_pidfd_open = getattr(os, "pidfd_open", None)
_pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
_close_fd = os.close


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


def _telegram_env_status() -> dict[str, Any]:
    values = read_env_values(_env_file_candidates(), names=TELEGRAM_ENV_KEYS)
    present = {
        key: bool((os.getenv(key) or values.get(key) or "").strip())
        for key in TELEGRAM_ENV_KEYS
    }
    return {
        "configured": bool(present["TELEGRAM_BOT_TOKEN"]),
        "present_keys": sorted(key for key, value in present.items() if value),
        "missing_keys": sorted(key for key, value in present.items() if not value),
    }


def _bool_spec(spec: dict[str, Any], key: str, default: bool = False) -> bool:
    value = spec.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _restart_deadline_seconds(spec: dict[str, Any]) -> int:
    try:
        value = int(spec.get("deadline_seconds") or DEFAULT_RESTART_DEADLINE_SECONDS)
    except (TypeError, ValueError):
        value = DEFAULT_RESTART_DEADLINE_SECONDS
    return max(
        MIN_RESTART_DEADLINE_SECONDS,
        min(MAX_RESTART_DEADLINE_SECONDS, value),
    )


def _restart_summary(
    *,
    requested: bool,
    performed: bool,
    deadline_seconds: int,
    deadline_exceeded: bool = False,
    telegram_evidence: str | None = None,
    milestones_ms: dict[str, int | None] | None = None,
    compat_defaulted: bool = False,
    verified: bool = False,
    functionally_verified: bool = False,
    method: str | None = None,
    fallback_attempted: bool = False,
    generation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "requested": requested,
        "performed": performed,
        "deadline_seconds": deadline_seconds,
        "deadline_exceeded": deadline_exceeded,
        "compat_defaulted": compat_defaulted,
        "verified": verified,
        "functionally_verified": functionally_verified,
        "fallback_attempted": fallback_attempted,
        "milestones_ms": milestones_ms or {},
    }
    if method is not None:
        summary["method"] = method
    if generation is not None:
        summary["generation"] = generation
    if telegram_evidence is not None:
        summary["telegram_evidence"] = telegram_evidence
    return summary


def _action_summary(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    return {
        "ok": bool(result.get("ok")),
        "returncode": result.get("returncode"),
        "timed_out": bool(result.get("timed_out")),
        "duration_ms": result.get("duration_ms"),
    }


def _gateway_service_proven_stopped(snapshot: dict[str, Any] | None) -> bool:
    """Whether the proven unit owner currently has no live gateway process."""
    if not isinstance(snapshot, dict) or snapshot.get("load_state") != "loaded":
        return False
    try:
        main_pid = int(snapshot.get("main_pid") or 0)
    except (TypeError, ValueError):
        return False
    return bool(
        main_pid == 0
        and snapshot.get("active_state") in {"inactive", "failed"}
        and snapshot.get("sub_state") in {"dead", "failed"}
    )


async def _wait_for_exact_gateway_exit(
    generation: dict[str, Any],
    *,
    timeout_seconds: float,
) -> bool | None:
    """Wait for one exact process generation to exit.

    ``None`` preserves an unreadable identity as an ambiguity; callers must
    not continue a restart transaction in that state.
    """
    deadline = _monotonic() + max(0.0, timeout_seconds)
    while True:
        active = gateway_runtime_generation_active(generation)
        if active is not True:
            return active
        remaining = deadline - _monotonic()
        if remaining <= 0:
            return True
        await asyncio.sleep(min(RESTART_VERIFY_POLL_SECONDS, remaining))


async def _force_exit_exact_gateway_generation(
    generation: dict[str, Any],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Terminate only a repeatedly re-proven non-systemd gateway process.

    The public stop command gets the first chance to shut down cleanly. This
    fallback sends Linux pidfd-scoped TERM and, only if needed, KILL. Before
    each signal it re-reads Hermes' state and requires the same profile-bound
    PID/start-time/argv generation. PID reuse, missing state, unreadable
    identity, and platforms without pidfds all fail closed without signaling
    another process.
    """
    result: dict[str, Any] = {
        "attempted": False,
        "term_sent": False,
        "kill_sent": False,
        "exited": False,
        "failure_reason": None,
    }
    deadline = _monotonic() + max(0.0, timeout_seconds)

    def _remaining() -> float:
        return max(0.0, deadline - _monotonic())

    def _reconfirm() -> bool:
        return gateway_runtime_generation_same(
            read_gateway_runtime_generation(), generation
        )

    if _remaining() <= 0:
        result["failure_reason"] = "gateway_force_exit_deadline_exceeded"
        return result

    active = gateway_runtime_generation_active(generation)
    if active is False:
        result["exited"] = True
        return result
    if active is None:
        result["failure_reason"] = "gateway_generation_unproven"
        return result
    if not _reconfirm():
        active = gateway_runtime_generation_active(generation)
        if active is False:
            result["exited"] = True
        else:
            result["failure_reason"] = "gateway_generation_not_reconfirmed"
        return result

    # A numeric PID can be recycled between the final identity proof and
    # signal delivery. Linux pidfds close that gap; other platforms must rely
    # on the documented Hermes stop path or their real supervisor rather than
    # risk signaling an unrelated process.
    if not _is_linux():
        result["failure_reason"] = "gateway_exact_signal_unavailable"
        return result

    pid = int(generation["pid"])
    force_kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
    pidfd: int | None = None
    pidfd_send_signal: Any = None
    pidfd_open = _pidfd_open
    pidfd_send_signal = _pidfd_send_signal
    if not callable(pidfd_open) or not callable(pidfd_send_signal):
        result["failure_reason"] = "gateway_pidfd_unavailable"
        return result
    try:
        pidfd = int(pidfd_open(pid, 0))
    except ProcessLookupError:
        result["exited"] = True
        return result
    except OSError:
        result["failure_reason"] = "gateway_pidfd_open_failed"
        return result

    # Opening a pidfd closes the PID-reuse gap for all subsequent signals,
    # but still re-prove that the handle was opened for the generation in
    # Hermes' state before sending anything.
    active_after_open = gateway_runtime_generation_active(generation)
    if active_after_open is False:
        _close_fd(pidfd)
        result["exited"] = True
        return result
    if not _reconfirm() or active_after_open is not True:
        _close_fd(pidfd)
        result["failure_reason"] = "gateway_generation_not_reconfirmed"
        return result

    assert pidfd is not None
    assert callable(pidfd_send_signal)
    result["attempted"] = True
    try:
        try:
            pidfd_send_signal(pidfd, signal.SIGTERM, None, 0)
            result["term_sent"] = True
        except ProcessLookupError:
            result["exited"] = True
            return result
        except OSError:
            result["failure_reason"] = "gateway_term_failed"
            return result

        active = await _wait_for_exact_gateway_exit(
            generation,
            timeout_seconds=min(EXACT_TERM_GRACE_SECONDS, _remaining()),
        )
        if active is False:
            result["exited"] = True
            return result
        if active is None:
            result["failure_reason"] = "gateway_generation_unproven_after_term"
            return result
        if _remaining() <= 0:
            result["failure_reason"] = "gateway_force_exit_deadline_exceeded"
            return result

        # Re-read the persisted state and live identity immediately before
        # KILL. The pidfd remains bound to the same process even if its numeric
        # PID is concurrently recycled.
        if not _reconfirm():
            active = gateway_runtime_generation_active(generation)
            if active is False:
                result["exited"] = True
            else:
                result["failure_reason"] = "gateway_generation_not_reconfirmed"
            return result
        try:
            pidfd_send_signal(pidfd, force_kill_signal, None, 0)
            result["kill_sent"] = True
        except ProcessLookupError:
            result["exited"] = True
            return result
        except OSError:
            result["failure_reason"] = "gateway_kill_failed"
            return result

        active = await _wait_for_exact_gateway_exit(
            generation,
            timeout_seconds=min(EXACT_KILL_GRACE_SECONDS, _remaining()),
        )
        if active is False:
            result["exited"] = True
        elif active is None:
            result["failure_reason"] = "gateway_generation_unproven_after_kill"
        else:
            result["failure_reason"] = "gateway_generation_still_active"
        return result
    finally:
        if pidfd is not None:
            _close_fd(pidfd)


async def _run_gateway_restart(
    ctx: Any,
    *,
    hermes_bin: Path,
    reason: str,
    deadline_seconds: int,
) -> dict[str, Any]:
    """Restart a gateway and prove service or foreground functional readiness."""
    del reason
    started = _monotonic()
    since_unix = time.time()
    log_path = _gateway_log_path()
    log_offset = gateway_log_size(log_path)

    def _ms() -> int:
        return int((_monotonic() - started) * 1000)

    def _remaining() -> float:
        return max(0.0, deadline_seconds - (_monotonic() - started))

    deadline_monotonic = started + deadline_seconds

    def _managed_mutation_remaining() -> float:
        """Budget available before the paired managed start reserve."""
        return max(0.0, _remaining() - MANAGED_START_RESERVE_SECONDS)

    # Reload env into this process for parity with the start-only heal path;
    # the gateway itself re-reads ~/.hermes/.env (where the saved secret lives)
    # when it restarts.
    try:
        load_env_files_into_process(_env_file_candidates())
    except Exception:  # noqa: BLE001 - env reload is best-effort.
        pass

    milestones: dict[str, int | None] = {
        "restart_started": _ms(),
        "official_done": None,
        "fallback_started": None,
        "restart_done": None,
        "verified": None,
    }
    discovery = await discover_gateway_service()
    discovery_reason = str(discovery.get("reason") or "service_unknown")
    owner = discovery.get("owner") if discovery.get("ok") else None
    before = discovery.get("generation") if discovery.get("ok") else None
    official_only = (
        not discovery.get("ok")
        and discovery_reason in GATEWAY_SERVICE_UNOWNED_REASONS
    )
    if (
        not official_only
        and (not isinstance(owner, dict) or not isinstance(before, dict))
    ):
        return {
            "verified": False,
            "functionally_verified": False,
            "deadline_exceeded": False,
            "restart_command_ok": False,
            "performed": False,
            "method": None,
            "fallback_attempted": False,
            "failure_reason": discovery_reason,
            "milestones_ms": milestones,
            "readiness": None,
            "restart_result": None,
            "fallback_actions": {},
            "generation": {
                "owner": None,
                "before": None,
                "after": None,
                "changed": False,
                "active": False,
            },
        }

    if official_only:
        # On hosts without systemd ownership, ``hermes gateway restart`` may
        # become the blocking gateway process itself. A timeout would kill the
        # replacement. Use only documented bounded stop/start commands:
        # snapshot Hermes' exact live gateway-state generation, request stop,
        # prove that generation is gone, then let ``start_hermes`` dispatch the
        # host's real supervisor (s6/launchd/etc.) or Tinyhat's detached
        # foreground fallback. Unknown healthy owners fail closed.
        before_runtime = read_gateway_runtime_generation()
        status_before: dict[str, Any] | None = None
        if _remaining() > 0:
            status_before = await run_process(
                [str(hermes_bin), "gateway", "status"],
                timeout_seconds=min(SERVICE_ACTION_TIMEOUT_SECONDS, _remaining()),
            )
        status_text = (
            f"{status_before.get('stdout') or ''}\n"
            f"{status_before.get('stderr') or ''}"
            if isinstance(status_before, dict)
            else ""
        ).lower()
        status_healthy = _gateway_status_is_healthy(status_before)
        status_known_down = any(
            marker in status_text
            for marker in (
                "gateway is not running",
                "gateway: inactive (dead)",
                "gateway stopped",
                "gateway service not found",
                "gateway service is not installed",
            )
        )
        performed = False
        stop_result: dict[str, Any] | None = None
        force_exit_result: dict[str, Any] | None = None
        start_result: dict[str, Any] | None = None
        after_runtime: dict[str, Any] | None = None
        failure_reason: str | None = None
        deadline_exceeded = False
        paired_stop_admitted = False
        paired_start_task: asyncio.Task[dict[str, Any]] | None = None

        def _ensure_paired_start_task() -> asyncio.Task[dict[str, Any]]:
            nonlocal paired_start_task
            if paired_start_task is None:
                # Cancellation can arrive while the documented stop is still
                # settling. Only skip start_hermes' safety preflight after the
                # exact old generation is positively gone; otherwise use its
                # normal status-first path so a still-live gateway is not
                # duplicated.
                gateway_known_stopped = bool(
                    before_runtime is None
                    or gateway_runtime_generation_active(before_runtime) is False
                )
                paired_start_task = asyncio.create_task(
                    start_hermes.run(
                        ctx,
                        {
                            "kind": "start_hermes",
                            "spec": {"reason": "heal_restart"},
                        },
                        deadline_monotonic=deadline_monotonic,
                        gateway_known_stopped=gateway_known_stopped,
                        include_hermes_status=False,
                    )
                )
            return paired_start_task

        async def _finish_paired_start() -> dict[str, Any]:
            """Finish the bounded paired start despite outer cancellation."""
            task = _ensure_paired_start_task()
            while True:
                try:
                    return await asyncio.shield(task)
                except asyncio.CancelledError:
                    # Once stop is admitted, cancellation is deferred until
                    # the bounded start transaction has put a gateway back.
                    continue

        async def _run_paired_start() -> dict[str, Any]:
            task = _ensure_paired_start_task()
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await _finish_paired_start()
                raise

        async def _await_after_paired_stop(awaitable: Any) -> Any:
            nonlocal start_result, performed
            try:
                return await awaitable
            except asyncio.CancelledError:
                # Stop may have initiated shutdown just before cancellation.
                # Reconcile for the bounded stop grace while deferring any
                # repeated cancellation. Start only after the exact old
                # generation is positively gone; unknown/replaced identity
                # still fails closed.
                reconcile_deadline = _monotonic() + min(
                    MANAGED_STOP_GRACE_SECONDS,
                    _remaining(),
                )
                safe_to_start = False
                while True:
                    old_active = gateway_runtime_generation_active(before_runtime)
                    current = read_gateway_runtime_generation()
                    current_same_or_absent = bool(
                        current is None
                        or gateway_runtime_generation_same(current, before_runtime)
                    )
                    if old_active is False and current_same_or_absent:
                        safe_to_start = True
                        break
                    if old_active is not True or not current_same_or_absent:
                        break
                    remaining = min(
                        _remaining(), reconcile_deadline - _monotonic()
                    )
                    if remaining <= 0:
                        break
                    sleep_task = asyncio.create_task(
                        asyncio.sleep(min(RESTART_VERIFY_POLL_SECONDS, remaining))
                    )
                    while True:
                        try:
                            await asyncio.shield(sleep_task)
                            break
                        except asyncio.CancelledError:
                            continue
                if safe_to_start:
                    try:
                        start_result = await _finish_paired_start()
                        performed = True
                    finally:
                        raise
                raise

        if status_before is None:
            failure_reason = "gateway_restart_deadline_exceeded"
            deadline_exceeded = True
        elif status_healthy and before_runtime is None:
            failure_reason = "gateway_runtime_generation_unproven"
        elif not status_healthy and not status_known_down and before_runtime is None:
            failure_reason = "gateway_status_unavailable"
        elif _remaining() <= 0:
            failure_reason = "gateway_restart_deadline_exceeded"
            deadline_exceeded = True
        else:
            if before_runtime is not None:
                current_before_stop = read_gateway_runtime_generation()
                old_active_before_stop = gateway_runtime_generation_active(
                    before_runtime
                )
                old_already_exited = (
                    current_before_stop is None
                    and old_active_before_stop is False
                )
                if not old_already_exited and not gateway_runtime_generation_same(
                    current_before_stop, before_runtime
                ):
                    failure_reason = "gateway_generation_changed_before_stop"
                mutation_remaining = _managed_mutation_remaining()
                if (
                    failure_reason is None
                    and not old_already_exited
                    and mutation_remaining <= 0
                ):
                    failure_reason = "gateway_restart_deadline_exceeded"
                    deadline_exceeded = True
                elif failure_reason is None and not old_already_exited:
                    paired_stop_admitted = True
                    stop_result = await _await_after_paired_stop(
                        run_process(
                            [str(hermes_bin), "gateway", "stop"],
                            timeout_seconds=min(
                                MANAGED_STOP_COMMAND_TIMEOUT_SECONDS,
                                mutation_remaining,
                            ),
                            kill_process_group=True,
                        )
                    )
                    performed = True
                    milestones["official_done"] = _ms()
                    stop_wait_deadline = _monotonic() + min(
                        MANAGED_STOP_GRACE_SECONDS,
                        _managed_mutation_remaining(),
                    )
                    old_generation_active = gateway_runtime_generation_active(
                        before_runtime
                    )
                    while old_generation_active is True:
                        remaining = min(
                            _managed_mutation_remaining(),
                            stop_wait_deadline - _monotonic(),
                        )
                        if remaining <= 0:
                            break
                        await _await_after_paired_stop(
                            asyncio.sleep(
                                min(RESTART_VERIFY_POLL_SECONDS, remaining)
                            )
                        )
                        old_generation_active = (
                            gateway_runtime_generation_active(before_runtime)
                        )
                    if old_generation_active is None:
                        failure_reason = "gateway_stop_unverified"
                    elif old_generation_active is True:
                        force_exit_result = await _await_after_paired_stop(
                            _force_exit_exact_gateway_generation(
                                before_runtime,
                                timeout_seconds=min(
                                    EXACT_TERM_GRACE_SECONDS
                                    + EXACT_KILL_GRACE_SECONDS,
                                    _managed_mutation_remaining(),
                                ),
                            )
                        )
                        if not force_exit_result.get("exited"):
                            if (
                                gateway_runtime_generation_active(before_runtime)
                                is False
                            ):
                                force_exit_result["exited"] = True
                                force_exit_result["failure_reason"] = None
                            else:
                                failure_reason = str(
                                    force_exit_result.get("failure_reason")
                                    or "gateway_force_exit_unverified"
                                )
            if (
                (failure_reason is None or paired_stop_admitted)
                and _remaining() > 0
            ):
                start_result = await _run_paired_start()
                performed = True
            elif failure_reason is None or paired_stop_admitted:
                failure_reason = "gateway_restart_deadline_exceeded"
                deadline_exceeded = True

        milestones["restart_done"] = _ms()
        readiness: dict[str, Any] | None = None
        verified = False
        generation_owner: str | None = None
        while failure_reason is None:
            remaining = _remaining()
            if remaining <= 0:
                deadline_exceeded = True
                failure_reason = "gateway_restart_deadline_exceeded"
                break
            current_runtime = read_gateway_runtime_generation()
            if current_runtime is None or gateway_runtime_generation_same(
                current_runtime, before_runtime
            ):
                await asyncio.sleep(
                    min(RESTART_VERIFY_POLL_SECONDS, remaining)
                )
                continue
            after_runtime = current_runtime
            foreground = _active_gateway_foreground_generation(hermes_bin)
            foreground_matches = bool(
                isinstance(foreground, dict)
                and foreground.get("pid") == after_runtime.get("pid")
                and foreground.get("process_start_time")
                == after_runtime.get("start_time")
                and foreground.get("argv") == after_runtime.get("argv")
            )
            generation_owner = (
                "foreground"
                if foreground_matches
                else "hermes_supervisor"
            )
            readiness = await probe_functional_readiness(
                hermes_bin,
                since_unix=float(after_runtime["started_at_unix"]),
                log_path=log_path if foreground_matches else None,
                log_offset=(int(foreground["log_offset"]) if foreground_matches else 0),
                service_main_pid=int(after_runtime["pid"]),
                expected_process_start_time=int(after_runtime["start_time"]),
                expected_gateway_argv=list(after_runtime["argv"]),
                timeout_seconds=remaining,
            )
            if _remaining() <= 0:
                deadline_exceeded = True
                failure_reason = "gateway_restart_deadline_exceeded"
                break
            if readiness.get("ready"):
                verified = True
                milestones["verified"] = _ms()
                break
            remaining = _remaining()
            if remaining <= 0:
                deadline_exceeded = True
                failure_reason = "gateway_restart_deadline_exceeded"
                break
            await asyncio.sleep(min(RESTART_VERIFY_POLL_SECONDS, remaining))

        if _remaining() <= 0 and not verified:
            deadline_exceeded = True
        if deadline_exceeded and failure_reason is None:
            failure_reason = "gateway_restart_deadline_exceeded"
        generation_changed = bool(
            after_runtime is not None
            and not gateway_runtime_generation_same(before_runtime, after_runtime)
        )
        current_after_runtime = read_gateway_runtime_generation()
        current_after_same = gateway_runtime_generation_same(
            current_after_runtime, after_runtime
        )
        after_runtime_active = gateway_runtime_generation_active(after_runtime)
        generation_active = bool(
            after_runtime is not None
            and current_after_same
            and after_runtime_active is True
        )
        if verified and not generation_active:
            verified = False
            if current_after_runtime is not None and not current_after_same:
                failure_reason = "gateway_generation_replaced_after_readiness"
            elif after_runtime_active is None:
                failure_reason = "gateway_generation_unproven_after_readiness"
            else:
                failure_reason = "gateway_generation_not_active"
        gateway = (
            start_result.get("gateway")
            if isinstance(start_result, dict)
            and isinstance(start_result.get("gateway"), dict)
            else {}
        )
        start_summary = {
            "ok": bool(
                isinstance(start_result, dict)
                and (
                    start_result.get("healthy")
                    or start_result.get("started")
                    or start_result.get("already_running")
                )
            ),
            "started": bool(
                isinstance(start_result, dict) and start_result.get("started")
            ),
            "already_running": bool(
                isinstance(start_result, dict)
                and start_result.get("already_running")
            ),
            "mode": gateway.get("mode"),
        }
        return {
            "verified": verified,
            "generation_verified": generation_changed and generation_active,
            "functionally_verified": verified,
            "deadline_exceeded": deadline_exceeded,
            "restart_command_ok": bool(start_summary["ok"]),
            "performed": performed,
            "method": "managed_restart",
            "fallback_attempted": bool(
                force_exit_result and force_exit_result.get("attempted")
            ),
            "failure_reason": failure_reason,
            "milestones_ms": milestones,
            "readiness": readiness,
            "restart_result": start_summary,
            "fallback_actions": {
                "gateway_stop": _action_summary(stop_result),
                "gateway_force_exit": force_exit_result,
                "gateway_start": start_summary,
            },
            "generation": {
                "owner": generation_owner,
                "before": public_gateway_runtime_generation(before_runtime),
                "after": public_gateway_runtime_generation(after_runtime),
                "changed": generation_changed,
                "active": generation_active,
            },
        }

    # Preserve enough of the overall deadline for a unit-scoped fallback.  A
    # timed-out Hermes CLI is terminated with its whole process group so an
    # orphaned systemctl restart cannot race the fallback below. Foreground
    # mode returned above and never runs this blocking command.
    remaining = _remaining()
    official_timeout = min(
        OFFICIAL_RESTART_GRACE_SECONDS,
        max(1.0, remaining - FORCE_FALLBACK_RESERVE_SECONDS),
    )
    restart_result = await run_process(
        [str(hermes_bin), "gateway", "restart"],
        timeout_seconds=official_timeout,
        kill_process_group=True,
    )
    milestones["official_done"] = _ms()
    restart_command_ok = bool(restart_result.get("ok"))
    performed = True

    # From here on owner/before are guaranteed by the guarded service path.
    assert isinstance(owner, dict)
    assert isinstance(before, dict)
    after = await snapshot_gateway_service(
        owner,
        timeout_seconds=max(1.0, min(SERVICE_ACTION_TIMEOUT_SECONDS, _remaining())),
    )
    generation_changed = gateway_generation_changed(before, after)
    fallback_attempted = False
    fallback_actions: dict[str, Any] = {}
    failure_reason: str | None = None
    deadline_exceeded = False
    systemd_pair_task: asyncio.Task[str | None] | None = None

    async def _systemd_pair_sequence() -> str | None:
        # Reset failure state best-effort, but always reach start. The pair is
        # admitted only with the full fallback reserve.
        pair_failure: str | None = None
        for action in ("reset_failed", "start"):
            try:
                action_result = await run_gateway_service_action(
                    owner,
                    action,
                    timeout_seconds=SERVICE_ACTION_TIMEOUT_SECONDS,
                )
            except Exception:
                # The reset is best-effort and must never strand a unit that
                # the preceding kill may already have stopped. Treat an
                # ordinary action failure as a failed result while preserving
                # cancellation semantics (CancelledError is not Exception).
                action_result = {
                    "ok": False,
                    "returncode": None,
                    "timed_out": False,
                    "duration_ms": None,
                }
            fallback_actions[action] = _action_summary(action_result)
            if action == "start" and not action_result.get("ok"):
                pair_failure = "gateway_force_start_failed"
        return pair_failure

    def _ensure_systemd_pair_task() -> asyncio.Task[str | None]:
        nonlocal systemd_pair_task
        if systemd_pair_task is None:
            systemd_pair_task = asyncio.create_task(_systemd_pair_sequence())
        return systemd_pair_task

    async def _finish_systemd_pair() -> str | None:
        task = _ensure_systemd_pair_task()
        while True:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                continue

    async def _run_systemd_pair() -> str | None:
        task = _ensure_systemd_pair_task()
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await _finish_systemd_pair()
            raise

    async def _await_after_systemd_destructive(awaitable: Any) -> Any:
        try:
            return await awaitable
        except asyncio.CancelledError:
            try:
                await _finish_systemd_pair()
            finally:
                raise

    if generation_changed and _gateway_service_proven_stopped(after):
        # The official restart may create a fresh invocation which dies before
        # readiness. Re-prove that exact dead invocation under the same owner,
        # then admit one bounded reset/start pair. This is deliberately
        # one-shot: a second dead generation is reported, not cycled forever.
        rediscovery = await discover_gateway_service()
        current_owner = rediscovery.get("owner") if rediscovery.get("ok") else None
        current = (
            rediscovery.get("generation") if rediscovery.get("ok") else None
        )
        current_owner_matches = bool(
            isinstance(current_owner, dict)
            and current_owner.get("manager") == owner.get("manager")
        )
        if not current_owner_matches:
            failure_reason = (
                str(rediscovery.get("reason") or "gateway_service_probe_unavailable")
                if not rediscovery.get("ok")
                else "gateway_service_owner_changed"
            )
        elif gateway_generation_changed(after, current):
            # Another invocation already replaced the dead one. Do not mutate
            # it; the readiness loop below will verify that generation.
            after = current
            generation_changed = gateway_generation_changed(before, after)
        elif not gateway_generation_same(after, current):
            failure_reason = "gateway_generation_not_proven"
        elif not _gateway_service_proven_stopped(current):
            after = current
        elif _remaining() < FORCE_FALLBACK_RESERVE_SECONDS:
            failure_reason = "gateway_restart_deadline_exceeded"
            deadline_exceeded = True
        else:
            fallback_attempted = True
            milestones["fallback_started"] = _ms()
            failure_reason = await _run_systemd_pair()

    if not generation_changed:
        # Destructive recovery is allowed only when a fresh owner resolution
        # still identifies the same old unit generation.  This closes the race
        # where the official command completes just as its grace period ends.
        rediscovery = await discover_gateway_service()
        current_owner = rediscovery.get("owner") if rediscovery.get("ok") else None
        current = (
            rediscovery.get("generation") if rediscovery.get("ok") else None
        )
        current_owner_matches = bool(
            isinstance(current_owner, dict)
            and current_owner.get("manager") == owner.get("manager")
        )
        current_generation_same = bool(
            current_owner_matches and gateway_generation_same(before, current)
        )
        current_proven_stopped = bool(
            current_owner_matches and _gateway_service_proven_stopped(current)
        )
        if (
            current_owner_matches
            and gateway_generation_changed(before, current)
        ):
            after = current
            generation_changed = True
        elif not current_generation_same and not current_proven_stopped:
            after = current if isinstance(current, dict) else after
            failure_reason = (
                str(rediscovery.get("reason") or "gateway_service_probe_unavailable")
                if not rediscovery.get("ok")
                else "gateway_generation_not_proven"
            )
        else:
            if _remaining() < FORCE_FALLBACK_RESERVE_SECONDS:
                failure_reason = "gateway_restart_deadline_exceeded"
                deadline_exceeded = True
            else:
                fallback_attempted = True
                milestones["fallback_started"] = _ms()
                after = current
            if fallback_attempted and gateway_generation_needs_force_kill(current):
                kill_result = await _await_after_systemd_destructive(
                    run_gateway_service_action(
                        owner,
                        "kill",
                        timeout_seconds=max(
                            1.0, min(SERVICE_ACTION_TIMEOUT_SECONDS, _remaining())
                        ),
                    )
                )
                fallback_actions["kill"] = _action_summary(kill_result)
                if not kill_result.get("ok"):
                    raced = await _await_after_systemd_destructive(
                        snapshot_gateway_service(
                            owner,
                            timeout_seconds=max(
                                1.0,
                                min(SERVICE_ACTION_TIMEOUT_SECONDS, _remaining()),
                            ),
                        )
                    )
                    if gateway_generation_changed(before, raced):
                        after = raced
                        generation_changed = True
                    else:
                        # A mutating systemctl call can time out after SIGKILL
                        # reached the cgroup. Treat its outcome as unknown and
                        # still complete the admitted reset/start pair; never
                        # strand a possibly-stopped unit because the client
                        # missed the acknowledgement.
                        after = raced if isinstance(raced, dict) else after

            if fallback_attempted and not generation_changed and failure_reason is None:
                # Once kill may have reached the unit, never abandon it before
                # the paired start, even if outer cancellation arrives.
                failure_reason = await _run_systemd_pair()

    milestones["restart_done"] = _ms()
    readiness: dict[str, Any] | None = None
    verified = False
    generation_verified = False
    functionally_verified = False
    while failure_reason is None:
        remaining = _remaining()
        if remaining <= 0:
            deadline_exceeded = True
            failure_reason = "gateway_restart_deadline_exceeded"
            break
        after = await snapshot_gateway_service(
            owner,
            timeout_seconds=min(SERVICE_ACTION_TIMEOUT_SECONDS, remaining),
        )
        generation_changed = gateway_generation_changed(before, after)
        if generation_changed and gateway_generation_active(after):
            generation_verified = True
            readiness = await probe_functional_readiness(
                hermes_bin,
                since_unix=since_unix,
                log_path=log_path,
                log_offset=log_offset,
                service_manager=str(owner.get("manager") or "user"),
                service_invocation_id=str(after.get("invocation_id") or "")
                or None,
                service_main_pid=(
                    int(after.get("main_pid") or 0)
                    if isinstance(after, dict)
                    else None
                ),
                timeout_seconds=remaining,
            )
            if _remaining() <= 0:
                deadline_exceeded = True
                failure_reason = "gateway_restart_deadline_exceeded"
                break
            if readiness.get("status_healthy"):
                telegram_connected = readiness.get("telegram_connected")
                if telegram_connected is True:
                    remaining = _remaining()
                    if remaining <= 0:
                        deadline_exceeded = True
                        failure_reason = "gateway_restart_deadline_exceeded"
                        break
                    confirmed_after = await snapshot_gateway_service(
                        owner,
                        timeout_seconds=min(
                            SERVICE_ACTION_TIMEOUT_SECONDS, remaining
                        ),
                    )
                    if _remaining() <= 0:
                        after = confirmed_after
                        generation_changed = gateway_generation_changed(
                            before, after
                        )
                        generation_verified = False
                        deadline_exceeded = True
                        failure_reason = "gateway_restart_deadline_exceeded"
                        break
                    same_generation = gateway_generation_same(
                        after, confirmed_after
                    )
                    confirmed_active = gateway_generation_active(confirmed_after)
                    after = confirmed_after
                    generation_changed = gateway_generation_changed(before, after)
                    if not isinstance(confirmed_after, dict):
                        generation_verified = False
                        failure_reason = (
                            "gateway_generation_unproven_after_readiness"
                        )
                        break
                    if not same_generation:
                        generation_verified = False
                        failure_reason = (
                            "gateway_generation_replaced_after_readiness"
                        )
                        break
                    if not confirmed_active:
                        generation_verified = False
                        failure_reason = "gateway_generation_not_active"
                        break
                    verified = True
                    functionally_verified = True
                    milestones["verified"] = _ms()
                    break
                if telegram_connected is None:
                    failure_reason = "telegram_readiness_unavailable"
                    break
        remaining = _remaining()
        if remaining <= 0:
            deadline_exceeded = True
            failure_reason = "gateway_restart_deadline_exceeded"
            break
        await asyncio.sleep(min(RESTART_VERIFY_POLL_SECONDS, remaining))

    if _remaining() <= 0 and not verified:
        deadline_exceeded = True
    if deadline_exceeded and failure_reason is None:
        failure_reason = "gateway_restart_deadline_exceeded"
    generation_public = {
        "owner": str(owner.get("manager") or "unknown"),
        "before": public_gateway_generation(before),
        "after": public_gateway_generation(after),
        "changed": generation_changed,
        "active": gateway_generation_active(after),
    }
    return {
        "verified": verified,
        "generation_verified": generation_verified,
        "functionally_verified": functionally_verified,
        "deadline_exceeded": deadline_exceeded,
        "restart_command_ok": restart_command_ok,
        "performed": performed,
        "method": "systemd_force" if fallback_attempted else "official",
        "fallback_attempted": fallback_attempted,
        "failure_reason": failure_reason,
        "milestones_ms": milestones,
        "readiness": readiness,
        "restart_result": _compact_process(restart_result),
        "fallback_actions": fallback_actions,
        "generation": generation_public,
    }


async def run(ctx: Any, command: dict[str, Any]) -> dict[str, Any]:
    raw_spec = command.get("spec")
    spec: dict[str, Any] = raw_spec if isinstance(raw_spec, dict) else {}
    deadline_seconds = _restart_deadline_seconds(spec)
    request_reason = str(spec.get("reason") or "").strip() or "heal_hermes"
    compat_defaulted = (
        "restart" not in spec and request_reason == "admin_heal_hermes"
    )
    restart_requested = _bool_spec(spec, "restart", compat_defaulted)

    telegram = _telegram_env_status()
    hermes_bin = find_hermes_binary()
    if hermes_bin is None:
        return {
            "schema": "tinyhat_hermes_heal_v1",
            "healthy": False,
            "healed": False,
            "reason": "hermes_cli_missing",
            "telegram": telegram,
            "gateway": None,
            "hermes": await probe_hermes_status(),
            "restart": _restart_summary(
                requested=restart_requested,
                performed=False,
                deadline_seconds=deadline_seconds,
                compat_defaulted=compat_defaulted,
            ),
            "message": "Hermes CLI was not found; run install_hermes first.",
        }
    if not telegram["configured"]:
        return {
            "schema": "tinyhat_hermes_heal_v1",
            "healthy": False,
            "healed": False,
            "reason": "telegram_not_configured",
            "telegram": telegram,
            "gateway": None,
            "hermes": await probe_hermes_status(),
            "restart": _restart_summary(
                requested=restart_requested,
                performed=False,
                deadline_seconds=deadline_seconds,
                compat_defaulted=compat_defaulted,
            ),
            "message": "Telegram is not configured; run configure_telegram first.",
        }

    env_candidates = _env_file_candidates()
    telegram_network = ensure_telegram_network_fallback_env(env_candidates)
    if not telegram_network["ok"]:
        return {
            "schema": "tinyhat_hermes_heal_v1",
            "healthy": False,
            "healed": False,
            "reason": "telegram_network_env_failed",
            "telegram": telegram,
            "telegram_network": telegram_network,
            "gateway": None,
            "hermes": await probe_hermes_status(),
            "restart": _restart_summary(
                requested=restart_requested,
                performed=False,
                deadline_seconds=deadline_seconds,
                compat_defaulted=compat_defaulted,
            ),
            "message": "Hermes Telegram network fallback could not be prepared.",
        }

    if not restart_requested:
        start_result = await start_hermes.run(
            ctx,
            {"kind": "start_hermes", "spec": {"reason": request_reason}},
        )
        healthy = bool(start_result.get("healthy"))
        already_running = bool(start_result.get("already_running"))
        start_requested = bool(start_result.get("started") and not already_running)
        # start_hermes proves unit/CLI liveness, not Telegram delivery. Keep
        # the observation in `healthy`, but reserve `healed` for the restart
        # path's invocation-scoped functional verification.
        healed = False
        if healthy and start_requested:
            result_reason = "gateway_started_unverified"
        elif healthy and already_running:
            result_reason = "gateway_checked_healthy"
        else:
            result_reason = "gateway_unhealthy"
        return {
            "schema": "tinyhat_hermes_heal_v1",
            "healthy": healthy,
            "healed": healed,
            "reason": result_reason,
            "telegram": telegram,
            "gateway": start_result.get("gateway"),
            "hermes": start_result.get("hermes"),
            "env_reload": start_result.get("env_reload"),
            "telegram_network": telegram_network,
            "start_hermes": start_result,
            "restart": _restart_summary(
                requested=False,
                performed=False,
                deadline_seconds=deadline_seconds,
                compat_defaulted=False,
            ),
            "message": (
                "Hermes Telegram gateway start was requested; functional "
                "readiness was not verified."
                if healthy and start_requested
                else "Hermes Telegram gateway was already running."
                if healthy and already_running
                else "Hermes Telegram gateway did not report healthy after repair."
            ),
        }

    # Restart path: a just-saved secret must reach the new gateway process,
    # so reload env files into this process before the stop/start pair.
    env_reload = load_env_files_into_process(env_candidates)
    restart = await _run_gateway_restart(
        ctx,
        hermes_bin=hermes_bin,
        reason=request_reason,
        deadline_seconds=deadline_seconds,
    )
    healthy = bool(restart.get("functionally_verified"))
    healed = healthy
    functionally_verified = bool(restart.get("functionally_verified"))
    readiness = restart["readiness"] or {}
    restart_result = restart["restart_result"]
    if healthy:
        result_reason = "gateway_restart_verified"
    else:
        result_reason = str(
            restart.get("failure_reason") or "gateway_restart_unverified"
        )
    return {
        "schema": "tinyhat_hermes_heal_v1",
        "healthy": healthy,
        "healed": healed,
        "reason": result_reason,
        "telegram": telegram,
        "gateway": readiness.get("status"),
        "hermes": None,
        "env_reload": env_reload,
        "telegram_network": telegram_network,
        "restart_result": restart_result,
        "restart_fallback": restart.get("fallback_actions") or {},
        "restart": _restart_summary(
            requested=True,
            performed=bool(restart.get("performed")),
            deadline_seconds=deadline_seconds,
            deadline_exceeded=bool(restart["deadline_exceeded"]),
            telegram_evidence=str(
                readiness.get("telegram_evidence") or "unavailable"
            ),
            milestones_ms=restart["milestones_ms"],
            compat_defaulted=compat_defaulted,
            verified=bool(restart.get("verified")),
            functionally_verified=functionally_verified,
            method=restart.get("method"),
            fallback_attempted=bool(restart.get("fallback_attempted")),
            generation=restart.get("generation"),
        ),
        "readiness": readiness,
        "message": (
            "Hermes Telegram gateway restarted and functionally verified."
            if functionally_verified
            else (
                "Hermes Telegram gateway restart could not be verified: "
                f"{result_reason}."
            )
        ),
    }
