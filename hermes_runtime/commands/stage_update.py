"""Stage a runtime version for activation on the next restart."""

from __future__ import annotations

import json
import time
from typing import Any


async def run(ctx: Any, command: dict[str, Any]) -> dict[str, Any]:
    spec = command.get("spec")
    if not isinstance(spec, dict):
        raise ValueError("stage_update requires a spec object")
    target_ref = str(spec.get("target_ref") or spec.get("target_version") or "").strip()
    if not target_ref:
        raise ValueError("stage_update requires target_ref")
    target_version = str(spec.get("target_version") or target_ref).strip()
    channel = str(spec.get("channel") or "lts").strip() or "lts"
    ctx.staged_version_file.parent.mkdir(parents=True, exist_ok=True)
    ctx.staged_version_file.write_text(target_ref + "\n", encoding="utf-8")
    metadata = {
        "target_ref": target_ref,
        "target_version": target_version,
        "channel": channel,
        "staged_at_unix": int(time.time()),
    }
    (ctx.staged_version_file.parent / "metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "message": f"staged {target_ref}",
        "target_ref": target_ref,
        "target_version": target_version,
        "channel": channel,
        "activation": "on_restart",
    }
