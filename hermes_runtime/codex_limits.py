"""Read OpenAI Codex usage limits through the Codex app-server CLI.

Tinyhat uses Hermes' formal CLI surface for auth. For usage limits, the formal
surface is Codex' app-server command:

    codex app-server --listen stdio://

This module starts that command, sends JSON-RPC messages over stdio, calls
``account/rateLimits/read``, and formats the response for Telegram. It does not
call the normal OpenAI REST API and it does not read OpenAI tokens directly.
Codex/Hermes own the user's auth state on the Computer.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

SCHEMA = "tinyhat_hermes_codex_limits_v1"
APP_SERVER_METHOD = "account/rateLimits/read"
DEFAULT_TIMEOUT_SECONDS = 60
MAX_STDERR_CHARS = 2000
DEFAULT_STATE_DIR = "/var/lib/tinyhat-hermes-runtime"


class CodexAppServerError(RuntimeError):
    """Raised when the Codex app-server command cannot return a useful result."""


def find_codex_binary() -> Path | None:
    explicit = (os.getenv("CODEX_BIN") or "").strip()
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    discovered = shutil.which("codex")
    if discovered:
        candidates.append(discovered)
    candidates.extend(
        [
            str(Path.home() / ".local" / "bin" / "codex"),
            "/usr/local/bin/codex",
            "/opt/homebrew/bin/codex",
        ]
    )
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


def _state_dir() -> Path:
    return Path(os.getenv("TINYHAT_RUNTIME_STATE_DIR") or DEFAULT_STATE_DIR)


def last_limits_snapshot_path(state_dir: Path | None = None) -> Path:
    return (state_dir or _state_dir()) / "codex" / "last_limits.json"


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(path)


def persist_limits_snapshot(result: dict[str, Any]) -> str | None:
    """Persist the structured Codex response for local inspection.

    The snapshot is deliberately based on Codex app-server JSON, not terminal
    output. It contains plan/usage data and command metadata, never OpenAI auth
    tokens.
    """

    try:
        path = last_limits_snapshot_path()
        _write_json_atomic(
            path,
            {
                "schema": SCHEMA,
                "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "source": result.get("source"),
                "method": result.get("method"),
                "duration_ms": result.get("duration_ms"),
                "limits": result.get("limits"),
                "summary": result.get("summary"),
            },
        )
        return str(path)
    except Exception:
        return None


async def _read_stderr_tail(
    process: asyncio.subprocess.Process,
    tail: list[str],
) -> None:
    if process.stderr is None:
        return
    while True:
        chunk = await process.stderr.read(512)
        if not chunk:
            return
        text = chunk.decode("utf-8", errors="replace")
        tail.append(text)
        joined = "".join(tail)
        if len(joined) > MAX_STDERR_CHARS:
            tail[:] = [joined[-MAX_STDERR_CHARS:]]


async def _request(
    process: asyncio.subprocess.Process,
    *,
    request_id: int,
    method: str,
    params: dict[str, Any] | None = None,
    timeout_seconds: int,
) -> dict[str, Any]:
    if process.stdin is None or process.stdout is None:
        raise CodexAppServerError("Codex app-server stdio is unavailable.")
    payload: dict[str, Any] = {"id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    process.stdin.write((json.dumps(payload, separators=(",", ":")) + "\n").encode())
    await process.stdin.drain()

    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CodexAppServerError(f"Codex app-server {method} timed out.")
        raw = await asyncio.wait_for(process.stdout.readline(), timeout=remaining)
        if not raw:
            raise CodexAppServerError("Codex app-server exited before replying.")
        try:
            message = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict) or message.get("id") != request_id:
            # Notifications such as remoteControl/status/changed can arrive
            # between replies. They are useful to Codex but not to this command.
            continue
        if "error" in message:
            error = message.get("error")
            if isinstance(error, dict):
                raise CodexAppServerError(str(error.get("message") or error))
            raise CodexAppServerError(str(error))
        result = message.get("result")
        return result if isinstance(result, dict) else {"value": result}


async def request_codex_rate_limits(
    *,
    codex_bin: Path | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    codex_bin = codex_bin or find_codex_binary()
    if codex_bin is None:
        raise CodexAppServerError("Codex CLI was not found.")

    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        str(codex_bin),
        "app-server",
        "--listen",
        "stdio://",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stderr_tail: list[str] = []
    stderr_task = asyncio.create_task(_read_stderr_tail(process, stderr_tail))
    try:
        initialize = await _request(
            process,
            request_id=1,
            method="initialize",
            params={
                "clientInfo": {
                    "name": "tinyhat-hermes-runtime",
                    "title": "Tinyhat Hermes runtime",
                    "version": "0.0.0",
                },
                "capabilities": {"experimentalApi": True},
            },
            timeout_seconds=timeout_seconds,
        )
        limits = await _request(
            process,
            request_id=2,
            method=APP_SERVER_METHOD,
            timeout_seconds=timeout_seconds,
        )
    finally:
        try:
            if process.stdin is not None:
                process.stdin.close()
        except Exception:
            pass
        if process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
        stderr_task.cancel()
        try:
            await stderr_task
        except asyncio.CancelledError:
            pass

    result = {
        "schema": SCHEMA,
        "ok": True,
        "source": "codex app-server",
        "method": APP_SERVER_METHOD,
        "codex_bin": str(codex_bin),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "initialize": initialize,
        "limits": limits,
        "summary": summarize_rate_limits(limits),
        "stderr_tail": "".join(stderr_tail)[-MAX_STDERR_CHARS:],
    }
    snapshot_path = persist_limits_snapshot(result)
    if snapshot_path:
        result["snapshot_path"] = snapshot_path
    return result


def _is_record(value: Any) -> bool:
    return isinstance(value, dict)


def _read_number(record: dict[str, Any], key: str) -> float | None:
    value = record.get(key)
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _read_string(record: dict[str, Any], key: str) -> str | None:
    value = record.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _format_duration(minutes: float) -> str:
    safe_minutes = max(0, int(round(minutes)))
    if safe_minutes < 60:
        return f"{safe_minutes}m"
    hours, mins = divmod(safe_minutes, 60)
    if mins == 0:
        return f"{hours}h"
    return f"{hours}h {mins}m"


def _format_amount(value: Any) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return f"{float(value):,.2f}".rstrip("0").rstrip(".")
    if isinstance(value, str):
        try:
            return f"{float(value):,.2f}".rstrip("0").rstrip(".")
        except ValueError:
            return value.strip()
    return str(value)


def _format_reset_short(seconds: float, *, now: float) -> str:
    delta = max(1, int(seconds - now))
    minutes = (delta + 59) // 60
    if minutes < 60:
        return f"in {minutes}m"
    hours = (minutes + 59) // 60
    if hours < 24:
        return f"in {hours}h"
    return f"in {(hours + 23) // 24}d"


def _format_reset_utc(seconds: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(seconds))


def _progress_bar(remaining_percent: float | None, *, width: int = 10) -> str:
    if remaining_percent is None:
        return "[??????????]"
    safe = max(0.0, min(100.0, remaining_percent))
    filled = int(round(safe / 100.0 * width))
    return "[" + ("█" * filled) + ("░" * (width - filled)) + "]"


def _format_reset(seconds: float, *, now: float) -> str:
    delta = max(1, int(seconds - now))
    minutes = (delta + 59) // 60
    if minutes < 60:
        rel = f"in {minutes}m"
    else:
        hours = (minutes + 59) // 60
        rel = f"in {hours}h" if hours < 24 else f"in {(hours + 23) // 24}d"
    return f"{rel} ({time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(seconds))})"


def _snapshot_label(snapshot: dict[str, Any]) -> str:
    label = _read_string(snapshot, "limitName") or _read_string(snapshot, "limitId")
    if not label or label == "codex":
        return "Codex"
    return label.replace("_", " ").replace("-", " ").strip()


def _window_summary(name: str, window: dict[str, Any], *, now: float) -> str:
    used = _read_number(window, "usedPercent")
    duration = _read_number(window, "windowDurationMins")
    resets_at = _read_number(window, "resetsAt")
    parts: list[str] = [name]
    if used is not None:
        remaining = max(0.0, 100.0 - used)
        parts.append(f"{remaining:.0f}% remaining")
        if duration is not None:
            parts.append(
                f"estimated quota left {_format_duration(duration * remaining / 100)}"
            )
    else:
        parts.append("usage unknown")
    if resets_at:
        parts.append(f"resets {_format_reset(resets_at, now=now)}")
    return ", ".join(parts)


def _collect_snapshots(value: Any, snapshots: list[dict[str, Any]], seen: set[str]) -> None:
    if isinstance(value, list):
        for item in value:
            _collect_snapshots(item, snapshots, seen)
        return
    if not isinstance(value, dict):
        return
    if (
        isinstance(value.get("primary"), dict)
        or isinstance(value.get("secondary"), dict)
        or value.get("limitId") is not None
        or value.get("limitName") is not None
    ):
        signature = json.dumps(
            {
                "limitId": value.get("limitId"),
                "limitName": value.get("limitName"),
                "primary": value.get("primary"),
                "secondary": value.get("secondary"),
            },
            sort_keys=True,
            default=str,
        )
        if signature not in seen:
            seen.add(signature)
            snapshots.append(value)
        return
    for key in ("rateLimitsByLimitId", "rateLimits", "data", "items"):
        _collect_snapshots(value.get(key), snapshots, seen)


def collect_rate_limit_snapshots(payload: dict[str, Any]) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    _collect_snapshots(payload, snapshots, set())
    snapshots.sort(key=lambda item: 0 if item.get("limitId") == "codex" else 1)
    return snapshots


def summarize_rate_limits(payload: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    now = now or time.time()
    snapshots = collect_rate_limit_snapshots(payload)
    summaries: list[dict[str, Any]] = []
    for snapshot in snapshots[:8]:
        windows = []
        for key, label in (("primary", "Primary"), ("secondary", "Weekly")):
            window = snapshot.get(key)
            if isinstance(window, dict):
                used_percent = _read_number(window, "usedPercent")
                window_duration_mins = _read_number(window, "windowDurationMins")
                remaining_percent = (
                    max(0.0, min(100.0, 100.0 - used_percent))
                    if used_percent is not None
                    else None
                )
                estimated_quota_left_mins = (
                    window_duration_mins * remaining_percent / 100.0
                    if window_duration_mins is not None
                    and remaining_percent is not None
                    else None
                )
                resets_at = _read_number(window, "resetsAt")
                windows.append(
                    {
                        "name": key,
                        "label": label,
                        "text": _window_summary(label, window, now=now),
                        "used_percent": used_percent,
                        "remaining_percent": remaining_percent,
                        "window_duration_mins": window_duration_mins,
                        "estimated_quota_left_mins": estimated_quota_left_mins,
                        "resets_at": resets_at,
                        "resets_in": (
                            _format_reset_short(resets_at, now=now)
                            if resets_at is not None
                            else None
                        ),
                        "resets_at_utc": (
                            _format_reset_utc(resets_at)
                            if resets_at is not None
                            else None
                        ),
                    }
                )
        summaries.append(
            {
                "limit_id": snapshot.get("limitId"),
                "label": _snapshot_label(snapshot),
                "plan_type": snapshot.get("planType"),
                "rate_limit_reached_type": snapshot.get("rateLimitReachedType"),
                "credits": snapshot.get("credits"),
                "windows": windows,
            }
        )
    return {
        "limits": summaries,
        "rate_limit_reset_credits": payload.get("rateLimitResetCredits"),
    }


def format_telegram_summary(result: dict[str, Any]) -> str:
    if not result.get("ok"):
        return str(result.get("message") or "Could not read OpenAI Codex limits.")
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    limits = summary.get("limits") if isinstance(summary.get("limits"), list) else []
    lines = ["OpenAI Codex usage limits"]
    if not limits:
        lines.append("Codex did not return account limits for this auth session.")
        return "\n".join(lines)
    for item in limits[:4]:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "Codex")
        header_parts = [label]
        if item.get("plan_type"):
            header_parts.append(f"plan {str(item['plan_type']).lower()}")
        if item.get("rate_limit_reached_type"):
            header_parts.append(f"limit reached: {item['rate_limit_reached_type']}")
        lines.append(f"\n{', '.join(header_parts)}")
        credits = item.get("credits")
        if isinstance(credits, dict):
            if credits.get("unlimited"):
                lines.append("Credits: unlimited")
            elif credits.get("balance") is not None:
                lines.append(f"Credits remaining: {_format_amount(credits['balance'])}")
        for window in item.get("windows") or []:
            if not isinstance(window, dict):
                continue
            window_label = str(window.get("label") or window.get("name") or "Window")
            remaining = (
                float(window["remaining_percent"])
                if isinstance(window.get("remaining_percent"), (int, float))
                else None
            )
            lines.append("")
            lines.append(window_label)
            lines.append(
                f"{_progress_bar(remaining)} "
                f"{remaining:.0f}% remaining"
                if remaining is not None
                else f"{_progress_bar(None)} remaining unknown"
            )
            estimated = window.get("estimated_quota_left_mins")
            if isinstance(estimated, (int, float)):
                lines.append(f"Estimated time left: {_format_duration(estimated)}")
            resets_in = window.get("resets_in")
            resets_at_utc = window.get("resets_at_utc")
            if resets_in and resets_at_utc:
                lines.append(f"Resets: {resets_in} ({resets_at_utc})")
            elif resets_in:
                lines.append(f"Resets: {resets_in}")
    reset_credits = summary.get("rate_limit_reset_credits")
    if isinstance(reset_credits, dict) and reset_credits.get("availableCount") is not None:
        lines.append(f"\nReset credits available: {reset_credits['availableCount']}")
    return "\n".join(lines).strip()


async def read_codex_limits() -> dict[str, Any]:
    try:
        return await request_codex_rate_limits()
    except Exception as exc:  # noqa: BLE001 - command result should explain failure.
        return {
            "schema": SCHEMA,
            "ok": False,
            "source": "codex app-server",
            "method": APP_SERVER_METHOD,
            "message": str(exc),
            "failure_code": exc.__class__.__name__,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", choices=("json", "telegram"), default="telegram")
    args = parser.parse_args(argv)
    result = asyncio.run(read_codex_limits())
    if args.mode == "json":
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(format_telegram_summary(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
