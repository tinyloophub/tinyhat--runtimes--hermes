"""Functional readiness probing for the Hermes Telegram gateway.

"Functionally ready" means two things at once:

1. ``hermes gateway status`` reports healthy (the same
   ``_gateway_status_is_healthy`` check the rest of the runtime uses), and
2. there is affirmative Telegram evidence: the gateway logged its Telegram
   connect marker (lines like ``[Telegram] Connected to Telegram``) *after*
   the moment the caller's restart began.

Telegram evidence collection is best-effort, but affirmative evidence is
required for functional readiness. It is reported by level:

- ``"log"`` — the foreground/fallback gateway log file this runtime manages
  (``<state dir>/hermes-gateway.log``) received new bytes after the restart
  began, and only those appended bytes are scanned for the marker. The public
  Hermes CLI exposes no ``gateway log`` / log-path facility, and in service
  mode the gateway logs to the systemd journal, so this level only fires for
  the runtime's own foreground gateway. A stale foreground log that never
  grows carries no signal and does not count as this level.
- ``"journal"`` — ``journalctl --user -u hermes-gateway.service`` output for
  the exact new ``_SYSTEMD_INVOCATION_ID`` scanned for the marker (service
  mode).
- ``"unavailable"`` — neither source is usable. Callers can distinguish that
  weaker status-only result from a fresh source that is available but has not
  emitted a Telegram-ready marker yet.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from hermes_runtime.commands.configure_telegram import (
    _compact_process,
    _gateway_status_is_healthy,
)
from hermes_runtime.hermes_cli import run_process

GATEWAY_SERVICE_NAME = "hermes-gateway.service"
STATUS_PROBE_TIMEOUT_SECONDS = 15
JOURNAL_PROBE_TIMEOUT_SECONDS = 15
# Positive markers for an established Telegram connection.  A readable source
# with fresh output but no marker returns False so a restart transaction can
# keep polling.  None is reserved for a source that cannot provide evidence.
TELEGRAM_CONNECTED_MARKERS = (
    "connected to telegram",
    "gateway running with",
    "started polling",
    "[telegram] connected",
)
_LOG_SCAN_MAX_BYTES = 262_144


def gateway_status_reports_telegram_fatal(result: dict[str, Any] | None) -> bool:
    """Whether Hermes' official status reports a fatal Telegram adapter."""
    if not isinstance(result, dict):
        return False
    text = f"{result.get('stdout') or ''}\n{result.get('stderr') or ''}".lower()
    return any(
        line.strip().startswith(("⚠ telegram:", "warning: telegram:"))
        for line in text.splitlines()
    )


def gateway_log_size(path: Path | None) -> int:
    """Return the gateway log's current size (0 when missing/unreadable).

    Callers snapshot this before a restart so a later probe only scans bytes
    the gateway appended after the restart began.
    """
    if path is None:
        return 0
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _log_telegram_evidence(path: Path | None, offset: int) -> bool | None:
    """Marker present in bytes appended after ``offset``; None = unusable."""
    if path is None:
        return None
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size <= offset:
        # No new content since the restart began. In service mode this file
        # is never written, so it carries no signal either way.
        return None
    start = max(offset, size - _LOG_SCAN_MAX_BYTES)
    try:
        with path.open("rb") as handle:
            handle.seek(start)
            appended = handle.read(_LOG_SCAN_MAX_BYTES)
    except OSError:
        return None
    text = appended.decode("utf-8", errors="replace").lower()
    return bool(any(m in text for m in TELEGRAM_CONNECTED_MARKERS))


async def _journal_telegram_evidence(
    since_unix: float,
    *,
    service_manager: str = "user",
    service_invocation_id: str | None = None,
    timeout_seconds: float = JOURNAL_PROBE_TIMEOUT_SECONDS,
) -> bool | None:
    """Marker from one exact service invocation; None = unusable."""
    journalctl = shutil.which("journalctl")
    invocation_id = str(service_invocation_id or "").strip()
    if not journalctl or not invocation_id:
        return None
    command = [journalctl]
    if service_manager == "user":
        command.append("--user")
    command.extend(
        [
            "-u",
            GATEWAY_SERVICE_NAME,
            f"_SYSTEMD_INVOCATION_ID={invocation_id}",
            f"--since=@{int(since_unix)}",
            "--no-pager",
            "--output=cat",
        ]
    )
    result = await run_process(
        command,
        timeout_seconds=timeout_seconds,
    )
    if not result.get("ok"):
        return None
    text = str(result.get("stdout") or "").lower()
    return bool(any(m in text for m in TELEGRAM_CONNECTED_MARKERS))


async def probe_functional_readiness(
    hermes_bin: Path,
    *,
    since_unix: float,
    log_path: Path | None = None,
    log_offset: int = 0,
    service_manager: str = "user",
    service_invocation_id: str | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """One functional-readiness probe (status + best-effort Telegram evidence).

    Returns ``ready`` true only when both the status probe and affirmative
    Telegram evidence are present.  A missing evidence source is unknown, not
    success.
    """
    probe_budget = max(
        0.2,
        float(
            timeout_seconds
            if timeout_seconds is not None
            else STATUS_PROBE_TIMEOUT_SECONDS + JOURNAL_PROBE_TIMEOUT_SECONDS
        ),
    )
    step_timeout = max(0.1, probe_budget / 2)
    status = await run_process(
        [str(hermes_bin), "gateway", "status"],
        timeout_seconds=min(STATUS_PROBE_TIMEOUT_SECONDS, step_timeout),
    )
    telegram_fatal = gateway_status_reports_telegram_fatal(status)
    status_healthy = _gateway_status_is_healthy(status) and not telegram_fatal

    telegram_evidence = "unavailable"
    telegram_connected: bool | None = None
    invocation_id = str(service_invocation_id or "").strip()
    # A service restart must be tied to the new systemd invocation. An
    # unfiltered foreground log can still receive bytes from the old process
    # during shutdown, so it is not valid evidence for a systemd generation.
    log_result = (
        None if invocation_id else _log_telegram_evidence(log_path, log_offset)
    )
    if invocation_id:
        journal_result = await _journal_telegram_evidence(
            since_unix,
            service_manager=service_manager,
            service_invocation_id=invocation_id,
            timeout_seconds=min(JOURNAL_PROBE_TIMEOUT_SECONDS, step_timeout),
        )
        if journal_result is not None:
            telegram_evidence, telegram_connected = "journal", journal_result
    elif log_result is True:
        telegram_evidence, telegram_connected = "log", True
    else:
        journal_result = await _journal_telegram_evidence(
            since_unix,
            service_manager=service_manager,
            service_invocation_id=None,
            timeout_seconds=min(JOURNAL_PROBE_TIMEOUT_SECONDS, step_timeout),
        )
        if journal_result is not None:
            telegram_evidence, telegram_connected = "journal", journal_result
        elif log_result is not None:
            telegram_evidence, telegram_connected = "log", log_result

    functionally_ready = status_healthy and telegram_connected is True
    return {
        "ready": functionally_ready,
        "functionally_ready": functionally_ready,
        "status_healthy": status_healthy,
        "telegram_fatal": telegram_fatal,
        "telegram_evidence": telegram_evidence,
        "telegram_connected": telegram_connected,
        "telegram_evidence_available": telegram_connected is not None,
        "status": _compact_process(status),
    }
