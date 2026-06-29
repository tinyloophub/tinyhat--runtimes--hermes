"""Install the Tinyhat plugin for Hermes Agent if it is missing.

What it does:
    Checks the documented Hermes plugin directory for the Tinyhat plugin. If
    it is missing, the runtime resolves the configured Tinyhat plugin ref
    (``channels/lts`` by default), prepares that exact checkout, and uses the
    public Hermes plugin CLI to install it:

        hermes plugins install file:///prepared/tinyhat-checkout --enable

    If the plugin already exists, the command does not reinstall it. It still
    enables the plugin with ``hermes plugins enable tinyhat`` so a disabled
    plugin can be recovered without replacing files.

When to use it:
    Hat admin queues this automatically during Computer creation after Hermes
    Agent is installed. Operators can also run it manually from the Computer
    page when they want to warm a machine before assigning it to an agent.

Example input:
    {"kind": "install_tinyhat_plugin", "spec": {}}

Example output:
    {
      "installed_before": false,
      "installed_now": true,
      "installed_after": true,
      "plugin_name": "tinyhat",
      "after": {"version": "0.20.0"}
    }

Side effects:
    Runs Hermes' public plugin install and enable commands. Does not read or
    write Tinyhat platform credentials and does not configure Telegram. Records
    the repo URL, ref, and commit in ``.tinyhat-plugin-source.json`` next to the
    installed plugin.
"""

from __future__ import annotations

from typing import Any

from hermes_runtime.plugin_manager import install_tinyhat_plugin


async def run(_ctx: Any, command: dict[str, Any]) -> dict[str, Any]:
    result = await install_tinyhat_plugin(command)
    return {
        "schema": "tinyhat_hermes_plugin_install_v1",
        **result,
    }
