"""Return a concise machine setup snapshot.

What it does:
    Summarizes the local runtime installation without asking the operator to
    SSH into the machine. It reads the installed runtime ref, current version,
    current commit, important directories, and selected systemd service
    properties when systemd is available.

When to use it:
    Use this from Hat admin after installing or updating a Computer to verify
    the runtime service is installed, restart-protected, and pointing at the
    expected runtime files.

Example input:
    {"kind": "setup_snapshot", "spec": {}}

Example output:
    {
      "service": {
        "systemctl_available": true,
        "properties": {"Restart": "always", "Nice": "-5"}
      },
      "install": {"install_ref": {"value": "channels/lts"}},
      "state": {"current_version": {"value": "0.0.1"}}
    }

Side effects:
    None. It reads files and systemd metadata only. It never reads env file
    contents and never uses sudo.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

from hermes_runtime import __version__


SERVICE_NAME = "tinyhat-hermes-runtime.service"
DEFAULT_INSTALL_ROOT = Path("/opt/tinyhat-hermes-runtime")
DEFAULT_STATE_ROOT = Path("/var/lib/tinyhat-hermes-runtime")
MAX_TEXT_CHARS = 12_000
MAX_DIRECTORY_ENTRIES = 20


def _path_from_env(name: str, default: Path) -> Path:
    value = (os.getenv(name) or "").strip()
    return Path(value) if value else default


def _path_status(path: Path) -> dict[str, Any]:
    try:
        info = path.stat()
    except FileNotFoundError:
        return {"path": str(path), "exists": False}
    except OSError as exc:
        return {"path": str(path), "exists": None, "error": str(exc)}
    if stat.S_ISDIR(info.st_mode):
        kind = "directory"
    elif stat.S_ISREG(info.st_mode):
        kind = "file"
    else:
        kind = "other"
    return {
        "path": str(path),
        "exists": True,
        "kind": kind,
        "mode": oct(stat.S_IMODE(info.st_mode)),
        "size_bytes": info.st_size,
    }


def _directory_entries(path: Path) -> list[dict[str, Any]]:
    try:
        entries = sorted(path.iterdir(), key=lambda item: item.name)
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        return []
    return [_path_status(entry) for entry in entries[:MAX_DIRECTORY_ENTRIES]]


def _read_state_file(path: Path, *, max_chars: int = 4096) -> dict[str, Any]:
    try:
        value = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"path": str(path), "exists": False}
    except OSError as exc:
        return {"path": str(path), "exists": None, "error": str(exc)}
    clean = value.strip()
    return {
        "path": str(path),
        "exists": True,
        "value": clean[:max_chars],
        "truncated": len(clean) > max_chars,
    }


def _run_systemctl(args: list[str]) -> dict[str, Any]:
    if shutil.which("systemctl") is None:
        return {
            "systemctl_available": False,
            "ok": False,
            "message": "systemctl is not installed in this environment.",
        }
    try:
        completed = subprocess.run(
            ["systemctl", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except subprocess.TimeoutExpired:
        return {
            "systemctl_available": True,
            "ok": False,
            "message": "systemctl timed out after 3 seconds.",
        }
    return {
        "systemctl_available": True,
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": completed.stdout[:MAX_TEXT_CHARS],
        "stderr": completed.stderr[:MAX_TEXT_CHARS],
    }


def _parse_systemctl_show(output: str) -> dict[str, str]:
    properties: dict[str, str] = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        properties[key] = value
    return properties


def _service_summary() -> dict[str, Any]:
    show = _run_systemctl(
        [
            "show",
            SERVICE_NAME,
            "-p",
            "ActiveState",
            "-p",
            "SubState",
            "-p",
            "UnitFileState",
            "-p",
            "FragmentPath",
            "-p",
            "Restart",
            "-p",
            "Nice",
            "-p",
            "OOMScoreAdjust",
            "--no-pager",
        ]
    )
    cat = _run_systemctl(["cat", SERVICE_NAME, "--no-pager"])
    return {
        "name": SERVICE_NAME,
        "systemctl_available": bool(show.get("systemctl_available")),
        "show_ok": bool(show.get("ok")),
        "cat_ok": bool(cat.get("ok")),
        "properties": _parse_systemctl_show(str(show.get("stdout") or "")),
        "unit_file_excerpt": str(cat.get("stdout") or "")[:MAX_TEXT_CHARS],
        "errors": [
            message
            for message in (show.get("message"), show.get("stderr"), cat.get("stderr"))
            if message
        ],
    }


def _warnings(
    *,
    service: dict[str, Any],
    current_version: dict[str, Any],
    install_ref: dict[str, Any],
) -> list[str]:
    warnings: list[str] = []
    properties = service.get("properties")
    props = properties if isinstance(properties, dict) else {}
    expected = {
        "Restart": "always",
        "Nice": "-5",
        "OOMScoreAdjust": "-900",
    }
    for key, expected_value in expected.items():
        actual = str(props.get(key) or "").strip()
        if actual and actual != expected_value:
            warnings.append(f"systemd {key} is {actual}, expected {expected_value}")
    if not service.get("systemctl_available"):
        warnings.append("systemctl is unavailable; service protection was not verified")
    if current_version.get("exists") is not True:
        warnings.append("current runtime VERSION file is missing")
    if install_ref.get("exists") is not True:
        warnings.append("INSTALL_REF is missing")
    return warnings


async def run(ctx: Any, _command: dict[str, Any]) -> dict[str, Any]:
    state_root = Path(getattr(ctx, "state_dir", None) or _path_from_env(
        "TINYHAT_RUNTIME_STATE_DIR",
        DEFAULT_STATE_ROOT,
    ))
    install_root = _path_from_env("TINYHAT_RUNTIME_PREFIX", DEFAULT_INSTALL_ROOT)
    current_dir = state_root / "current"
    install_ref = _read_state_file(install_root / "INSTALL_REF")
    current_version = _read_state_file(current_dir / "VERSION")
    current_commit_sha = _read_state_file(current_dir / "COMMIT_SHA")
    service = _service_summary()
    return {
        "schema": "tinyhat_hermes_setup_snapshot_v1",
        "runtime_code_version": __version__,
        "service": service,
        "install": {
            "root": _path_status(install_root),
            "entries": _directory_entries(install_root),
            "install_ref": install_ref,
            "env_file": _path_status(install_root / "env" / "runtime.env"),
        },
        "state": {
            "root": _path_status(state_root),
            "entries": _directory_entries(state_root),
            "current_dir": _path_status(current_dir),
            "current_version": current_version,
            "current_commit_sha": current_commit_sha,
        },
        "expected_service_protection": {
            "Restart": "always",
            "Nice": "-5",
            "OOMScoreAdjust": "-900",
        },
        "warnings": _warnings(
            service=service,
            current_version=current_version,
            install_ref=install_ref,
        ),
    }
