"""Resume one already-authorized Hat installation through the Tinyhat plugin.

The platform owns authorization and installation lifecycle state. This command
only gives it a deterministic bridge to the installed plugin without starting
a Hermes or language-model turn. Plugin output is validated on the Computer
and reduced to a value-blind status before it crosses the runtime boundary.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import sys
from typing import Any, Callable, TypeVar

from hermes_runtime.plugin_manager import DEFAULT_TINYHAT_PLUGIN_NAME, plugin_dir

SCHEMA = "tinyhat_hermes_resume_hat_installation_v1"
_PACKAGE_NAME = "_tinyhat_runtime_resume_plugin"
_MAX_PLUGIN_RESULT_CHARS = 128 * 1024
_PLUGIN_EXECUTION_ERROR = (
    "The installed Tinyhat plugin could not resume installation."
)
_ALLOWED_PLUGIN_KEYS = frozenset(
    {
        "installation_id",
        "hat_handle",
        "hat_title",
        "source",
        "status",
        "computer_id",
        "billing",
        "requirements",
        "repository_head_sha",
        "skills_installed",
        "credentials_requested",
        "credentials_installed",
        "installed_at",
        "last_error",
        "payment_required",
        "checkout",
        "next_action",
        "onboarding_message",
        "installation_started",
        "repository",
        "skills",
        "credential_transfer",
        "agent_instruction",
        "operation_telemetry",
    }
)
_WAITING_STATUSES = frozenset(
    {
        "authorized",
        "payment_pending",
        "assignment_pending",
        "skills_pending",
        "credentials_pending",
    }
)
_SENSITIVE_KEY_NAMES = frozenset(
    {
        "authorization",
        "ciphertext",
        "credential_value",
        "password",
        "plaintext",
        "private_key",
        "secret",
        "secret_value",
        "token",
    }
)
_ThreadResult = TypeVar("_ThreadResult")


def _remove_loaded_package() -> None:
    for name in tuple(sys.modules):
        if name == _PACKAGE_NAME or name.startswith(f"{_PACKAGE_NAME}."):
            sys.modules.pop(name, None)


def _is_sensitive_key(value: str) -> bool:
    normalized = value.strip().lower().replace("-", "_")
    return (
        normalized in _SENSITIVE_KEY_NAMES
        or normalized.endswith("_token")
        or normalized.endswith("_api_key")
    )


def _contains_sensitive_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            _is_sensitive_key(str(key)) or _contains_sensitive_key(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_sensitive_key(item) for item in value)
    return False


def _normalize_plugin_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or len(value) > _MAX_PLUGIN_RESULT_CHARS:
        raise RuntimeError("The installed Tinyhat plugin returned an invalid result.")
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        raise RuntimeError(
            "The installed Tinyhat plugin returned an invalid result."
        ) from None
    if not isinstance(payload, dict):
        raise RuntimeError("The installed Tinyhat plugin returned an invalid result.")
    if payload.get("status") == "error" or payload.get("schema") == "tinyhat_tool_error_v1":
        raise RuntimeError("The installed Tinyhat plugin could not resume installation.")
    if set(payload) - _ALLOWED_PLUGIN_KEYS or _contains_sensitive_key(payload):
        raise RuntimeError("The installed Tinyhat plugin returned an unsafe result.")

    status = payload.get("status")
    started_value = payload.get("installation_started")
    if status == "none":
        if started_value is not False or payload.get("onboarding_message") is not None:
            raise RuntimeError("The installed Tinyhat plugin returned an invalid result.")
        outcome = "none"
        installation_started = False
    elif status == "active":
        if not isinstance(started_value, bool):
            raise RuntimeError("The installed Tinyhat plugin returned an invalid result.")
        installation_started = started_value
        outcome = "started" if installation_started else "already_active"
    elif status in _WAITING_STATUSES:
        if started_value is not None and not isinstance(started_value, bool):
            raise RuntimeError("The installed Tinyhat plugin returned an invalid result.")
        installation_started = started_value is True
        if status in {"payment_pending", "assignment_pending"} and installation_started:
            raise RuntimeError("The installed Tinyhat plugin returned an invalid result.")
        outcome = "started" if installation_started else "waiting"
    else:
        raise RuntimeError("The installed Tinyhat plugin returned an invalid result.")

    return {
        "status": status,
        "outcome": outcome,
        "installation_started": installation_started,
    }


def _resume_from_installed_plugin() -> dict[str, Any]:
    root = plugin_dir(DEFAULT_TINYHAT_PLUGIN_NAME)
    init_path = root / "__init__.py"
    if not init_path.is_file():
        raise RuntimeError("The installed Tinyhat plugin is unavailable.")

    _remove_loaded_package()
    try:
        try:
            spec = importlib.util.spec_from_file_location(
                _PACKAGE_NAME,
                init_path,
                submodule_search_locations=[str(root)],
            )
            if spec is None or spec.loader is None:
                raise RuntimeError(_PLUGIN_EXECUTION_ERROR)
            package = importlib.util.module_from_spec(spec)
            sys.modules[_PACKAGE_NAME] = package
            spec.loader.exec_module(package)
            hats_module = importlib.import_module(f"{_PACKAGE_NAME}.hats")
            resume = getattr(hats_module, "hats", None)
            if not callable(resume):
                raise RuntimeError(_PLUGIN_EXECUTION_ERROR)
            result = resume({"action": "resume_installation"})
        except Exception:
            raise RuntimeError(_PLUGIN_EXECUTION_ERROR) from None
        return _normalize_plugin_result(result)
    finally:
        _remove_loaded_package()


async def _drain_worker(worker: asyncio.Task[_ThreadResult]) -> None:
    """Wait through repeated cancellation until the plugin worker stops."""

    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    if worker.done():
        try:
            worker.result()
        except BaseException:
            pass


async def _await_plugin_completion(
    function: Callable[[], _ThreadResult],
) -> _ThreadResult:
    """Do not release a cancelled command while its plugin thread is running."""

    worker = asyncio.create_task(asyncio.to_thread(function))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        await _drain_worker(worker)
        raise


async def run(_ctx: Any, _command: dict[str, Any]) -> dict[str, Any]:
    result = await _await_plugin_completion(_resume_from_installed_plugin)
    return {
        "schema": SCHEMA,
        **result,
    }
