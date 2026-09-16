"""Framework selection, inspection, installation and owner approvals."""

from __future__ import annotations

import platform
import shutil

from hermes_runtime.channel_agent import control
from hermes_runtime.hermes_cli import run_process


async def run(ctx, command):
    kind, spec = command["kind"], command.get("spec") or {}
    from urllib.parse import urlencode

    from hermes_runtime.commands.configure_channels import api_path

    assignment = str(spec.get("assignment") or "")
    if not assignment:
        raise ValueError("A current Computer assignment is required.")
    current = await ctx.platform.get_json(
        api_path(ctx) + "?" + urlencode({"assignment": assignment})
    )
    if current.get("assignment") != assignment:
        raise ValueError("Computer assignment changed.")
    await control.bind(ctx, assignment)
    if kind.startswith("channels_use_"):
        return await control.request_switch(ctx, kind.removeprefix("channels_use_"))
    if kind == "channels_status":
        await control.inventory(ctx)
        return control.snapshot(ctx)
    if kind == "install_agent_framework":
        framework = spec.get("framework")
        if framework not in control.FRAMEWORKS:
            raise ValueError("Unknown framework.")
        before = await control.inventory(ctx)
        if not before[framework]["installed"]:
            if framework == "hermes":
                from hermes_runtime.commands.install_hermes import run as install

                await install(ctx, command)
            else:
                if platform.system() != "Linux" or not shutil.which("npm"):
                    raise RuntimeError(
                        "Framework installation requires the Computer's Linux Node installation."
                    )
                package = {
                    "codex": "@openai/codex@0.153.4",
                    "claude_code": "@anthropic-ai/claude-code@2.1.266",
                }[framework]
                result = await run_process(
                    ["npm", "install", "--global", package], timeout_seconds=600
                )
                if not result.get("ok"):
                    raise RuntimeError("Framework installation failed.")
        after = await control.inventory(ctx)
        if not after[framework]["installed"]:
            raise RuntimeError("Framework installation could not be verified.")
        return control.snapshot(ctx)
    raise ValueError("Unknown framework command.")
