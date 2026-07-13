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
- ``spec.restart=true``: performs a durable one-shot gateway restart owned
  end-to-end by this runtime process (which lives outside the gateway's
  control group, so the follow-up start can never be orphaned by the stop's
  kill sweep): reload env files, run the official ``hermes gateway
  restart`` command for a bounded grace period.  If the same systemd
  generation remains, it narrowly force-cycles only the proven owner of
  ``hermes-gateway.service``.  It then verifies a different active generation,
  healthy ``hermes gateway status``, and fresh Telegram connect evidence tied
  to the new systemd invocation. Missing evidence never reports healed.

It does not call the Telegram setup endpoint, mint bot tokens, unassign the
Computer, or restart the Tinyhat runtime service. No secret values are logged
or returned; results carry env-variable *names* only.

Example input:
    {"kind": "heal_hermes", "spec": {"restart": true, "deadline_seconds": 90,
     "reason": "secret_saved_restart"}}

The result's ``restart`` object reports {requested, performed,
deadline_seconds, deadline_exceeded, telegram_evidence, milestones_ms}
where ``milestones_ms`` carries millisecond offsets from the restart start
for restart_started / restart_done / verified. The restart itself uses the
official ``hermes gateway restart`` command.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from hermes_runtime.commands import start_hermes
from hermes_runtime.commands.configure_telegram import (
    _compact_process,
    _env_file_candidates,
    _gateway_log_path,
    ensure_telegram_network_fallback_env,
)
from hermes_runtime.gateway_readiness import (
    gateway_log_size,
    probe_functional_readiness,
)
from hermes_runtime.gateway_service import (
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

# Module alias so tests can inject a fake clock without touching the global
# ``time`` module that asyncio itself relies on.
_monotonic = time.monotonic


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


async def _run_gateway_restart(
    ctx: Any,
    *,
    hermes_bin: Path,
    reason: str,
    deadline_seconds: int,
) -> dict[str, Any]:
    """Restart one proven gateway unit and verify a new generation."""
    del ctx, reason
    started = _monotonic()
    since_unix = time.time()
    log_path = _gateway_log_path()
    log_offset = gateway_log_size(log_path)

    def _ms() -> int:
        return int((_monotonic() - started) * 1000)

    def _remaining() -> float:
        return max(0.0, deadline_seconds - (_monotonic() - started))

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
    owner = discovery.get("owner") if discovery.get("ok") else None
    before = discovery.get("generation") if discovery.get("ok") else None
    if not isinstance(owner, dict) or not isinstance(before, dict):
        return {
            "verified": False,
            "functionally_verified": False,
            "deadline_exceeded": False,
            "restart_command_ok": False,
            "performed": False,
            "method": None,
            "fallback_attempted": False,
            "failure_reason": str(discovery.get("reason") or "service_unknown"),
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

    # Preserve enough of the overall deadline for a unit-scoped fallback.  A
    # timed-out Hermes CLI is terminated with its whole process group so an
    # orphaned systemctl restart cannot race the fallback below.
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
    after = await snapshot_gateway_service(
        owner,
        timeout_seconds=max(1.0, min(SERVICE_ACTION_TIMEOUT_SECONDS, _remaining())),
    )
    generation_changed = gateway_generation_changed(before, after)
    fallback_attempted = False
    fallback_actions: dict[str, Any] = {}
    failure_reason: str | None = None
    deadline_exceeded = False

    if not generation_changed:
        # Destructive recovery is allowed only when a fresh owner resolution
        # still identifies the same old unit generation.  This closes the race
        # where the official command completes just as its grace period ends.
        rediscovery = await discover_gateway_service()
        current_owner = rediscovery.get("owner") if rediscovery.get("ok") else None
        current = (
            rediscovery.get("generation") if rediscovery.get("ok") else None
        )
        if (
            isinstance(current_owner, dict)
            and current_owner.get("manager") == owner.get("manager")
            and gateway_generation_changed(before, current)
        ):
            after = current
            generation_changed = True
        elif not (
            isinstance(current_owner, dict)
            and current_owner.get("manager") == owner.get("manager")
            and gateway_generation_same(before, current)
        ):
            after = current if isinstance(current, dict) else after
            failure_reason = str(
                rediscovery.get("reason") or "gateway_generation_not_proven"
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
                kill_result = await run_gateway_service_action(
                    owner,
                    "kill",
                    timeout_seconds=max(
                        1.0, min(SERVICE_ACTION_TIMEOUT_SECONDS, _remaining())
                    ),
                )
                fallback_actions["kill"] = _action_summary(kill_result)
                if not kill_result.get("ok"):
                    raced = await snapshot_gateway_service(
                        owner,
                        timeout_seconds=max(
                            1.0, min(SERVICE_ACTION_TIMEOUT_SECONDS, _remaining())
                        ),
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
                for action in ("reset_failed", "start"):
                    action_result = await run_gateway_service_action(
                        owner,
                        action,
                        # The full force cycle was admitted only with the
                        # reserve above. Once kill succeeds, never abandon the
                        # unit before the paired start, even if a prior action
                        # consumes its whole slice.
                        timeout_seconds=SERVICE_ACTION_TIMEOUT_SECONDS,
                    )
                    fallback_actions[action] = _action_summary(action_result)
                    if action == "start" and not action_result.get("ok"):
                        failure_reason = "gateway_force_start_failed"
                        break

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
            timeout_seconds=max(
                1.0, min(SERVICE_ACTION_TIMEOUT_SECONDS, remaining)
            ),
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
                timeout_seconds=remaining,
            )
            if _remaining() <= 0:
                deadline_exceeded = True
                failure_reason = "gateway_restart_deadline_exceeded"
                break
            if readiness.get("status_healthy"):
                telegram_connected = readiness.get("telegram_connected")
                if telegram_connected is True:
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
