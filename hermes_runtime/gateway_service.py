"""Bounded, unit-scoped helpers for the public Hermes gateway service.

The recovery path uses only systemd's documented unit interface.  It never
searches for arbitrary processes: a destructive action is allowed only after
exactly one systemd manager has proven that it owns the loaded
``hermes-gateway.service`` unit.
"""

from __future__ import annotations

import shutil
from typing import Any

from hermes_runtime.hermes_cli import run_process

GATEWAY_SERVICE_NAME = "hermes-gateway.service"
SYSTEMCTL_PROBE_TIMEOUT_SECONDS = 5
SYSTEMCTL_ACTION_TIMEOUT_SECONDS = 5
_SHOW_PROPERTIES = (
    "LoadState,ActiveState,SubState,Result,MainPID,InvocationID,"
    "ActiveEnterTimestampMonotonic,ExecMainStartTimestampMonotonic"
)


def _manager_command(systemctl: str, manager: str, *args: str) -> list[str]:
    command = [systemctl]
    if manager == "user":
        command.append("--user")
    command.extend(args)
    return command


def _parse_properties(stdout: str) -> dict[str, str]:
    properties: dict[str, str] = {}
    for raw_line in stdout.splitlines():
        key, separator, value = raw_line.partition("=")
        if separator and key:
            properties[key] = value.strip()
    return properties


def _nonnegative_int(value: str | None) -> int:
    try:
        return max(0, int(value or "0"))
    except (TypeError, ValueError):
        return 0


def public_gateway_generation(
    snapshot: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return only non-sensitive unit identity/readiness fields."""
    if not isinstance(snapshot, dict):
        return None
    return {
        "manager": snapshot.get("manager"),
        "load_state": snapshot.get("load_state"),
        "active_state": snapshot.get("active_state"),
        "sub_state": snapshot.get("sub_state"),
        "result": snapshot.get("result"),
        "main_pid": snapshot.get("main_pid"),
        "invocation_id": snapshot.get("invocation_id"),
        "active_enter_timestamp_monotonic": snapshot.get(
            "active_enter_timestamp_monotonic"
        ),
        "exec_main_start_timestamp_monotonic": snapshot.get(
            "exec_main_start_timestamp_monotonic"
        ),
    }


async def _probe_gateway_service(
    owner: dict[str, str],
    *,
    timeout_seconds: float = SYSTEMCTL_PROBE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Read one manager while preserving missing vs unavailable."""
    systemctl = str(owner.get("systemctl") or "")
    manager = str(owner.get("manager") or "")
    if not systemctl or manager not in {"system", "user"}:
        return {"state": "unavailable", "snapshot": None}
    result = await run_process(
        _manager_command(
            systemctl,
            manager,
            "show",
            GATEWAY_SERVICE_NAME,
            "--no-pager",
            f"--property={_SHOW_PROPERTIES}",
        ),
        timeout_seconds=timeout_seconds,
    )
    if not result.get("ok"):
        return {"state": "unavailable", "snapshot": None}
    properties = _parse_properties(str(result.get("stdout") or ""))
    if properties.get("LoadState") != "loaded":
        return {"state": "missing", "snapshot": None}
    snapshot = {
        "manager": manager,
        "load_state": properties.get("LoadState") or "unknown",
        "active_state": properties.get("ActiveState") or "unknown",
        "sub_state": properties.get("SubState") or "unknown",
        "result": properties.get("Result") or "unknown",
        "main_pid": _nonnegative_int(properties.get("MainPID")),
        "invocation_id": (properties.get("InvocationID") or "").strip() or None,
        "active_enter_timestamp_monotonic": _nonnegative_int(
            properties.get("ActiveEnterTimestampMonotonic")
        ),
        "exec_main_start_timestamp_monotonic": _nonnegative_int(
            properties.get("ExecMainStartTimestampMonotonic")
        ),
    }
    return {"state": "loaded", "snapshot": snapshot}


async def snapshot_gateway_service(
    owner: dict[str, str],
    *,
    timeout_seconds: float = SYSTEMCTL_PROBE_TIMEOUT_SECONDS,
) -> dict[str, Any] | None:
    """Read the gateway unit generation from its already-proven manager."""
    probe = await _probe_gateway_service(owner, timeout_seconds=timeout_seconds)
    snapshot = probe.get("snapshot")
    return snapshot if isinstance(snapshot, dict) else None


async def discover_gateway_service() -> dict[str, Any]:
    """Find the single manager that owns the loaded gateway unit.

    Both managers are inspected.  If both own a loaded unit, recovery fails
    closed instead of guessing which process may be killed.
    """
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return {
            "ok": False,
            "reason": "systemctl_unavailable",
            "owner": None,
            "generation": None,
        }

    candidates: list[tuple[dict[str, str], dict[str, Any]]] = []
    unavailable_managers: list[str] = []
    for manager in ("system", "user"):
        owner = {"manager": manager, "systemctl": systemctl}
        probe = await _probe_gateway_service(owner)
        snapshot = probe.get("snapshot")
        if isinstance(snapshot, dict):
            candidates.append((owner, snapshot))
        elif probe.get("state") == "unavailable":
            unavailable_managers.append(manager)

    if not candidates:
        return {
            "ok": False,
            "reason": (
                "gateway_service_probe_unavailable"
                if unavailable_managers
                else "gateway_service_not_found"
            ),
            "owner": None,
            "generation": None,
        }
    # More than one loaded unit is ambiguous even when only one is currently
    # live.  The official Hermes CLI may address a different manager than the
    # one we would select here, which could start a second Telegram poller.
    if len(candidates) > 1:
        return {
            "ok": False,
            "reason": "gateway_service_owner_ambiguous",
            "owner": None,
            "generation": None,
            "candidate_managers": [
                owner["manager"] for owner, _ in candidates
            ],
        }
    if len(candidates) == 1 and not unavailable_managers:
        owner, generation = candidates[0]
    else:
        return {
            "ok": False,
            "reason": "gateway_service_owner_ambiguous",
            "owner": None,
            "generation": None,
            "candidate_managers": [owner["manager"] for owner, _ in candidates],
        }
    return {
        "ok": True,
        "reason": "gateway_service_owner_found",
        "owner": owner,
        "generation": generation,
    }


def gateway_generation_changed(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> bool:
    """Whether ``after`` positively identifies a different service run."""
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    if before.get("manager") != after.get("manager"):
        return False

    before_invocation = str(before.get("invocation_id") or "")
    after_invocation = str(after.get("invocation_id") or "")
    if before_invocation and after_invocation:
        return before_invocation != after_invocation

    before_pid = _nonnegative_int(str(before.get("main_pid") or "0"))
    after_pid = _nonnegative_int(str(after.get("main_pid") or "0"))
    if after_pid > 0 and before_pid != after_pid:
        return True

    before_started = _nonnegative_int(
        str(before.get("active_enter_timestamp_monotonic") or "0")
    )
    after_started = _nonnegative_int(
        str(after.get("active_enter_timestamp_monotonic") or "0")
    )
    if after_started > 0 and before_started != after_started:
        return True
    before_exec_started = _nonnegative_int(
        str(before.get("exec_main_start_timestamp_monotonic") or "0")
    )
    after_exec_started = _nonnegative_int(
        str(after.get("exec_main_start_timestamp_monotonic") or "0")
    )
    return after_exec_started > 0 and before_exec_started != after_exec_started


def gateway_generation_same(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> bool:
    """Whether two snapshots positively identify the same service run."""
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    if before.get("manager") != after.get("manager"):
        return False
    before_invocation = str(before.get("invocation_id") or "")
    after_invocation = str(after.get("invocation_id") or "")
    if before_invocation and after_invocation:
        return before_invocation == after_invocation
    before_pid = _nonnegative_int(str(before.get("main_pid") or "0"))
    after_pid = _nonnegative_int(str(after.get("main_pid") or "0"))
    if before_pid > 0 or after_pid > 0:
        return before_pid == after_pid
    before_started = _nonnegative_int(
        str(before.get("active_enter_timestamp_monotonic") or "0")
    )
    after_started = _nonnegative_int(
        str(after.get("active_enter_timestamp_monotonic") or "0")
    )
    if before_started > 0 or after_started > 0:
        return before_started == after_started
    before_exec_started = _nonnegative_int(
        str(before.get("exec_main_start_timestamp_monotonic") or "0")
    )
    after_exec_started = _nonnegative_int(
        str(after.get("exec_main_start_timestamp_monotonic") or "0")
    )
    if before_exec_started > 0 or after_exec_started > 0:
        return before_exec_started == after_exec_started
    return False


def gateway_generation_active(snapshot: dict[str, Any] | None) -> bool:
    return bool(
        isinstance(snapshot, dict)
        and snapshot.get("active_state") == "active"
        and snapshot.get("sub_state") == "running"
        and _nonnegative_int(str(snapshot.get("main_pid") or "0")) > 0
    )


def gateway_generation_live(snapshot: dict[str, Any] | None) -> bool:
    return bool(
        isinstance(snapshot, dict)
        and snapshot.get("active_state")
        in {"active", "activating", "deactivating", "reloading"}
        and _nonnegative_int(str(snapshot.get("main_pid") or "0")) > 0
    )


def gateway_generation_needs_force_kill(
    snapshot: dict[str, Any] | None,
) -> bool:
    """Whether systemd may still own processes that need a cgroup kill.

    Transitional units can report MainPID=0 while residual cgroup processes
    keep stop jobs wedged.  State, rather than MainPID, is the safety signal
    because the unit owner was already proven by discovery.
    """
    return bool(
        isinstance(snapshot, dict)
        and snapshot.get("active_state")
        in {"active", "activating", "deactivating", "reloading"}
    )


async def run_gateway_service_action(
    owner: dict[str, str],
    action: str,
    *,
    timeout_seconds: float = SYSTEMCTL_ACTION_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run one allowlisted action against the proven gateway unit only."""
    systemctl = str(owner.get("systemctl") or "")
    manager = str(owner.get("manager") or "")
    if not systemctl or manager not in {"system", "user"}:
        return {
            "ok": False,
            "returncode": None,
            "timed_out": False,
            "stderr": "gateway service owner is unavailable",
        }
    action_args: dict[str, tuple[str, ...]] = {
        "kill": (
            "kill",
            "--signal=SIGKILL",
            "--kill-whom=all",
            GATEWAY_SERVICE_NAME,
        ),
        "reset_failed": ("reset-failed", GATEWAY_SERVICE_NAME),
        "start": ("--no-block", "start", GATEWAY_SERVICE_NAME),
    }
    args = action_args.get(action)
    if args is None:
        return {
            "ok": False,
            "returncode": None,
            "timed_out": False,
            "stderr": "gateway service action is not allowed",
        }
    return await run_process(
        _manager_command(systemctl, manager, *args),
        timeout_seconds=timeout_seconds,
    )


__all__ = [
    "discover_gateway_service",
    "gateway_generation_active",
    "gateway_generation_changed",
    "gateway_generation_live",
    "gateway_generation_needs_force_kill",
    "gateway_generation_same",
    "public_gateway_generation",
    "run_gateway_service_action",
    "snapshot_gateway_service",
]
