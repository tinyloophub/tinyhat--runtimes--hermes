"""Install the familiar browser and file manager for a Tinyhat desktop."""

from __future__ import annotations

import platform
import shutil
from pathlib import Path
from typing import Any

from hermes_runtime.hermes_cli import run_process

SCHEMA = "tinyhat_hermes_install_desktop_apps_v1"
SUPPORTED_ARCHITECTURES = {"aarch64", "amd64", "arm64", "x86_64"}


def _installer_path() -> Path:
    return Path(__file__).resolve().parents[1] / "install_desktop_apps.sh"


def _binary(names: tuple[str, ...]) -> Path | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def _supported() -> bool:
    return (
        platform.system().strip().lower() == "linux"
        and platform.machine().strip().lower() in SUPPORTED_ARCHITECTURES
    )


async def _program_status(
    *, names: tuple[str, ...], version_args: tuple[str, ...]
) -> dict[str, Any]:
    binary = _binary(names)
    if binary is None:
        return {"installed": False, "binary": None, "version": None}
    version_probe = await run_process(
        [str(binary), *version_args], timeout_seconds=30
    )
    version = str(version_probe.get("stdout") or "").strip().splitlines()
    return {
        "installed": True,
        "binary": str(binary),
        "version": version[0] if version else None,
        "probe": version_probe,
    }


async def _desktop_status() -> dict[str, Any]:
    return {
        "chrome": await _program_status(
            names=("google-chrome-stable", "google-chrome"),
            version_args=("--version",),
        ),
        "thunar": await _program_status(
            names=("thunar",),
            version_args=("--version",),
        ),
    }


def _ready(status: dict[str, Any]) -> bool:
    return all(
        bool((status.get(name) or {}).get("installed"))
        for name in ("chrome", "thunar")
    )


async def run(_ctx: Any, _command: dict[str, Any]) -> dict[str, Any]:
    """Ensure Chrome and Thunar using the installer used at provisioning."""

    before = await _desktop_status()
    install_result: dict[str, Any] | None = None
    if not _ready(before):
        install_result = await run_process(
            ["bash", str(_installer_path())],
            timeout_seconds=900,
        )
    after = await _desktop_status()
    supported = _supported()
    installed_before = _ready(before)
    installed_after = _ready(after)
    ok = installed_after if supported else bool(
        install_result is None or install_result.get("ok")
    )
    return {
        "schema": SCHEMA,
        "ok": ok,
        "supported": supported,
        "architecture": platform.machine(),
        "installed_before": installed_before,
        "installed_after": installed_after,
        "changed": not installed_before and installed_after,
        "before": before,
        "after": after,
        "install": install_result,
    }
