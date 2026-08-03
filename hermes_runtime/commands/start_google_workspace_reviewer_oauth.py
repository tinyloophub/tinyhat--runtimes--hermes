"""Start one platform-bound Google Workspace reviewer OAuth handoff.

This command is deliberately narrower than a generic plugin invocation. The
platform supplies one opaque reviewer request id, and the runtime calls one
documented function from the installed Tinyhat plugin. Neither the plugin
result nor any OAuth material is returned to the platform command ledger.
"""

from __future__ import annotations

import asyncio
import importlib
import re
import sys
import threading
import types
from typing import Any

from hermes_runtime.plugin_manager import DEFAULT_TINYHAT_PLUGIN_NAME, plugin_dir

COMMAND_SCHEMA = "tinyhat_hermes_google_workspace_reviewer_oauth_start_v1"
PLUGIN_RESULT_SCHEMA = "tinyhat_google_workspace_reviewer_oauth_start_v1"
PLUGIN_PACKAGE_NAME = "_tinyhat_hermes_runtime_installed_plugin"
PLUGIN_MODULE_NAME = f"{PLUGIN_PACKAGE_NAME}.google_workspace"
PLUGIN_FUNCTION_NAME = "start_google_workspace_reviewer_oauth"
PLUGIN_FAILURE_MESSAGE = (
    "The installed Tinyhat plugin could not start Google Workspace reviewer OAuth."
)
REVIEWER_REQUEST_ID_RE = re.compile(r"^gwrq_[A-Za-z0-9_-]{43}$")
EXPECTED_PLUGIN_RESULT = {
    "schema": PLUGIN_RESULT_SCHEMA,
    "action": "reviewer_oauth_start",
    "status": "started",
}
_PLUGIN_IMPORT_LOCK = threading.Lock()


def _validated_reviewer_request_id(command: dict[str, Any]) -> str:
    spec = command.get("spec")
    if not isinstance(spec, dict) or set(spec) != {"reviewer_request_id"}:
        raise ValueError(
            "start_google_workspace_reviewer_oauth spec must contain exactly "
            "reviewer_request_id."
        )
    reviewer_request_id = spec["reviewer_request_id"]
    if (
        not isinstance(reviewer_request_id, str)
        or REVIEWER_REQUEST_ID_RE.fullmatch(reviewer_request_id) is None
    ):
        raise ValueError(
            "start_google_workspace_reviewer_oauth requires a valid opaque "
            "reviewer_request_id."
        )
    return reviewer_request_id


def _clear_loaded_plugin_modules() -> None:
    prefix = f"{PLUGIN_PACKAGE_NAME}."
    for name in tuple(sys.modules):
        if name == PLUGIN_PACKAGE_NAME or name.startswith(prefix):
            sys.modules.pop(name, None)


def _invoke_installed_plugin(reviewer_request_id: str) -> None:
    """Invoke only the installed plugin's documented reviewer OAuth function."""

    root = plugin_dir(DEFAULT_TINYHAT_PLUGIN_NAME)
    module_path = root / "google_workspace.py"
    package_init = root / "__init__.py"
    with _PLUGIN_IMPORT_LOCK:
        _clear_loaded_plugin_modules()
        try:
            if not package_init.is_file() or not module_path.is_file():
                raise RuntimeError(PLUGIN_FAILURE_MESSAGE)

            # The installed Tinyhat plugin is a Python package. Build only the
            # package namespace needed for documented relative imports; do not
            # execute its registration entry point or any generic Hermes tool.
            package = types.ModuleType(PLUGIN_PACKAGE_NAME)
            package.__file__ = str(package_init)
            package.__package__ = PLUGIN_PACKAGE_NAME
            package.__path__ = [str(root)]  # type: ignore[attr-defined]
            sys.modules[PLUGIN_PACKAGE_NAME] = package

            module = importlib.import_module(PLUGIN_MODULE_NAME)
            start = getattr(module, PLUGIN_FUNCTION_NAME, None)
            if not callable(start):
                raise RuntimeError(PLUGIN_FAILURE_MESSAGE)
            plugin_result = start(reviewer_request_id)
            if plugin_result != EXPECTED_PLUGIN_RESULT:
                raise RuntimeError(PLUGIN_FAILURE_MESSAGE)
        except Exception:
            # Plugin exceptions and return values are not trusted for platform
            # reporting: either could contain an authorization URL or another
            # transient OAuth value. Keep every failure value-blind as well.
            raise RuntimeError(PLUGIN_FAILURE_MESSAGE) from None
        finally:
            _clear_loaded_plugin_modules()


async def run(_ctx: Any, command: dict[str, Any]) -> dict[str, Any]:
    reviewer_request_id = _validated_reviewer_request_id(command)
    await asyncio.to_thread(_invoke_installed_plugin, reviewer_request_id)
    return {
        "schema": COMMAND_SCHEMA,
        "action": "reviewer_oauth_start",
        "status": "started",
    }
