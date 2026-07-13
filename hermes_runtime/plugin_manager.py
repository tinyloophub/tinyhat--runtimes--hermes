"""Helpers for managing the Tinyhat Hermes plugin through public CLI commands."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_runtime.hermes_cli import find_hermes_binary, run_process
from hermes_runtime.runtime_env import hermes_home

DEFAULT_TINYHAT_PLUGIN_REPO_URL = "https://github.com/tinyhat-ai/tinyhat.git"
DEFAULT_TINYHAT_PLUGIN_REF = "channels/lts"
DEFAULT_TINYHAT_PLUGIN_NAME = "tinyhat"
PLUGIN_COMMAND_TIMEOUT_SECONDS = 300
PLUGIN_SOURCE_METADATA = ".tinyhat-plugin-source.json"
MAX_PLUGIN_REPO_URL_LENGTH = 2_048
MAX_PLUGIN_REF_LENGTH = 512
EXACT_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40}$")


@dataclass(frozen=True)
class PluginTargetSelection:
    """One immutable repo/ref decision for a complete plugin operation."""

    source: str
    repo_url: str
    ref: str

    def as_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "repo_url": self.repo_url,
            "ref": self.ref,
        }


def _command_spec(command: dict[str, Any]) -> dict[str, Any]:
    spec = command.get("spec")
    return spec if isinstance(spec, dict) else {}


def plugin_name(command: dict[str, Any]) -> str:
    spec = _command_spec(command)
    raw = (
        spec.get("plugin_name")
        or os.getenv("TINYHAT_PLUGIN_NAME")
        or DEFAULT_TINYHAT_PLUGIN_NAME
    )
    return str(raw).strip() or DEFAULT_TINYHAT_PLUGIN_NAME


def _bounded_target_value(
    value: Any,
    *,
    field: str,
    max_length: int,
    strict: bool,
    reject_leading_hyphen: bool = False,
) -> str | None:
    if not isinstance(value, str):
        if strict and value is not None:
            raise ValueError(f"{field} must be a string")
        return None
    clean = value.strip()
    if not clean:
        if strict:
            raise ValueError(f"{field} must not be empty")
        return None
    if (
        len(clean) > max_length
        or any(char in clean for char in ("\x00", "\r", "\n"))
        or (reject_leading_hyphen and clean.startswith("-"))
    ):
        if strict:
            raise ValueError(f"{field} is malformed or too long")
        return None
    return clean


def _environment_target_value(
    value: str | None,
    *,
    field: str,
    max_length: int,
    reject_leading_hyphen: bool = False,
) -> str | None:
    if value is None or not value.strip():
        return None
    return _bounded_target_value(
        value,
        field=field,
        max_length=max_length,
        strict=True,
        reject_leading_hyphen=reject_leading_hyphen,
    )


def _spec_target_value(
    spec: dict[str, Any],
    keys: tuple[str, ...],
    *,
    field: str,
    max_length: int,
    reject_leading_hyphen: bool = False,
) -> str | None:
    values = {
        value
        for key in keys
        if key in spec and spec.get(key) not in (None, "")
        if (
            value := _bounded_target_value(
                spec.get(key),
                field=field,
                max_length=max_length,
                strict=True,
                reject_leading_hyphen=reject_leading_hyphen,
            )
        )
    }
    if len(values) > 1:
        raise ValueError(f"conflicting {field} values")
    return next(iter(values)) if values else None


def _select_plugin_target(
    command: dict[str, Any],
    *,
    installed: dict[str, Any] | None = None,
) -> PluginTargetSelection:
    """Select one logical plugin target without crossing source levels.

    A partial explicit target may inherit its counterpart from environment
    configuration, preserving the existing per-field command contract. It is
    never completed from installed metadata, which could belong to an unrelated
    custom source.
    """

    spec = _command_spec(command)
    explicit_repo = _spec_target_value(
        spec,
        ("plugin_repo_url", "repo_url"),
        field="plugin repo URL",
        max_length=MAX_PLUGIN_REPO_URL_LENGTH,
    )
    explicit_ref = _spec_target_value(
        spec,
        ("plugin_ref", "ref"),
        field="plugin ref",
        max_length=MAX_PLUGIN_REF_LENGTH,
        reject_leading_hyphen=True,
    )
    environment_repo = _environment_target_value(
        os.getenv("TINYHAT_PLUGIN_REPO_URL")
        if explicit_repo is None
        else None,
        field="TINYHAT_PLUGIN_REPO_URL",
        max_length=MAX_PLUGIN_REPO_URL_LENGTH,
    )
    environment_ref = _environment_target_value(
        os.getenv("TINYHAT_PLUGIN_REF") if explicit_ref is None else None,
        field="TINYHAT_PLUGIN_REF",
        max_length=MAX_PLUGIN_REF_LENGTH,
        reject_leading_hyphen=True,
    )
    if explicit_repo or explicit_ref:
        return PluginTargetSelection(
            source="spec",
            repo_url=(
                explicit_repo
                or environment_repo
                or DEFAULT_TINYHAT_PLUGIN_REPO_URL
            ),
            ref=explicit_ref or environment_ref or DEFAULT_TINYHAT_PLUGIN_REF,
        )

    if environment_repo or environment_ref:
        return PluginTargetSelection(
            source="environment",
            repo_url=environment_repo or DEFAULT_TINYHAT_PLUGIN_REPO_URL,
            ref=environment_ref or DEFAULT_TINYHAT_PLUGIN_REF,
        )

    installed_source_value = (
        installed.get("source")
        if isinstance(installed, dict)
        else _read_source_metadata(plugin_name(command))
    )
    installed_source = (
        installed_source_value if isinstance(installed_source_value, dict) else {}
    )
    installed_repo = _bounded_target_value(
        installed_source.get("repo_url"),
        field="installed plugin repo URL",
        max_length=MAX_PLUGIN_REPO_URL_LENGTH,
        strict=False,
    )
    installed_ref = _bounded_target_value(
        installed_source.get("ref"),
        field="installed plugin ref",
        max_length=MAX_PLUGIN_REF_LENGTH,
        strict=False,
        reject_leading_hyphen=True,
    )
    if installed_repo and installed_ref:
        return PluginTargetSelection(
            source="installed_metadata",
            repo_url=installed_repo,
            ref=installed_ref,
        )

    return PluginTargetSelection(
        source="default",
        repo_url=DEFAULT_TINYHAT_PLUGIN_REPO_URL,
        ref=DEFAULT_TINYHAT_PLUGIN_REF,
    )


def plugin_target_selection(command: dict[str, Any]) -> dict[str, str]:
    """Return the selected target as the command/report-compatible mapping."""

    return _select_plugin_target(command).as_dict()


def plugin_target_commit(command: dict[str, Any]) -> str | None:
    spec = _command_spec(command)
    requested = []
    for key in ("target_commit", "target_sha"):
        if key not in spec or spec.get(key) in (None, ""):
            continue
        raw_value = spec.get(key)
        if not isinstance(raw_value, str):
            raise ValueError(f"{key} must be a full 40-character Git commit SHA")
        value = raw_value.strip().lower()
        if EXACT_COMMIT_RE.fullmatch(value) is None:
            raise ValueError(f"{key} must be a full 40-character Git commit SHA")
        requested.append(value)
    if len(set(requested)) > 1:
        raise ValueError("target_commit and target_sha must identify the same commit")
    return requested[0] if requested else None


def plugin_dir(name: str) -> Path:
    return hermes_home() / "plugins" / name


def _source_metadata_path(name: str) -> Path:
    return plugin_dir(name) / PLUGIN_SOURCE_METADATA


def _read_manifest_field(path: Path, field: str) -> str | None:
    if not path.is_file():
        return None
    pattern = re.compile(rf"^\s*{re.escape(field)}\s*:\s*['\"]?([^'\"\n#]+)")
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = pattern.match(line)
        if match:
            return match.group(1).strip()
    return None


def _read_source_metadata(name: str) -> dict[str, Any] | None:
    path = _source_metadata_path(name)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_source_metadata(name: str, metadata: dict[str, Any]) -> None:
    path = _source_metadata_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def plugin_snapshot(name: str) -> dict[str, Any]:
    root = plugin_dir(name)
    # Hermes file plugins live under ~/.hermes/plugins/<name>. The CLI does not
    # expose a stable JSON "show plugin" command yet, so this public plugin
    # manifest is the version source after Hermes CLI install/list succeeds.
    manifest = root / "plugin.yaml"
    source = _read_source_metadata(name)
    return {
        "name": name,
        "installed": manifest.is_file(),
        "plugin_dir": str(root),
        "manifest": str(manifest),
        "manifest_name": _read_manifest_field(manifest, "name"),
        "version": _read_manifest_field(manifest, "version"),
        "source": source,
    }


def _source_commit(snapshot: dict[str, Any]) -> str | None:
    source = snapshot.get("source")
    if not isinstance(source, dict):
        return None
    commit = _bounded_target_value(
        source.get("commit"),
        field="plugin commit",
        max_length=40,
        strict=False,
    )
    commit = str(commit or "").lower()
    return commit or None


def public_plugin_target_error(exc: Exception, *, repo_url: str) -> str:
    message = str(exc)
    if repo_url:
        message = message.replace(repo_url, "<plugin-repo>")
    for private_root in (tempfile.gettempdir(), str(Path.home())):
        if private_root:
            message = message.replace(private_root, "<local-path>")
    return message[:500]


def _target_matches_installed(
    snapshot: dict[str, Any],
    *,
    repo_url: str,
    ref: str,
    target_commit: str | None,
) -> bool:
    if not snapshot.get("installed"):
        return False
    if not _source_matches(snapshot, repo_url=repo_url, ref=ref):
        return False
    installed_commit = _source_commit(snapshot)
    return bool(
        target_commit and installed_commit and installed_commit == target_commit
    )


async def plugin_target_snapshot(
    command: dict[str, Any],
    *,
    selection: PluginTargetSelection | None = None,
    requested_commit: str | None = None,
) -> dict[str, Any]:
    selection = selection or _select_plugin_target(command)
    repo_url = selection.repo_url
    ref = selection.ref
    if requested_commit is None:
        requested_commit = plugin_target_commit(command)
    checkout_ref = requested_commit or ref
    checkout, commit, tmp = await _prepare_checkout(
        repo_url,
        checkout_ref,
        fallback_ref=(
            ref
            if requested_commit and requested_commit.lower() != ref.lower()
            else None
        ),
    )
    try:
        clean_commit = str(commit or "").strip().lower() or None
        if requested_commit and clean_commit != requested_commit:
            raise RuntimeError(
                "Tinyhat plugin checkout did not match the requested exact commit"
            )
        manifest = checkout / "plugin.yaml"
        return {
            "repo_url": repo_url,
            "ref": ref,
            "commit": clean_commit,
            "requested_commit": requested_commit,
            "version": _read_manifest_field(manifest, "version"),
            "manifest_name": _read_manifest_field(manifest, "name"),
        }
    finally:
        tmp.cleanup()


async def tinyhat_plugin_status(
    command: dict[str, Any],
    *,
    selection: PluginTargetSelection | None = None,
    installed: dict[str, Any] | None = None,
    requested_commit: str | None = None,
) -> dict[str, Any]:
    """Return installed and target Tinyhat plugin versions without changing it."""
    name = plugin_name(command)
    installed = installed if installed is not None else plugin_snapshot(name)
    selection = selection or _select_plugin_target(command, installed=installed)
    repo_url = selection.repo_url
    ref = selection.ref
    if requested_commit is None:
        requested_commit = plugin_target_commit(command)
    target: dict[str, Any] | None = None
    target_error: str | None = None
    try:
        target = await plugin_target_snapshot(
            command,
            selection=selection,
            requested_commit=requested_commit,
        )
    except Exception as exc:  # pragma: no cover - exercised through callers
        target_error = public_plugin_target_error(exc, repo_url=repo_url)
    target_commit = str((target or {}).get("commit") or "").strip() or None
    update_available = (
        None
        if target_error
        else not _target_matches_installed(
            installed,
            repo_url=repo_url,
            ref=ref,
            target_commit=target_commit,
        )
    )
    if update_available is True and not installed.get("installed"):
        decision = "plugin_missing"
    elif update_available is True:
        decision = "target_ref_changed"
    elif update_available is False:
        decision = "installed_matches_target"
    else:
        decision = "target_unavailable"
    return {
        "plugin_name": name,
        "plugin_repo_url": repo_url,
        "plugin_ref": ref,
        "target_selection": {
            "source": selection.source,
            "plugin_repo_url": repo_url,
            "plugin_ref": ref,
        },
        "installed": installed,
        "target": target,
        "installed_version": installed.get("version"),
        "installed_commit": _source_commit(installed),
        "target_version": (target or {}).get("version"),
        "target_commit": target_commit,
        "update_available": update_available,
        "decision": decision,
        "checked_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "target_error": target_error,
    }


def _compact_process(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "args": result.get("args"),
        "ok": bool(result.get("ok")),
        "returncode": result.get("returncode"),
        "duration_ms": result.get("duration_ms"),
        "stdout": result.get("stdout"),
        "stderr": result.get("stderr"),
        "stdout_truncated": bool(result.get("stdout_truncated")),
        "stderr_truncated": bool(result.get("stderr_truncated")),
    }


async def _plugins_list(hermes_bin: Path) -> dict[str, Any]:
    return await run_process(
        [str(hermes_bin), "plugins", "list"],
        timeout_seconds=60,
    )


async def _enable_plugin(hermes_bin: Path, name: str) -> dict[str, Any]:
    return await run_process(
        [str(hermes_bin), "plugins", "enable", name],
        timeout_seconds=60,
    )


async def _install_plugin(
    hermes_bin: Path,
    *,
    identifier: str,
    force: bool,
) -> dict[str, Any]:
    args = [str(hermes_bin), "plugins", "install", identifier, "--enable"]
    if force:
        args.append("--force")
    return await run_process(args, timeout_seconds=PLUGIN_COMMAND_TIMEOUT_SECONDS)


async def _git(args: list[str], *, timeout_seconds: int = 120) -> dict[str, Any]:
    return await run_process(["git", *args], timeout_seconds=timeout_seconds)


def _raise_if_failed(action: str, result: dict[str, Any]) -> None:
    if result.get("ok"):
        return
    raise RuntimeError(
        f"Tinyhat plugin {action} failed with returncode={result.get('returncode')}: "
        f"{str(result.get('stderr') or result.get('stdout') or '').strip()}"
    )


async def _prepare_checkout(
    repo_url: str,
    ref: str,
    *,
    fallback_ref: str | None = None,
) -> tuple[Path, str | None, tempfile.TemporaryDirectory[str]]:
    if ref.startswith("-") or (fallback_ref and fallback_ref.startswith("-")):
        raise ValueError("plugin ref must not begin with '-'")
    tmp = tempfile.TemporaryDirectory(prefix="tinyhat-plugin-")
    checkout = Path(tmp.name) / "tinyhat"
    try:
        exact_commit = ref.lower() if EXACT_COMMIT_RE.fullmatch(ref) else None
        if exact_commit:
            init = await _git(["init", str(checkout)], timeout_seconds=60)
            _raise_if_failed("initialize checkout", init)
            remote = await _git(
                ["-C", str(checkout), "remote", "add", "origin", repo_url],
                timeout_seconds=60,
            )
            _raise_if_failed("configure checkout remote", remote)
            fetch = await _git(
                [
                    "-C",
                    str(checkout),
                    "fetch",
                    "--depth",
                    "1",
                    "origin",
                    exact_commit,
                ],
                timeout_seconds=PLUGIN_COMMAND_TIMEOUT_SECONDS,
            )
            if not fetch.get("ok") and fallback_ref:
                fetch = await _git(
                    [
                        "-C",
                        str(checkout),
                        "fetch",
                        "--depth",
                        "1",
                        "origin",
                        fallback_ref,
                    ],
                    timeout_seconds=PLUGIN_COMMAND_TIMEOUT_SECONDS,
                )
            _raise_if_failed("fetch exact commit", fetch)
            checkout_result = await _git(
                ["-C", str(checkout), "checkout", "--detach", exact_commit],
                timeout_seconds=60,
            )
            _raise_if_failed("checkout exact commit", checkout_result)
        else:
            clone = await _git(
                [
                    "clone",
                    "--depth",
                    "1",
                    "--branch",
                    ref,
                    repo_url,
                    str(checkout),
                ],
                timeout_seconds=PLUGIN_COMMAND_TIMEOUT_SECONDS,
            )
            if not clone.get("ok"):
                clone = await _git(
                    ["clone", "--depth", "1", repo_url, str(checkout)],
                    timeout_seconds=PLUGIN_COMMAND_TIMEOUT_SECONDS,
                )
                _raise_if_failed("clone", clone)
                fetch = await _git(
                    ["-C", str(checkout), "fetch", "--depth", "1", "origin", ref],
                    timeout_seconds=PLUGIN_COMMAND_TIMEOUT_SECONDS,
                )
                _raise_if_failed("fetch ref", fetch)
                checkout_result = await _git(
                    ["-C", str(checkout), "checkout", "--detach", "FETCH_HEAD"],
                    timeout_seconds=60,
                )
                _raise_if_failed("checkout ref", checkout_result)
        commit_result = await _git(
            ["-C", str(checkout), "rev-parse", "HEAD"],
            timeout_seconds=60,
        )
        _raise_if_failed("read commit", commit_result)
        commit = str(commit_result.get("stdout") or "").strip() or None
        if exact_commit and str(commit or "").lower() != exact_commit:
            raise RuntimeError(
                "Tinyhat plugin checkout did not match the exact commit"
            )
        return checkout, commit, tmp
    except Exception:
        tmp.cleanup()
        raise


def _source_matches(snapshot: dict[str, Any], *, repo_url: str, ref: str) -> bool:
    source = snapshot.get("source")
    if not isinstance(source, dict):
        return False
    return (
        str(source.get("repo_url") or "") == repo_url
        and str(source.get("ref") or "") == ref
        and bool(source.get("commit"))
    )


async def _set_origin(name: str, repo_url: str) -> dict[str, Any] | None:
    target = plugin_dir(name)
    if not (target / ".git").exists():
        return None
    return await _git(["-C", str(target), "remote", "set-url", "origin", repo_url], timeout_seconds=60)


async def _install_from_ref(
    hermes_bin: Path,
    *,
    name: str,
    selection: PluginTargetSelection,
    force: bool,
    checkout_ref: str | None = None,
    expected_commit: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    checkout, commit, tmp = await _prepare_checkout(
        selection.repo_url,
        checkout_ref or selection.ref,
        fallback_ref=(
            selection.ref
            if checkout_ref
            and EXACT_COMMIT_RE.fullmatch(checkout_ref)
            and checkout_ref.lower() != selection.ref.lower()
            else None
        ),
    )
    try:
        clean_commit = str(commit or "").strip().lower() or None
        clean_expected_commit = str(expected_commit or "").strip().lower() or None
        if clean_expected_commit and clean_commit != clean_expected_commit:
            raise RuntimeError(
                "Tinyhat plugin checkout changed after target selection; "
                "the update was not installed"
            )
        install = await _install_plugin(
            hermes_bin,
            identifier=checkout.as_uri(),
            force=force,
        )
        _raise_if_failed("install", install)
        remote = await _set_origin(name, selection.repo_url)
        if remote is not None:
            _raise_if_failed("set origin", remote)
        metadata = {
            "repo_url": selection.repo_url,
            "ref": selection.ref,
            "commit": clean_commit,
        }
        _write_source_metadata(name, metadata)
        return install, metadata
    finally:
        tmp.cleanup()


async def install_tinyhat_plugin(
    command: dict[str, Any],
    *,
    force: bool = False,
) -> dict[str, Any]:
    hermes_bin = find_hermes_binary()
    name = plugin_name(command)
    before = plugin_snapshot(name)
    selection = _select_plugin_target(command, installed=before)
    repo_url = selection.repo_url
    ref = selection.ref
    requested_commit = plugin_target_commit(command)
    list_before: dict[str, Any] | None = None
    install: dict[str, Any] | None = None
    enable: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None

    if hermes_bin is None:
        raise RuntimeError("Hermes CLI was not found; run install_hermes first.")

    list_before = await _plugins_list(hermes_bin)
    if not before["installed"] or force:
        install, metadata = await _install_from_ref(
            hermes_bin,
            name=name,
            selection=selection,
            force=force or before["installed"],
            checkout_ref=requested_commit,
            expected_commit=requested_commit,
        )

    enable = await _enable_plugin(hermes_bin, name)
    _raise_if_failed("enable", enable)
    list_after = await _plugins_list(hermes_bin)
    after = plugin_snapshot(name)
    if not after["installed"]:
        raise RuntimeError(
            "Hermes reported plugin install success, but the Tinyhat runtime "
            "could not read plugin.yaml from Hermes' documented plugin "
            f"directory for {name!r}. Check HERMES_HOME/TINYHAT_HERMES_HOME "
            "or Hermes plugin path behavior."
        )

    return {
        "plugin_name": name,
        "plugin_repo_url": repo_url,
        "plugin_ref": ref,
        "target_commit": metadata.get("commit") if metadata else None,
        "installed_before": bool(before["installed"]),
        "installed_now": install is not None and bool(install.get("ok")),
        "installed_after": bool(after["installed"]),
        "changed": install is not None and bool(install.get("ok")),
        "before": before,
        "after": after,
        "commands": {
            "list_before": _compact_process(list_before),
            "install": _compact_process(install),
            "enable": _compact_process(enable),
            "list_after": _compact_process(list_after),
        },
    }


async def update_tinyhat_plugin(command: dict[str, Any]) -> dict[str, Any]:
    hermes_bin = find_hermes_binary()
    name = plugin_name(command)
    before = plugin_snapshot(name)
    selection = _select_plugin_target(command, installed=before)
    repo_url = selection.repo_url
    ref = selection.ref
    requested_commit = plugin_target_commit(command)
    install: dict[str, Any] | None = None
    enable: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None

    if hermes_bin is None:
        raise RuntimeError("Hermes CLI was not found; run install_hermes first.")

    list_before = await _plugins_list(hermes_bin)
    before_status = await tinyhat_plugin_status(
        command,
        selection=selection,
        installed=before,
        requested_commit=requested_commit,
    )
    if before_status.get("target_error"):
        raise RuntimeError(
            "Tinyhat plugin target could not be resolved: "
            f"{before_status.get('target_error')}"
        )
    target = (
        before_status.get("target")
        if isinstance(before_status.get("target"), dict)
        else None
    )
    target_commit = str((target or {}).get("commit") or "").strip() or None
    installed_commit = _source_commit(before)
    if not before["installed"] or not _source_matches(before, repo_url=repo_url, ref=ref) or (
        target_commit and target_commit != installed_commit
    ):
        install, metadata = await _install_from_ref(
            hermes_bin,
            name=name,
            selection=selection,
            force=bool(before["installed"]),
            checkout_ref=target_commit,
            expected_commit=target_commit,
        )
    else:
        metadata = before.get("source") if isinstance(before.get("source"), dict) else None

    enable = await _enable_plugin(hermes_bin, name)
    _raise_if_failed("enable", enable)
    list_after = await _plugins_list(hermes_bin)
    after = plugin_snapshot(name)
    if not after["installed"]:
        raise RuntimeError(
            "Hermes reported plugin update success, but the Tinyhat runtime "
            "could not read plugin.yaml from Hermes' documented plugin "
            f"directory for {name!r}. Check HERMES_HOME/TINYHAT_HERMES_HOME "
            "or Hermes plugin path behavior."
        )
    after_status = await tinyhat_plugin_status(
        command,
        selection=selection,
        installed=after,
        requested_commit=requested_commit,
    )

    return {
        "plugin_name": name,
        "plugin_repo_url": repo_url,
        "plugin_ref": ref,
        "target_commit": requested_commit
        or target_commit
        or (metadata.get("commit") if metadata else None),
        "target_version": (target or {}).get("version"),
        "installed_before": bool(before["installed"]),
        "installed_after": bool(after["installed"]),
        "updated_now": install is not None and bool(install.get("ok")),
        "installed_now": not before["installed"] and install is not None and bool(install.get("ok")),
        "changed": install is not None and bool(install.get("ok")),
        "before": before,
        "after": after,
        "before_status": before_status,
        "after_status": after_status,
        "update_available_after": after_status.get("update_available"),
        "commands": {
            "list_before": _compact_process(list_before),
            "install": _compact_process(install),
            "enable": _compact_process(enable),
            "list_after": _compact_process(list_after),
        },
        "reload": {
            "hermes_cli_sees_plugin": bool(after["installed"]),
            "gateway_restart_required": bool(install is not None),
            "message": (
                "Hermes has the updated plugin installed and enabled. Restart "
                "the Hermes gateway or run start_hermes after stopping it if a "
                "long-running Telegram gateway should reload plugin commands now."
            )
            if install is not None
            else "Hermes already had the selected plugin checkout enabled.",
        },
    }
