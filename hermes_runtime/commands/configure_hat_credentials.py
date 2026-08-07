"""Initiate grouped Hat credential entry through the installed Tinyhat plugin."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import re
import sys
from typing import Any

from hermes_runtime.plugin_manager import DEFAULT_TINYHAT_PLUGIN_NAME, plugin_dir

SCHEMA = "tinyhat_hermes_configure_hat_credentials_v1"
_PACKAGE_NAME = "_tinyhat_runtime_command_plugin"
_HANDLE_PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _canonical_hat_handle(value: Any) -> str:
    handle = str(value or "").strip()
    parts = handle.split("/")
    if (
        len(handle) > 255
        or len(parts) != 3
        or parts[1] != "hats"
        or any(_HANDLE_PART_RE.fullmatch(part) is None for part in (parts[0], parts[2]))
    ):
        raise RuntimeError("configure_hat_credentials requires a canonical Hat handle.")
    return handle


def _remove_loaded_package() -> None:
    for name in tuple(sys.modules):
        if name == _PACKAGE_NAME or name.startswith(f"{_PACKAGE_NAME}."):
            sys.modules.pop(name, None)


def _start_from_installed_plugin(handle: str) -> str:
    root = plugin_dir(DEFAULT_TINYHAT_PLUGIN_NAME)
    init_path = root / "__init__.py"
    if not init_path.is_file():
        raise RuntimeError("The installed Tinyhat plugin is unavailable.")

    _remove_loaded_package()
    spec = importlib.util.spec_from_file_location(
        _PACKAGE_NAME,
        init_path,
        submodule_search_locations=[str(root)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("The installed Tinyhat plugin could not be loaded.")
    package = importlib.util.module_from_spec(spec)
    sys.modules[_PACKAGE_NAME] = package
    try:
        spec.loader.exec_module(package)
        secret_handoff = importlib.import_module(f"{_PACKAGE_NAME}.secret_handoff")
        starter = getattr(secret_handoff, "start_hat_credentials_handoff", None)
        if not callable(starter):
            raise RuntimeError(
                "The installed Tinyhat plugin does not support grouped Hat credentials."
            )
        message = str(starter(handle))
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and payload.get("status") == "error":
            raise RuntimeError(
                str(payload.get("message") or "Could not prepare Hat credentials.")
            )
        return message
    finally:
        _remove_loaded_package()


async def run(_ctx: Any, command: dict[str, Any]) -> dict[str, Any]:
    spec = command.get("spec") if isinstance(command.get("spec"), dict) else {}
    handle = _canonical_hat_handle(spec.get("hat_handle"))
    message = await asyncio.to_thread(_start_from_installed_plugin, handle)
    return {
        "schema": SCHEMA,
        "started": True,
        "hat_handle": handle,
        "message": message,
        "telegram_button_requested": True,
        "gateway_restart_requested": False,
    }
