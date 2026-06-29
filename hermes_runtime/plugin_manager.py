"""Helpers for managing the Tinyhat Hermes plugin through public CLI commands."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from hermes_runtime.hermes_cli import find_hermes_binary, run_process

DEFAULT_TINYHAT_PLUGIN_REPO_URL = "https://github.com/tinyhat-ai/tinyhat.git"
DEFAULT_TINYHAT_PLUGIN_REF = "channels/lts"
DEFAULT_TINYHAT_PLUGIN_NAME = "tinyhat"
PLUGIN_COMMAND_TIMEOUT_SECONDS = 300
PLUGIN_SOURCE_METADATA = ".tinyhat-plugin-source.json"


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


def plugin_repo_url(command: dict[str, Any]) -> str:
    spec = _command_spec(command)
    raw = (
        spec.get("plugin_repo_url")
        or spec.get("repo_url")
        or os.getenv("TINYHAT_PLUGIN_REPO_URL")
        or DEFAULT_TINYHAT_PLUGIN_REPO_URL
    )
    return str(raw).strip() or DEFAULT_TINYHAT_PLUGIN_REPO_URL


def plugin_ref(command: dict[str, Any]) -> str:
    spec = _command_spec(command)
    raw = (
        spec.get("plugin_ref")
        or spec.get("ref")
        or os.getenv("TINYHAT_PLUGIN_REF")
        or DEFAULT_TINYHAT_PLUGIN_REF
    )
    return str(raw).strip() or DEFAULT_TINYHAT_PLUGIN_REF


def hermes_home() -> Path:
    raw = (
        os.getenv("TINYHAT_HERMES_HOME")
        or os.getenv("HERMES_HOME")
        or str(Path.home() / ".hermes")
    )
    return Path(raw).expanduser()


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
    path.write_text(json.dumps(metadata, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def plugin_snapshot(name: str) -> dict[str, Any]:
    root = plugin_dir(name)
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


async def _resolve_ref(repo_url: str, ref: str) -> str | None:
    result = await _git(["ls-remote", repo_url, ref], timeout_seconds=60)
    if not result.get("ok"):
        return None
    stdout = str(result.get("stdout") or "")
    first = stdout.strip().splitlines()[0] if stdout.strip() else ""
    sha = first.split()[0] if first else ""
    if re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        return sha.lower()
    if re.fullmatch(r"[0-9a-fA-F]{7,40}", ref):
        return ref.lower()
    return None


async def _prepare_checkout(repo_url: str, ref: str) -> tuple[Path, str | None, tempfile.TemporaryDirectory[str]]:
    tmp = tempfile.TemporaryDirectory(prefix="tinyhat-plugin-")
    checkout = Path(tmp.name) / "tinyhat"
    clone = await _git(
        ["clone", "--depth", "1", "--branch", ref, repo_url, str(checkout)],
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
    commit_result = await _git(["-C", str(checkout), "rev-parse", "HEAD"], timeout_seconds=60)
    _raise_if_failed("read commit", commit_result)
    commit = str(commit_result.get("stdout") or "").strip() or None
    return checkout, commit, tmp


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
    repo_url: str,
    ref: str,
    force: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    checkout, commit, tmp = await _prepare_checkout(repo_url, ref)
    try:
        install = await _install_plugin(
            hermes_bin,
            identifier=checkout.as_uri(),
            force=force,
        )
        _raise_if_failed("install", install)
        remote = await _set_origin(name, repo_url)
        if remote is not None:
            _raise_if_failed("set origin", remote)
        metadata = {
            "repo_url": repo_url,
            "ref": ref,
            "commit": commit,
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
    repo_url = plugin_repo_url(command)
    ref = plugin_ref(command)
    before = plugin_snapshot(name)
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
            repo_url=repo_url,
            ref=ref,
            force=force or before["installed"],
        )

    enable = await _enable_plugin(hermes_bin, name)
    _raise_if_failed("enable", enable)
    list_after = await _plugins_list(hermes_bin)
    after = plugin_snapshot(name)
    if not after["installed"]:
        raise RuntimeError("Hermes plugin install completed, but plugin.yaml was not found.")

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
    repo_url = plugin_repo_url(command)
    ref = plugin_ref(command)
    before = plugin_snapshot(name)
    install: dict[str, Any] | None = None
    enable: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None

    if hermes_bin is None:
        raise RuntimeError("Hermes CLI was not found; run install_hermes first.")

    list_before = await _plugins_list(hermes_bin)
    target_commit = await _resolve_ref(repo_url, ref)
    installed_commit = (
        before.get("source", {}).get("commit")
        if isinstance(before.get("source"), dict)
        else None
    )
    if not before["installed"] or not _source_matches(before, repo_url=repo_url, ref=ref) or (
        target_commit and target_commit != installed_commit
    ):
        install, metadata = await _install_from_ref(
            hermes_bin,
            name=name,
            repo_url=repo_url,
            ref=ref,
            force=bool(before["installed"]),
        )
    else:
        metadata = before.get("source") if isinstance(before.get("source"), dict) else None

    enable = await _enable_plugin(hermes_bin, name)
    _raise_if_failed("enable", enable)
    list_after = await _plugins_list(hermes_bin)
    after = plugin_snapshot(name)
    if not after["installed"]:
        raise RuntimeError("Hermes plugin update completed, but plugin.yaml was not found.")

    return {
        "plugin_name": name,
        "plugin_repo_url": repo_url,
        "plugin_ref": ref,
        "target_commit": target_commit or (metadata.get("commit") if metadata else None),
        "installed_before": bool(before["installed"]),
        "installed_after": bool(after["installed"]),
        "updated_now": install is not None and bool(install.get("ok")),
        "installed_now": not before["installed"] and install is not None and bool(install.get("ok")),
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
