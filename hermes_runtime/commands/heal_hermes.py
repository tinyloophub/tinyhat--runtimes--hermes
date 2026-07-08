"""Repair the local Hermes messaging runtime without fetching new secrets.

The command is intentionally bounded to already-configured Computers. It
reloads Tinyhat-managed env files, verifies that Telegram credentials are
present, and then either:

- default (``spec.restart`` absent/false): reuses the same durable gateway
  start path as ``start_hermes`` — a bounded start-only heal that no-ops on
  an already-healthy gateway; or
- ``spec.restart=true``: performs a durable one-shot gateway restart owned
  end-to-end by this runtime process (which lives outside the gateway's
  control group, so the follow-up start can never be orphaned by the stop's
  kill sweep): reload env files, run the official ``hermes gateway
  restart`` command, then
  poll *functional* readiness — ``hermes gateway status`` healthy plus
  best-effort Telegram connect evidence from the gateway log or journal —
  until ``spec.deadline_seconds`` (default 90, clamped 30..300). ``healthy``
  is true only when functional readiness was verified within the deadline.

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

from hermes_runtime.commands import start_hermes, stop_hermes
from hermes_runtime.commands.configure_telegram import (
    _compact_process,
    _env_file_candidates,
    _gateway_log_path,
)
from hermes_runtime.gateway_readiness import (
    gateway_log_size,
    probe_functional_readiness,
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
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "requested": requested,
        "performed": performed,
        "deadline_seconds": deadline_seconds,
        "deadline_exceeded": deadline_exceeded,
        "milestones_ms": milestones_ms or {},
    }
    if telegram_evidence is not None:
        summary["telegram_evidence"] = telegram_evidence
    return summary


async def _run_gateway_restart(
    ctx: Any,
    *,
    hermes_bin: Path,
    reason: str,
    deadline_seconds: int,
) -> dict[str, Any]:
    """Restart via official ``hermes gateway restart`` then verify readiness."""
    started = _monotonic()
    since_unix = time.time()
    log_path = _gateway_log_path()
    log_offset = gateway_log_size(log_path)

    def _ms() -> int:
        return int((_monotonic() - started) * 1000)

    # Reload env into this process for parity with the start-only heal path;
    # the gateway itself re-reads ~/.hermes/.env (where the saved secret lives)
    # when it restarts.
    try:
        load_env_files_into_process(_env_file_candidates())
    except Exception:  # noqa: BLE001 - env reload is best-effort.
        pass

    milestones: dict[str, int | None] = {"restart_started": _ms()}
    # Use the official hermes gateway restart -- a single atomic Hermes CLI
    # command that stops and starts the installed gateway service. This
    # replaces a hand-rolled stop+start whose start step could be fooled by a
    # stopped-but-exit-0 gateway status into skipping the restart entirely
    # (the live secret-save failure mode). run_process injects the root
    # user-manager bus env so the CLI's internal systemctl --user reaches
    # uid 0's user manager from the system-service runtime. Verified live: this
    # brings the gateway to active + polling Telegram within seconds on a GCE
    # Hermes Computer, where the hand-rolled path left it foreground-degraded.
    restart_result = await run_process(
        [str(hermes_bin), "gateway", "restart"],
        timeout_seconds=min(180, max(60, deadline_seconds)),
    )
    milestones["restart_done"] = _ms()
    # The restart command's own result is part of the success contract. If
    # ``hermes gateway restart`` reports failure, a status-only "active
    # (running)" reading can be the OLD gateway that was never cycled (with
    # the just-saved secret still unread), so we must not accept it as
    # verified -- readiness evidence is intentionally status-only when
    # Telegram evidence is unavailable (see gateway_readiness). Report the
    # command failure honestly instead.
    restart_command_ok = bool(restart_result.get("ok"))

    readiness: dict[str, Any] | None = None
    verified = False
    deadline_exceeded = False
    if not restart_command_ok:
        # One diagnostic probe, but never mark verified on a failed restart.
        readiness = await probe_functional_readiness(
            hermes_bin,
            since_unix=since_unix,
            log_path=log_path,
            log_offset=log_offset,
        )
        milestones["verified"] = None
    while restart_command_ok:
        readiness = await probe_functional_readiness(
            hermes_bin,
            since_unix=since_unix,
            log_path=log_path,
            log_offset=log_offset,
        )
        if readiness.get("ready"):
            verified = True
            milestones["verified"] = _ms()
            break
        remaining = deadline_seconds - (_monotonic() - started)
        if remaining <= 0:
            deadline_exceeded = True
            milestones["verified"] = None
            break
        await asyncio.sleep(min(RESTART_VERIFY_POLL_SECONDS, remaining))

    return {
        "verified": verified,
        "deadline_exceeded": deadline_exceeded,
        "restart_command_ok": restart_command_ok,
        "milestones_ms": milestones,
        "readiness": readiness,
        "restart_result": _compact_process(restart_result),
    }


async def run(ctx: Any, command: dict[str, Any]) -> dict[str, Any]:
    raw_spec = command.get("spec")
    spec: dict[str, Any] = raw_spec if isinstance(raw_spec, dict) else {}
    restart_requested = _bool_spec(spec, "restart", False)
    deadline_seconds = _restart_deadline_seconds(spec)
    reason = str(spec.get("reason") or "").strip() or "heal_hermes"

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
            ),
            "message": "Telegram is not configured; run configure_telegram first.",
        }

    if not restart_requested:
        start_result = await start_hermes.run(
            ctx,
            {"kind": "start_hermes", "spec": {"reason": reason}},
        )
        healthy = bool(start_result.get("healthy"))
        return {
            "schema": "tinyhat_hermes_heal_v1",
            "healthy": healthy,
            "healed": healthy,
            "reason": "gateway_healthy" if healthy else "gateway_unhealthy",
            "telegram": telegram,
            "gateway": start_result.get("gateway"),
            "hermes": start_result.get("hermes"),
            "env_reload": start_result.get("env_reload"),
            "start_hermes": start_result,
            "restart": _restart_summary(
                requested=False,
                performed=False,
                deadline_seconds=deadline_seconds,
            ),
            "message": (
                "Hermes Telegram gateway is healthy."
                if healthy
                else "Hermes Telegram gateway did not report healthy after repair."
            ),
        }

    # Restart path: a just-saved secret must reach the new gateway process,
    # so reload env files into this process before the stop/start pair.
    env_reload = load_env_files_into_process(_env_file_candidates())
    restart = await _run_gateway_restart(
        ctx,
        hermes_bin=hermes_bin,
        reason=reason,
        deadline_seconds=deadline_seconds,
    )
    healthy = bool(restart["verified"])
    restart_command_ok = bool(restart.get("restart_command_ok"))
    readiness = restart["readiness"] or {}
    restart_result = restart["restart_result"]
    if healthy:
        reason = "gateway_restart_verified"
    elif not restart_command_ok:
        reason = "gateway_restart_command_failed"
    else:
        reason = "gateway_restart_deadline_exceeded"
    return {
        "schema": "tinyhat_hermes_heal_v1",
        "healthy": healthy,
        "healed": healthy,
        "reason": reason,
        "telegram": telegram,
        "gateway": readiness.get("status"),
        "hermes": None,
        "env_reload": env_reload,
        "restart_result": restart_result,
        "restart": _restart_summary(
            requested=True,
            performed=True,
            deadline_seconds=deadline_seconds,
            deadline_exceeded=bool(restart["deadline_exceeded"]),
            telegram_evidence=str(
                readiness.get("telegram_evidence") or "unavailable"
            ),
            milestones_ms=restart["milestones_ms"],
        ),
        "readiness": readiness,
        "message": (
            "Hermes Telegram gateway restarted and functionally verified."
            if healthy
            else (
                "The `hermes gateway restart` command failed; the gateway "
                "was not restarted."
                if not restart_command_ok
                else (
                    "Hermes Telegram gateway restart did not verify within "
                    f"{deadline_seconds}s."
                )
            )
        ),
    }
