"""Show the Tinyhat plugin version installed in Hermes.

What it does:
    Reads the Tinyhat plugin installed under Hermes' plugin directory and
    compares it with the configured plugin target, ``channels/lts`` by default.
    It reports both the installed version/commit and the target version/commit.

When to use it:
    Use this from Hat admin when you want to know what plugin skills/tools this
    Computer currently exposes to Hermes, without changing anything.

Example input:
    {"kind": "tinyhat_plugin_status", "spec": {}}

Example output:
    {
      "installed_version": "0.20.0",
      "target_version": "0.20.1",
      "update_available": true,
      "decision": "target_ref_changed"
    }

Side effects:
    Read-only. It may clone the public plugin repo into a temporary directory
    so it can read the target manifest, then deletes that temporary checkout.
"""

from __future__ import annotations

from typing import Any

from hermes_runtime.plugin_manager import tinyhat_plugin_status


async def run(_ctx: Any, command: dict[str, Any]) -> dict[str, Any]:
    result = await tinyhat_plugin_status(command)
    return {
        "schema": "tinyhat_hermes_plugin_status_v1",
        **result,
    }
