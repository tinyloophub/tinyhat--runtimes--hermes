"""Update the Tinyhat Hermes plugin to the latest available version.

What it does:
    Resolves the selected logical Tinyhat plugin ref and compares it with the
    repo/ref/commit metadata recorded next to the installed plugin. A full
    ``target_commit`` or ``target_sha`` may pin the checked commit while the
    logical channel/tag remains in installed metadata. If the target changed,
    the runtime prepares that exact checkout and asks Hermes to reinstall it:

        hermes plugins install file:///prepared/tinyhat-checkout --enable --force

    If the plugin is missing, this command falls back to the same install path
    as ``install_tinyhat_plugin``. After either path, it enables the plugin and
    verifies that the installed plugin metadata now matches the selected
    target. A long-running Hermes Telegram gateway may still need a Hermes
    restart to reload plugin commands already held in memory.

When to use it:
    Run this from Hat admin when a Computer should pick up the latest Tinyhat
    plugin without changing the Tinyhat runtime itself. This is the command to
    use for plugin-level feature rollout.

Example input:
    {"kind": "update_tinyhat_plugin", "spec": {
      "plugin_ref": "channels/lts",
      "target_commit": "0123456789abcdef0123456789abcdef01234567"
    }}

Example output:
    {
      "updated_now": true,
      "installed_after": true,
      "plugin_name": "tinyhat",
      "after": {"version": "0.20.1"}
    }

Side effects:
    Runs Hermes' public plugin install/enable commands for the resolved channel
    ref when an update is needed. It does not restart Hermes Agent by itself;
    run ``stop_hermes`` then ``start_hermes`` when an already-running Telegram
    gateway should reload the new plugin immediately.
"""

from __future__ import annotations

from typing import Any

from hermes_runtime.plugin_manager import update_tinyhat_plugin


async def run(_ctx: Any, command: dict[str, Any]) -> dict[str, Any]:
    result = await update_tinyhat_plugin(command)
    return {
        "schema": "tinyhat_hermes_plugin_update_v1",
        **result,
    }
