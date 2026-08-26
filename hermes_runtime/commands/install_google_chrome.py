"""Install Google Chrome Stable for an interactive Tinyhat Computer desktop."""

from __future__ import annotations

import platform
import shutil
from pathlib import Path
from typing import Any

from hermes_runtime.hermes_cli import run_process

SCHEMA = "tinyhat_hermes_install_google_chrome_v1"
SUPPORTED_ARCHITECTURES = {"aarch64", "amd64", "arm64", "x86_64"}


def _installer_path() -> Path:
    return Path(__file__).resolve().parents[1] / "install_google_chrome.sh"


def _chrome_binary() -> Path | None:
    for name in ("google-chrome-stable", "google-chrome"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def _supported() -> bool:
    return (
        platform.system().strip().lower() == "linux"
        and platform.machine().strip().lower() in SUPPORTED_ARCHITECTURES
    )


async def _chrome_status() -> dict[str, Any]:
    binary = _chrome_binary()
    if binary is None:
        return {"installed": False, "binary": None, "version": None}
    version_probe = await run_process([str(binary), "--version"], timeout_seconds=30)
    version = str(version_probe.get("stdout") or "").strip() or None
    return {
        "installed": True,
        "binary": str(binary),
        "version": version,
        "probe": version_probe,
    }


async def run(_ctx: Any, _command: dict[str, Any]) -> dict[str, Any]:
    """Ensure Chrome is present using the installer used at provisioning."""

    before = await _chrome_status()
    install_result: dict[str, Any] | None = None
    if not before["installed"]:
        install_result = await run_process(
            ["bash", str(_installer_path())],
            timeout_seconds=900,
        )
    after = await _chrome_status()
    supported = _supported()
    ok = bool(after["installed"]) if supported else bool(
        install_result is None or install_result.get("ok")
    )
    return {
        "schema": SCHEMA,
        "ok": ok,
        "supported": supported,
        "architecture": platform.machine(),
        "installed_before": bool(before["installed"]),
        "installed_after": bool(after["installed"]),
        "changed": not bool(before["installed"]) and bool(after["installed"]),
        "before": before,
        "after": after,
        "install": install_result,
    }
