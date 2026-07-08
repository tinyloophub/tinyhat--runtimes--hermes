"""Functional readiness probing for the Hermes Telegram gateway.

"Functionally ready" means two things at once:

1. ``hermes gateway status`` reports healthy (the same
   ``_gateway_status_is_healthy`` check the rest of the runtime uses), and
2. there is affirmative Telegram evidence: the gateway logged its Telegram
   connect marker (lines like ``[Telegram] Connected to Telegram``) *after*
   the moment the caller's restart began.

Telegram evidence is best-effort and reported by level:

- ``"log"`` — the foreground/fallback gateway log file this runtime manages
  (``<state dir>/hermes-gateway.log``) received new bytes after the restart
  began, and only those appended bytes are scanned for the marker. The public
  Hermes CLI exposes no ``gateway log`` / log-path facility, and in service
  mode the gateway logs to the systemd journal, so this level only fires for
  the runtime's own foreground gateway. A stale foreground log that never
  grows carries no signal and does not count as this level.
- ``"journal"`` — ``journalctl --user -u hermes-gateway.service --since
  @<restart unix ts>`` output scanned for the marker (service mode).
- ``"unavailable"`` — neither source is usable. Callers must never fail
  solely because evidence is unavailable; readiness falls back to the status
  probe alone and the achieved level is reported so the platform can see how
  strong the verification was.
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
TELEGRAM_CONNECTED_MARKER = "connected to telegram"
_LOG_SCAN_MAX_BYTES = 262_144


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
    return TELEGRAM_CONNECTED_MARKER in appended.decode(
        "utf-8", errors="replace"
    ).lower()


async def _journal_telegram_evidence(since_unix: float) -> bool | None:
    """Marker present in the unit's journal since the restart; None = unusable."""
    journalctl = shutil.which("journalctl")
    if not journalctl:
        return None
    result = await run_process(
        [
            journalctl,
            "--user",
            "-u",
            GATEWAY_SERVICE_NAME,
            f"--since=@{int(since_unix)}",
            "--no-pager",
            "--output=cat",
        ],
        timeout_seconds=JOURNAL_PROBE_TIMEOUT_SECONDS,
    )
    if not result.get("ok"):
        return None
    return TELEGRAM_CONNECTED_MARKER in str(result.get("stdout") or "").lower()


async def probe_functional_readiness(
    hermes_bin: Path,
    *,
    since_unix: float,
    log_path: Path | None = None,
    log_offset: int = 0,
) -> dict[str, Any]:
    """One functional-readiness probe (status + best-effort Telegram evidence).

    Returns ``ready`` true when the status probe is healthy and Telegram
    evidence is affirmative or unavailable — an available-but-negative
    evidence source keeps ``ready`` false so callers keep polling.
    """
    status = await run_process(
        [str(hermes_bin), "gateway", "status"],
        timeout_seconds=STATUS_PROBE_TIMEOUT_SECONDS,
    )
    status_healthy = _gateway_status_is_healthy(status)

    telegram_evidence = "unavailable"
    telegram_connected: bool | None = None
    log_result = _log_telegram_evidence(log_path, log_offset)
    if log_result is True:
        telegram_evidence, telegram_connected = "log", True
    else:
        journal_result = await _journal_telegram_evidence(since_unix)
        if journal_result is not None:
            telegram_evidence, telegram_connected = "journal", journal_result
        elif log_result is not None:
            telegram_evidence, telegram_connected = "log", log_result

    ready = status_healthy and telegram_connected is not False
    return {
        "ready": ready,
        "status_healthy": status_healthy,
        "telegram_evidence": telegram_evidence,
        "telegram_connected": telegram_connected,
        "status": _compact_process(status),
    }
