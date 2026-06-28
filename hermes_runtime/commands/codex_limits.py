"""Return the OpenAI Codex usage limits visible to this Computer.

What it does:
    Starts the official Codex app-server CLI over stdio:
    ``codex app-server --listen stdio://``. It initializes that app-server and
    calls ``account/rateLimits/read``. That is the same Codex account surface
    OpenClaw uses for remaining subscription windows, reset times, plan type,
    and reset-credit information.

When to use it:
    Use this after the user has connected OpenAI Codex auth through Hermes. It
    lets Hat admin and Telegram show how much of the user's Codex subscription
    window remains without asking Tinyhat to handle the user's OpenAI token.

Example input:
    {"kind": "codex_limits", "spec": {}}

Example output:
    {
      "schema": "tinyhat_hermes_codex_limits_v1",
      "ok": true,
      "source": "codex app-server",
      "method": "account/rateLimits/read",
      "summary": {"limits": [{"label": "Codex", "windows": [...]}]},
      "limits": {"rateLimits": {...}}
    }

Side effects:
    None in Tinyhat platform state. Codex may refresh its own local
    auth/session state while serving the request. The command writes the last
    structured app-server response to ``codex/last_limits.json`` under the
    runtime state directory so operators can debug from JSON instead of
    terminal logs. The command does not read or return OpenAI auth tokens and
    does not call the normal OpenAI REST API.
"""

from __future__ import annotations

from typing import Any

from hermes_runtime.codex_limits import read_codex_limits


async def run(_ctx: Any, _command: dict[str, Any]) -> dict[str, Any]:
    return await read_codex_limits()
