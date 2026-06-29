"""Check whether the Tinyhat plugin has an available update.

What it does:
    Resolves the configured Tinyhat plugin ref, ``channels/lts`` by default,
    reads that plugin manifest, and compares the target commit with the commit
    recorded next to the installed plugin. It only looks and reports; it does
    not install or update the plugin.

When to use it:
    Use this from Hat admin before running ``update_tinyhat_plugin``, or when
    you want a manual plugin freshness check in addition to the daily runtime
    update check.

Example input:
    {"kind": "check_tinyhat_plugin_update", "spec": {}}

Example output:
    {
      "installed_version": "0.20.0",
      "target_version": "0.20.1",
      "update_available": true,
      "decision": "target_ref_changed"
    }

Side effects:
    Read-only. It may make a Git request and use a temporary checkout of the
    public plugin repo. It does not modify Hermes, platform state, or local
    plugin files.
"""

from __future__ import annotations

from typing import Any

from hermes_runtime.plugin_manager import tinyhat_plugin_status


async def run(_ctx: Any, command: dict[str, Any]) -> dict[str, Any]:
    result = await tinyhat_plugin_status(command)
    return {
        "schema": "tinyhat_hermes_plugin_update_check_v1",
        "message": "plugin update check complete",
        **result,
    }
