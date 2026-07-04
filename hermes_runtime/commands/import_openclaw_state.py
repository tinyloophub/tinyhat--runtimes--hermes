"""Import OpenClaw user state through Hermes' public migration CLI."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from hermes_runtime.hermes_cli import find_hermes_binary, run_process


SCHEMA = "tinyhat_hermes_import_openclaw_state_v1"
DEFAULT_TIMEOUT_SECONDS = 10 * 60


def _candidate_sources(spec: dict[str, Any]) -> list[Path]:
    raw_candidates = [
        spec.get("source"),
        os.getenv("TINYHAT_HERMES_OPENCLAW_MIGRATION_SOURCE"),
        os.getenv("TINYHAT_OPENCLAW_STATE_DIR"),
        os.getenv("OPENCLAW_STATE_DIR"),
        os.getenv("TINYHAT_RUNTIME_HOME"),
        "/var/lib/tinyhat-openclaw",
        str(Path.home() / ".openclaw"),
        "/root/.openclaw",
    ]
    sources: list[Path] = []
    seen: set[str] = set()
    for raw in raw_candidates:
        path_text = str(raw or "").strip()
        if not path_text:
            continue
        path = Path(path_text).expanduser()
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        sources.append(path)
    return sources


def _select_source(spec: dict[str, Any]) -> tuple[Path | None, list[dict[str, Any]]]:
    checked: list[dict[str, Any]] = []
    for source in _candidate_sources(spec):
        exists = source.exists()
        checked.append({"path": str(source), "exists": exists})
        if exists and source.is_dir():
            return source, checked
    return None, checked


def _bool_spec(spec: dict[str, Any], key: str, default: bool = False) -> bool:
    value = spec.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _timeout(spec: dict[str, Any]) -> int:
    try:
        return max(60, int(spec.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS


def _hermes_home_candidates() -> list[Path]:
    raw_candidates = [
        os.getenv("HERMES_HOME"),
        str(Path.home() / ".hermes"),
    ]
    candidates: list[Path] = []
    seen: set[str] = set()
    for raw in raw_candidates:
        path_text = str(raw or "").strip()
        if not path_text:
            continue
        path = Path(path_text).expanduser()
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(path)
    return candidates


def _latest_execute_report(source: Path, started_at: float) -> dict[str, Any] | None:
    latest: tuple[float, dict[str, Any]] | None = None
    source_key = str(source.resolve())
    for home in _hermes_home_candidates():
        reports_dir = home / "migration" / "openclaw"
        if not reports_dir.is_dir():
            continue
        for report_path in reports_dir.glob("*/report.json"):
            try:
                stat = report_path.stat()
            except OSError:
                continue
            if stat.st_mtime < started_at - 2:
                continue
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if report.get("mode") != "execute":
                continue
            if str(report.get("source_root") or "") != source_key:
                continue
            summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
            try:
                error_count = int(summary.get("error") or 0)
            except (TypeError, ValueError):
                error_count = 1
            if error_count > 0:
                continue
            if latest is None or stat.st_mtime > latest[0]:
                latest = (stat.st_mtime, report)
    if latest is None:
        return None
    return latest[1]


def _looks_like_preview_only(stdout: str, *, source: Path, started_at: float) -> bool:
    """Return true when Hermes printed only its non-mutating preview report.

    ``hermes claw migrate`` always prints a preview before applying changes.
    A real apply then prints a Migration Results section and writes a structured
    execute-mode ``report.json``. Tinyhat must reject true preview-only exits
    without false-failing successful applies that happen to include the preview.
    """

    normalized = " ".join(stdout.split()).lower()
    if "migration results" in normalized:
        return False
    if _latest_execute_report(source=source, started_at=started_at) is not None:
        return False
    return (
        "dry run results" in normalized
        or "no files were modified. this is a preview" in normalized
    )


async def run(ctx: Any, command: dict[str, Any]) -> dict[str, Any]:
    del ctx
    spec = command.get("spec") if isinstance(command.get("spec"), dict) else {}
    hermes_bin = find_hermes_binary()
    if hermes_bin is None:
        raise RuntimeError("Hermes CLI was not found; cannot import OpenClaw state.")

    source, checked_sources = _select_source(spec)
    if source is None:
        return {
            "schema": SCHEMA,
            "imported": False,
            "status": "skipped",
            "failure_code": "openclaw_source_not_found",
            "source": None,
            "checked_sources": checked_sources,
            "diagnostic": "No OpenClaw source directory was found.",
        }

    dry_run = _bool_spec(spec, "dry_run", False)
    overwrite = _bool_spec(spec, "overwrite", True)
    preset = str(spec.get("preset") or "full").strip() or "full"
    if preset not in {"full", "user-data"}:
        preset = "full"

    args = [
        str(hermes_bin),
        "claw",
        "migrate",
        "--source",
        str(source),
        "--preset",
        preset,
    ]
    if overwrite:
        args.append("--overwrite")
    if dry_run:
        args.append("--dry-run")
    else:
        args.append("--yes")
    migrate_secrets = _bool_spec(spec, "migrate_secrets", False) or _bool_spec(
        spec,
        "include_private_values",
        False,
    )
    if migrate_secrets:
        args.append("--migrate-secrets")

    started_at = time.time()
    process = await run_process(args, timeout_seconds=_timeout(spec))
    ok = bool(process.get("ok"))
    if not ok:
        raise RuntimeError(
            "hermes claw migrate failed: "
            + str(process.get("stderr") or process.get("stdout") or "unknown error")[:1000]
        )
    stdout = str(process.get("stdout") or "")
    preview_only = _looks_like_preview_only(stdout, source=source, started_at=started_at)
    if preview_only and not dry_run:
        raise RuntimeError(
            "hermes claw migrate exited successfully but did not apply changes. "
            "Hermes reported a dry-run preview only; stop OpenClaw and retry."
        )
    return {
        "schema": SCHEMA,
        "imported": ok and not dry_run and not preview_only,
        "dry_run": dry_run,
        "preview_only": preview_only,
        "source": str(source),
        "checked_sources": checked_sources,
        "preset": preset,
        "overwrite": overwrite,
        "migrate_secrets": migrate_secrets,
        "include_private_values": migrate_secrets,
        "hermes": {
            "command": "hermes claw migrate",
            "returncode": process.get("returncode"),
            "ok": ok,
            "timed_out": process.get("timed_out"),
            "duration_ms": process.get("duration_ms"),
            "stdout": process.get("stdout"),
            "stderr": process.get("stderr"),
            "stdout_truncated": process.get("stdout_truncated"),
            "stderr_truncated": process.get("stderr_truncated"),
        },
        "diagnostic": (
            "OpenClaw state import completed"
            if ok and not dry_run
            else "OpenClaw state import dry run completed"
            if ok
            else "OpenClaw state import failed"
        ),
    }
