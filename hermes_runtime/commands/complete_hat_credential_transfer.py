"""Prepare one pre-authorized Hat credential transfer through the plugin.

The platform dispatches this command to the exact Computer registered as the
Hat's credential host.  The runtime is intentionally a thin, value-blind
adapter: the installed Tinyhat plugin reads the local encrypted values,
encrypts them for the consumer, signs the envelope, and submits ciphertext to
the platform.  No Hermes/LLM turn and no creator interaction are involved.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import re
import sys
from typing import Any

from hermes_runtime.plugin_manager import DEFAULT_TINYHAT_PLUGIN_NAME, plugin_dir

SCHEMA = "tinyhat_hermes_hat_credential_transfer_v1"
PLUGIN_RESULT_SCHEMA = "tinyhat_hat_credential_transfer_result_v1"
_PACKAGE_NAME = "_tinyhat_runtime_command_plugin"
_HANDLE_PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_HANDOFF_RE = re.compile(r"^sh_[A-Za-z0-9_-]{16,64}$")
_RESULT_KEYS = frozenset(
    {
        "schema",
        "handoff_id",
        "hat_handle",
        "credential_count",
        "submitted",
        "authenticated_envelope",
        "value_available",
    }
)


def _canonical_hat_handle(value: Any) -> str:
    handle = str(value or "").strip()
    parts = handle.split("/")
    if (
        len(handle) > 255
        or len(parts) != 3
        or parts[1] != "hats"
        or any(_HANDLE_PART_RE.fullmatch(part) is None for part in (parts[0], parts[2]))
    ):
        raise RuntimeError(
            "complete_hat_credential_transfer requires a canonical Hat handle."
        )
    return handle


def _handoff_id(value: Any) -> str:
    handoff_id = str(value or "").strip()
    if _HANDOFF_RE.fullmatch(handoff_id) is None:
        raise RuntimeError(
            "complete_hat_credential_transfer requires a valid handoff id."
        )
    return handoff_id


def _remove_loaded_package() -> None:
    for name in tuple(sys.modules):
        if name == _PACKAGE_NAME or name.startswith(f"{_PACKAGE_NAME}."):
            sys.modules.pop(name, None)


def _validate_plugin_result(
    value: Any, *, handoff_id: str, hat_handle: str
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - _RESULT_KEYS:
        raise RuntimeError("The Tinyhat plugin returned an unsafe transfer result.")
    if any(
        (
            value.get("schema") != PLUGIN_RESULT_SCHEMA,
            str(value.get("handoff_id") or "") != handoff_id,
            str(value.get("hat_handle") or "") != hat_handle,
            value.get("submitted") is not True,
            value.get("authenticated_envelope") is not True,
            value.get("value_available") is not False,
            not isinstance(value.get("credential_count"), int),
            int(value.get("credential_count") or 0) < 1,
        )
    ):
        raise RuntimeError("The Tinyhat plugin returned an invalid transfer result.")
    return dict(value)


def _complete_from_installed_plugin(
    *, handoff_id: str, hat_handle: str
) -> dict[str, Any]:
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
        transfer = importlib.import_module(f"{_PACKAGE_NAME}.hat_transfer")
        completer = getattr(transfer, "complete_hat_credential_transfer", None)
        if not callable(completer):
            raise RuntimeError(
                "The installed Tinyhat plugin does not support automatic Hat transfers."
            )
        result = completer(handoff_id, expected_hat_handle=hat_handle)
        return _validate_plugin_result(
            result,
            handoff_id=handoff_id,
            hat_handle=hat_handle,
        )
    finally:
        _remove_loaded_package()


async def run(_ctx: Any, command: dict[str, Any]) -> dict[str, Any]:
    spec = command.get("spec") if isinstance(command.get("spec"), dict) else {}
    handoff_id = _handoff_id(spec.get("handoff_id"))
    hat_handle = _canonical_hat_handle(spec.get("hat_handle"))
    result = await asyncio.to_thread(
        _complete_from_installed_plugin,
        handoff_id=handoff_id,
        hat_handle=hat_handle,
    )
    return {
        "schema": SCHEMA,
        "transfer": result,
    }
