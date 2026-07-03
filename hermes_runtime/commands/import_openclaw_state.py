"""Import OpenClaw user state through Hermes' public migration CLI."""

from __future__ import annotations

import os
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

    process = await run_process(args, timeout_seconds=_timeout(spec))
    ok = bool(process.get("ok"))
    if not ok:
        raise RuntimeError(
            "hermes claw migrate failed: "
            + str(process.get("stderr") or process.get("stdout") or "unknown error")[:1000]
        )
    return {
        "schema": SCHEMA,
        "imported": ok and not dry_run,
        "dry_run": dry_run,
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
