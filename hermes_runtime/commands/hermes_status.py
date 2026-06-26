"""Check Hermes Agent through its public CLI.

What it does:
    Finds the ``hermes`` command and runs the three official status checks that
    Tinyhat currently cares about:

    - ``hermes --version``
    - ``hermes status``
    - ``hermes status --all``

When to use it:
    Use this from Hat admin after installing Hermes, after updating the Tinyhat
    runtime, or whenever you want a direct machine-side proof that Hermes can
    answer its own status command.

Example input:
    {"kind": "hermes_status", "spec": {}}

Example output:
    {
      "installed": true,
      "ok": true,
      "version": "Hermes Agent 0.1.0",
      "commands": {
        "version": {"returncode": 0, "stdout": "..."},
        "status": {"returncode": 0, "stdout": "..."},
        "status_all": {"returncode": 0, "stdout": "..."}
      }
    }

Side effects:
    None. It runs read-only Hermes CLI commands and returns bounded stdout and
    stderr for admin inspection.
"""

from __future__ import annotations

from typing import Any

from hermes_runtime.hermes_cli import probe_hermes_status


async def run(_ctx: Any, _command: dict[str, Any]) -> dict[str, Any]:
    return await probe_hermes_status()
