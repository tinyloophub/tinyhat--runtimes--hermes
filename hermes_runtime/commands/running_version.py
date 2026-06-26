"""Return the runtime version from the code handling this command.

What it does:
    Reads the version from the imported ``hermes_runtime`` package that is
    currently executing inside this Python process. It also reports the module
    file path and package directory that Python imported.

Why this exists:
    State files such as ``current/VERSION`` are useful, but they are still
    metadata. After an update, operators need a direct proof that the running
    service has actually imported the new runtime package. This command answers
    that question without relying on a newly added command being present.

When to use it:
    Run this from Hat admin after ``activate_update`` or
    ``restart_runtime_service``. If ``code_version`` is the expected release,
    the running service is using that runtime code.

Example input:
    {"kind": "running_version", "spec": {}}

Example output:
    {
      "code_version": "0.0.4",
      "module_file": "/opt/tinyhat-hermes-runtime/hermes_runtime/__init__.py",
      "package_dir": "/opt/tinyhat-hermes-runtime/hermes_runtime"
    }

Side effects:
    None. It reads the already-imported Python module object only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import hermes_runtime
from hermes_runtime import __version__


async def run(_ctx: Any, _command: dict[str, Any]) -> dict[str, Any]:
    module_file = Path(getattr(hermes_runtime, "__file__", "") or "")
    package_dir = module_file.parent if module_file else None
    return {
        "schema": "tinyhat_hermes_running_version_v1",
        "code_version": __version__,
        "module_file": str(module_file) if module_file else None,
        "package_dir": str(package_dir) if package_dir else None,
        "proof": (
            "code_version comes from the hermes_runtime package imported by "
            "the Python process that executed this command."
        ),
    }
