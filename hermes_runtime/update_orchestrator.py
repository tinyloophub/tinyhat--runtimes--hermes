"""Check and prepare Tinyhat runtime and plugin updates as one operation."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib import parse

from hermes_runtime import __version__
from hermes_runtime.commands import stage_update
from hermes_runtime.plugin_manager import (
    plugin_name,
    plugin_snapshot,
    update_tinyhat_plugin,
)
from hermes_runtime.telegram_codex_auth import _telegram_send
from hermes_runtime.update_check import run_update_check


SCHEMA = "tinyhat_hermes_check_and_stage_updates_v1"
FULL_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
ALLOWED_CHANNELS = {"lts", "latest", "custom"}
MAX_REASON_LENGTH = 64
MAX_VERSION_LENGTH = 128
PLUGIN_REPAIR_SCHEMA = "tinyhat_hermes_plugin_repair_v1"
PLUGIN_REPAIR_FILE = "pending_plugin_repair.json"
PLUGIN_REPAIR_PENDING = "repair_pending"
PLUGIN_INSTALLED_UNACKNOWLEDGED = "installed_unacknowledged"


def _clean_text(value: Any, *, max_length: int) -> str | None:
    if not isinstance(value, str):
        return None
    clean = "".join(char for char in value.strip() if char not in {"\x00", "\r", "\n"})
    return clean[:max_length] or None


def _required_text(spec: dict[str, Any], key: str, *, max_length: int = 512) -> str:
    raw_value = spec.get(key)
    if not isinstance(raw_value, str):
        raise ValueError(f"check_and_stage_updates requires {key}")
    value = raw_value.strip()
    if (
        not value
        or len(value) > max_length
        or any(char in value for char in ("\x00", "\r", "\n"))
    ):
        raise ValueError(f"check_and_stage_updates requires a valid {key}")
    return value


def _required_sha(spec: dict[str, Any], key: str) -> str:
    value = _required_text(spec, key, max_length=40)
    if FULL_GIT_SHA_RE.fullmatch(value) is None:
        raise ValueError(f"{key} must be a full git commit sha")
    return value.lower()


def _required_plugin_repo_url(spec: dict[str, Any]) -> str:
    value = _required_text(spec, "plugin_repo_url", max_length=2_048)
    try:
        parsed = parse.urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("plugin_repo_url must be a public HTTPS URL") from exc
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("plugin_repo_url must be a public HTTPS URL")
    clean_host = hostname.lower()
    if ":" in clean_host:
        clean_host = f"[{clean_host}]"
    netloc = clean_host if port is None else f"{clean_host}:{port}"
    return parse.urlunsplit(("https", netloc, parsed.path, "", ""))


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _plugin_repair_path(ctx: Any) -> Path:
    return ctx.state_dir / "updates" / PLUGIN_REPAIR_FILE


def _plugin_target_matches(
    proof: dict[str, Any],
    *,
    repo_url: str,
    ref: str,
    commit: str,
) -> bool:
    return (
        proof.get("installed") is True
        and proof.get("repo_url") == repo_url
        and proof.get("ref") == ref
        and proof.get("commit") == commit
    )


def _plugin_repair_pending(
    ctx: Any,
    *,
    repo_url: str,
    ref: str,
    commit: str,
) -> bool:
    payload = _plugin_marker(
        ctx,
        repo_url=repo_url,
        ref=ref,
        commit=commit,
    )
    return bool(
        payload and payload.get("state", PLUGIN_REPAIR_PENDING) == PLUGIN_REPAIR_PENDING
    )


def _plugin_marker(
    ctx: Any,
    *,
    repo_url: str,
    ref: str,
    commit: str,
) -> dict[str, Any] | None:
    payload = _read_json(_plugin_repair_path(ctx))
    if not (
        payload
        and payload.get("schema") == PLUGIN_REPAIR_SCHEMA
        and payload.get("plugin_repo_url") == repo_url
        and payload.get("plugin_ref") == ref
        and payload.get("target_commit") == commit
    ):
        return None
    return payload


def _plugin_installed_unacknowledged(
    ctx: Any,
    *,
    repo_url: str,
    ref: str,
    commit: str,
) -> dict[str, Any] | None:
    payload = _plugin_marker(
        ctx,
        repo_url=repo_url,
        ref=ref,
        commit=commit,
    )
    if payload is None or payload.get("state") != PLUGIN_INSTALLED_UNACKNOWLEDGED:
        return None
    return payload


def _record_plugin_repair(
    ctx: Any,
    *,
    repo_url: str,
    ref: str,
    commit: str,
) -> None:
    _write_json_atomic(
        _plugin_repair_path(ctx),
        {
            "schema": PLUGIN_REPAIR_SCHEMA,
            "state": PLUGIN_REPAIR_PENDING,
            "plugin_repo_url": repo_url,
            "plugin_ref": ref,
            "target_commit": commit,
        },
    )


def _record_plugin_installed_unacknowledged(
    ctx: Any,
    *,
    repo_url: str,
    ref: str,
    commit: str,
    installed: dict[str, Any],
) -> None:
    _write_json_atomic(
        _plugin_repair_path(ctx),
        {
            "schema": PLUGIN_REPAIR_SCHEMA,
            "state": PLUGIN_INSTALLED_UNACKNOWLEDGED,
            "plugin_repo_url": repo_url,
            "plugin_ref": ref,
            "target_commit": commit,
            "installed_version": _clean_text(
                installed.get("version"),
                max_length=MAX_VERSION_LENGTH,
            ),
        },
    )


def _clear_plugin_marker(
    ctx: Any,
    *,
    repo_url: str,
    ref: str,
    commit: str,
) -> None:
    if (
        _plugin_marker(
            ctx,
            repo_url=repo_url,
            ref=ref,
            commit=commit,
        )
        is None
    ):
        return
    _plugin_repair_path(ctx).unlink(missing_ok=True)


def _staged_runtime_matches(ctx: Any, *, target_ref: str, target_sha: str) -> bool:
    if ctx.staged_version() != target_ref:
        return False
    metadata = _read_json(ctx.staged_metadata_file)
    if metadata is None:
        return False
    return (
        _clean_text(metadata.get("target_ref"), max_length=512) == target_ref
        and (_clean_text(metadata.get("target_sha"), max_length=40) or "").lower()
        == target_sha
    )


def _activation_matches(ctx: Any, *, target_ref: str) -> bool:
    try:
        value = ctx.activation_marker.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return False
    return value == target_ref


def _plugin_proof(value: Any) -> dict[str, Any]:
    source = value.get("source") if isinstance(value, dict) else None
    source = source if isinstance(source, dict) else {}
    return {
        "installed": bool(isinstance(value, dict) and value.get("installed") is True),
        "version": _clean_text(
            value.get("version") if isinstance(value, dict) else None,
            max_length=MAX_VERSION_LENGTH,
        ),
        "repo_url": _clean_text(source.get("repo_url"), max_length=2_048),
        "ref": _clean_text(source.get("ref"), max_length=512),
        "commit": (_clean_text(source.get("commit"), max_length=40) or "").lower()
        or None,
    }


def _generic_error(exc: Exception, *, component: str) -> dict[str, str]:
    return {
        "code": exc.__class__.__name__[:128],
        "message": f"{component} update failed",
    }


def _capabilities_message(version: str | None) -> str:
    clean_version = _clean_text(version, max_length=MAX_VERSION_LENGTH) or "unknown"
    return (
        f"Tinyhat capabilities updated to version {clean_version}.\n\n"
        "The new capabilities will be picked up after the next Hermes /restart.\n\n"
        "To use them now, run /restart."
    )


def _runtime_update_message(version: str) -> str:
    return (
        f"Tinyhat runtime update staged to version {version}.\n\n"
        "It will be activated automatically."
    )


async def _send_notice(text: str) -> dict[str, Any]:
    try:
        result = await asyncio.to_thread(_telegram_send, text)
    except Exception as exc:  # noqa: BLE001 - updates must not depend on Telegram.
        return {
            "attempted": True,
            "sent": False,
            "error": _generic_error(exc, component="Telegram notification"),
        }
    return {
        "attempted": True,
        "sent": bool(result.get("ok")),
        "http_status": (
            result.get("http_status")
            if isinstance(result.get("http_status"), int)
            else None
        ),
        "error": (
            None
            if result.get("ok")
            else {
                "code": "telegram_delivery_failed",
                "message": "Telegram notification failed",
            }
        ),
    }


async def _send_update_notice(version: str | None) -> dict[str, Any]:
    return await _send_notice(_capabilities_message(version))


async def _send_runtime_update_notice(version: str) -> dict[str, Any]:
    return await _send_notice(_runtime_update_message(version))


def acknowledge_check_and_stage_result(
    ctx: Any,
    command: dict[str, Any],
    result: dict[str, Any],
) -> bool:
    """Clear a delivered plugin marker after the platform accepted its result.

    The caller must invoke this only after the command-result POST succeeds.
    Keeping that ordering makes plugin installation, the Telegram restart notice,
    and platform settlement recoverable when the process crashes or result
    delivery fails.
    """

    if command.get("kind") != "check_and_stage_updates":
        return False
    if result.get("schema") != SCHEMA:
        return False
    plugin = result.get("plugin")
    if not isinstance(plugin, dict):
        return False
    if (
        plugin.get("status") != "updated"
        or plugin.get("changed") is not True
        or plugin.get("error") is not None
    ):
        return False

    spec = command.get("spec")
    if not isinstance(spec, dict):
        return False
    try:
        repo_url = _required_plugin_repo_url(spec)
        ref = _required_text(spec, "plugin_ref")
        commit = _required_sha(spec, "target_commit")
    except ValueError:
        return False
    installed = _plugin_proof(plugin.get("installed"))
    if not _plugin_target_matches(
        installed,
        repo_url=repo_url,
        ref=ref,
        commit=commit,
    ):
        return False
    if (
        _plugin_installed_unacknowledged(
            ctx,
            repo_url=repo_url,
            ref=ref,
            commit=commit,
        )
        is None
    ):
        return False
    _clear_plugin_marker(
        ctx,
        repo_url=repo_url,
        ref=ref,
        commit=commit,
    )
    return True


async def check_and_stage_updates(ctx: Any, spec: dict[str, Any]) -> dict[str, Any]:
    """Check exact targets, prepare changed components, and request reload."""

    reason = _required_text(spec, "reason", max_length=MAX_REASON_LENGTH)
    auto_update_run_id = _clean_text(
        spec.get("auto_update_run_id"),
        max_length=64,
    )
    channel = _required_text(spec, "channel", max_length=32)
    if channel not in ALLOWED_CHANNELS:
        raise ValueError("channel must be lts, latest, or custom")
    target_ref = _required_text(spec, "target_ref")
    target_sha = _required_sha(spec, "target_sha")
    plugin_values = (
        _clean_text(spec.get("plugin_repo_url"), max_length=2048),
        _clean_text(spec.get("plugin_ref"), max_length=512),
        _clean_text(spec.get("target_commit"), max_length=40),
    )
    plugin_requested = all(plugin_values)
    if any(plugin_values) and not plugin_requested:
        raise ValueError(
            "plugin_repo_url, plugin_ref, and target_commit must be provided together"
        )
    plugin_repo_url: str | None = None
    plugin_ref: str | None = None
    target_commit: str | None = None
    if plugin_requested:
        plugin_repo_url = _required_plugin_repo_url(spec)
        plugin_ref = _required_text(spec, "plugin_ref")
        target_commit = _required_sha(spec, "target_commit")

    exact_spec = {
        "channel": channel,
        "target_ref": target_ref,
        "target_sha": target_sha,
    }
    if plugin_requested:
        exact_spec.update(
            {
                "plugin_repo_url": plugin_repo_url,
                "plugin_ref": plugin_ref,
                "target_commit": target_commit,
            }
        )
    discovery = await run_update_check(
        state_dir=ctx.state_dir,
        current_version=ctx.current_version(),
        current_code_version=__version__,
        current_sha=ctx.current_commit_sha(),
        spec=exact_spec,
        reason="check_and_stage_updates",
        include_plugin_check=plugin_requested,
    )

    plugin_discovery = discovery.get("plugin_update_check")
    plugin_discovery = plugin_discovery if isinstance(plugin_discovery, dict) else {}
    runtime_update_available = discovery.get("update_available") is True
    plugin_update_value = plugin_discovery.get("update_available")
    plugin_update_available = plugin_requested and plugin_update_value is True
    plugin_discovery_failed = bool(
        plugin_requested
        and (
            not isinstance(plugin_update_value, bool)
            or plugin_discovery.get("target_error")
            or plugin_discovery.get("error")
            or plugin_discovery.get("decision") == "target_unavailable"
        )
    )

    runtime_error: dict[str, str] | None = None
    runtime_staged_now = False
    runtime_already_staged = False
    runtime_activation_requested = False
    runtime_stage_result: dict[str, Any] | None = None
    runtime_activation_pending = _activation_matches(ctx, target_ref=target_ref)
    if runtime_update_available:
        try:
            runtime_already_staged = _staged_runtime_matches(
                ctx,
                target_ref=target_ref,
                target_sha=target_sha,
            )
            if not runtime_already_staged:
                runtime_stage_result = await stage_update.run(
                    ctx,
                    {
                        "kind": "stage_update",
                        "spec": {
                            "channel": channel,
                            "target_ref": target_ref,
                            "target_version": target_ref,
                            "target_sha": target_sha,
                        },
                    },
                )
                runtime_staged_now = bool(runtime_stage_result.get("code_staged"))
        except Exception as exc:  # noqa: BLE001 - plugin update remains independent.
            runtime_error = _generic_error(exc, component="Runtime")

    plugin_changed = False
    plugin_result: dict[str, Any] | None = None
    plugin_before = _plugin_proof(plugin_discovery.get("installed"))
    plugin_installed_unacknowledged: dict[str, Any] | None = None
    plugin_repair_pending = False
    if plugin_requested:
        assert plugin_repo_url is not None
        assert plugin_ref is not None
        assert target_commit is not None
        plugin_installed_unacknowledged = _plugin_installed_unacknowledged(
            ctx,
            repo_url=plugin_repo_url,
            ref=plugin_ref,
            commit=target_commit,
        )
        plugin_repair_pending = _plugin_repair_pending(
            ctx,
            repo_url=plugin_repo_url,
            ref=plugin_ref,
            commit=target_commit,
        )
    plugin_error: dict[str, str] | None = (
        {
            "code": "plugin_target_unavailable",
            "message": "Plugin update check failed",
        }
        if plugin_discovery_failed and plugin_installed_unacknowledged is None
        else None
    )
    plugin_repair_performed = False
    if plugin_installed_unacknowledged is not None:
        assert plugin_repo_url is not None
        assert plugin_ref is not None
        assert target_commit is not None
        plugin_result = {
            "changed": False,
            "after": {
                "installed": True,
                "version": _clean_text(
                    plugin_installed_unacknowledged.get("installed_version"),
                    max_length=MAX_VERSION_LENGTH,
                ),
                "source": {
                    "repo_url": plugin_repo_url,
                    "ref": plugin_ref,
                    "commit": target_commit,
                },
            },
        }
        plugin_changed = True
    elif (
        plugin_requested
        and plugin_error is None
        and (plugin_update_available or plugin_repair_pending)
    ):
        assert plugin_repo_url is not None
        assert plugin_ref is not None
        assert target_commit is not None
        try:
            plugin_repair_performed = plugin_repair_pending
            if not plugin_repair_pending:
                _record_plugin_repair(
                    ctx,
                    repo_url=plugin_repo_url,
                    ref=plugin_ref,
                    commit=target_commit,
                )
                plugin_repair_pending = True
            plugin_result = await update_tinyhat_plugin(
                {
                    "kind": "update_tinyhat_plugin",
                    "spec": {
                        "reason": reason,
                        "auto_update_run_id": auto_update_run_id,
                        "plugin_repo_url": plugin_repo_url,
                        "plugin_ref": plugin_ref,
                        "target_commit": target_commit,
                    },
                }
            )
            plugin_changed = bool(
                plugin_result.get("changed")
                or plugin_repair_performed
                or plugin_update_available
            )
            installed_after = _plugin_proof(plugin_result.get("after"))
            if not _plugin_target_matches(
                installed_after,
                repo_url=plugin_repo_url,
                ref=plugin_ref,
                commit=target_commit,
            ):
                raise RuntimeError("Plugin update did not prove the exact target")
            _record_plugin_installed_unacknowledged(
                ctx,
                repo_url=plugin_repo_url,
                ref=plugin_ref,
                commit=target_commit,
                installed=installed_after,
            )
        except Exception as exc:  # noqa: BLE001 - runtime staging remains independent.
            plugin_error = _generic_error(exc, component="Plugin")
            plugin_changed = False
            plugin_result = None
            try:
                failed_snapshot = plugin_snapshot(
                    plugin_name(
                        {
                            "spec": {
                                "plugin_repo_url": plugin_repo_url,
                                "plugin_ref": plugin_ref,
                                "target_commit": target_commit,
                            }
                        }
                    )
                )
                failed_after = _plugin_proof(failed_snapshot)
            except Exception:  # noqa: BLE001 - retain repair marker and fail closed.
                failed_snapshot = None
                failed_after = {}
            if _plugin_target_matches(
                failed_after,
                repo_url=plugin_repo_url,
                ref=plugin_ref,
                commit=target_commit,
            ):
                plugin_result = {"after": failed_snapshot}
                plugin_changed = not _plugin_target_matches(
                    plugin_before,
                    repo_url=plugin_repo_url,
                    ref=plugin_ref,
                    commit=target_commit,
                )

    runtime_ready_to_activate = bool(
        runtime_error is None
        and (
            runtime_activation_pending
            or (
                runtime_update_available
                and (runtime_staged_now or runtime_already_staged)
            )
        )
    )
    if runtime_ready_to_activate:
        try:
            if not runtime_activation_pending:
                ctx.activation_marker.parent.mkdir(parents=True, exist_ok=True)
                ctx.activation_marker.write_text(target_ref + "\n", encoding="utf-8")
            # The staged target is not the running target yet. Keep requesting
            # the small runtime restart even when a previous activation marker
            # survived a failed startup activation attempt.
            runtime_activation_requested = True
        except Exception as exc:  # noqa: BLE001 - report activation failure safely.
            runtime_error = _generic_error(exc, component="Runtime activation")

    runtime_changed = bool(
        runtime_error is None and (runtime_staged_now or runtime_activation_requested)
    )
    changed = runtime_changed or plugin_changed

    plugin_after = _plugin_proof(
        plugin_result.get("after") if isinstance(plugin_result, dict) else None
    )
    if plugin_result is None:
        plugin_after = dict(plugin_before)
    plugin_version = plugin_after.get("version") or _clean_text(
        plugin_discovery.get("target_version"),
        max_length=MAX_VERSION_LENGTH,
    )

    notification: dict[str, Any] = {
        "attempted": False,
        "sent": None,
        "http_status": None,
        "error": None,
    }
    if plugin_changed:
        notification = await _send_update_notice(
            plugin_version if isinstance(plugin_version, str) else None
        )
    elif runtime_changed:
        notification = await _send_runtime_update_notice(target_ref)
    if changed:
        ctx.restart_requested = True

    errors = [error for error in (runtime_error, plugin_error) if error is not None]
    if errors and changed:
        status = "partial"
    elif errors:
        status = "failed"
    elif changed:
        status = "updated"
    else:
        status = "current"

    return {
        "schema": SCHEMA,
        "reason": reason,
        "auto_update_run_id": auto_update_run_id,
        "status": status,
        "changed": changed,
        "runtime": {
            "status": (
                "failed"
                if runtime_error
                else "staged"
                if runtime_changed
                else "already_staged"
                if runtime_already_staged
                else "current"
            ),
            "current_version": _clean_text(
                discovery.get("current_code_version")
                or discovery.get("current_version"),
                max_length=MAX_VERSION_LENGTH,
            ),
            "current_sha": (
                _clean_text(discovery.get("current_sha"), max_length=40) or ""
            ).lower()
            or None,
            "target_ref": target_ref,
            "target_sha": target_sha,
            "update_available": runtime_update_available,
            "changed": runtime_changed,
            "staged_now": runtime_staged_now,
            "activation_requested": runtime_activation_requested,
            "error": runtime_error,
        },
        "plugin": {
            "status": (
                "not_requested"
                if not plugin_requested
                else "failed"
                if plugin_error
                else "updated"
                if plugin_changed
                else "current"
            ),
            "repo_url": plugin_repo_url,
            "ref": plugin_ref,
            "current_version": plugin_before.get("version"),
            "current_commit": plugin_before.get("commit"),
            "target_version": _clean_text(
                plugin_discovery.get("target_version"),
                max_length=MAX_VERSION_LENGTH,
            ),
            "target_commit": target_commit,
            "installed_version": plugin_after.get("version"),
            "installed_commit": plugin_after.get("commit"),
            "installed": {
                "installed": bool(plugin_after.get("installed") is True),
                "version": plugin_after.get("version"),
                "source": {
                    "repo_url": plugin_after.get("repo_url") or plugin_repo_url,
                    "ref": plugin_after.get("ref") or plugin_ref,
                    "commit": plugin_after.get("commit"),
                },
            },
            "update_available": plugin_update_available,
            "changed": plugin_changed,
            "repair_performed": plugin_repair_performed and plugin_error is None,
            "error": plugin_error,
        },
        "runtime_restart_requested": changed,
        "hermes_restart_required": plugin_changed,
        "notification": notification,
    }
