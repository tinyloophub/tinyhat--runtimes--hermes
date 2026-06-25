"""Stage a runtime version for activation on the next restart.

What it does:
    Downloads or copies the selected runtime package into the staged update
    directory and records the selected runtime ref. The running process keeps
    using the current code until a later activation/restart step.

Update flow map:
    [pick target release]
        -> check_update             look only; writes updates/last_check.json
        -> stage_update             prepare selected ref; current runtime keeps running
        -> activate_update          mark staged ref and request service restart
        -> restart_runtime_service  optional plain restart; no staging changes
        -> service startup          promote staged ref into current/VERSION

    This command is the prepare step. It does not switch the running runtime.
    The selected version is used only after activate_update restarts the
    tinyhat Hermes runtime service and startup promotes staged -> current.

When to use it:
    Use this from Hat admin after choosing an exact runtime release that should
    be prepared but not activated yet.

Example input:
    {
      "kind": "stage_update",
      "spec": {
        "channel": "custom",
        "target_ref": "v0.0.2",
        "target_version": "v0.0.2",
        "target_sha": "abc123..."
      }
    }

Example output:
    {
      "message": "staged v0.0.2",
      "target_ref": "v0.0.2",
      "activation": "requires_activate_update"
    }

Side effects:
    Writes ``staged/VERSION``, ``staged/metadata.json``, and a staged
    ``staged/runtime/hermes_runtime`` package under runtime state. It does not
    change the running process, reboot the VPS, or restart Hermes. When
    ``target_sha`` is supplied, the runtime downloads that immutable commit
    instead of the movable target ref.
"""

from __future__ import annotations

import json
import time
from typing import Any

from hermes_runtime.update_artifacts import prepare_staged_runtime


async def run(ctx: Any, command: dict[str, Any]) -> dict[str, Any]:
    spec = command.get("spec")
    if not isinstance(spec, dict):
        raise ValueError("stage_update requires a spec object")
    target_ref = str(spec.get("target_ref") or spec.get("target_version") or "").strip()
    if not target_ref:
        raise ValueError("stage_update requires target_ref")
    target_version = str(spec.get("target_version") or target_ref).strip()
    target_sha = str(spec.get("target_sha") or "").strip() or None
    channel = str(spec.get("channel") or "lts").strip() or "lts"
    staged_artifact = prepare_staged_runtime(
        state_dir=ctx.state_dir,
        target_ref=target_ref,
        target_sha=target_sha,
    )
    ctx.staged_version_file.parent.mkdir(parents=True, exist_ok=True)
    ctx.staged_version_file.write_text(target_ref + "\n", encoding="utf-8")
    metadata = {
        "target_ref": target_ref,
        "target_version": target_version,
        "target_sha": target_sha,
        "channel": channel,
        "staged_at_unix": int(time.time()),
        "artifact": staged_artifact,
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
        "code_staged": True,
        "package_dir": staged_artifact["package_dir"],
        "activation": "requires_activate_update",
    }
