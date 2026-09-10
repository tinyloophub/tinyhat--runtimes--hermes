#!/usr/bin/env python3
"""Replay local-test heartbeat responses through the runtime in a temporary home."""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hermes_runtime import computer_metadata
from hermes_runtime.main import RuntimeContext, _heartbeat_once


def main() -> None:
    responses = [json.loads((Path(sys.argv[1]) / name).read_text()) for name in ("created.json", "assigned.json", "ready.json")]
    # Only exercise metadata. The replay must never execute recorded commands.
    for response in responses:
        response["command"] = None
        response["agent_api_context"] = None
    with tempfile.TemporaryDirectory() as directory:
        home = Path(directory)
        platform = SimpleNamespace(post_json=AsyncMock(side_effect=responses + [responses[-1]]))
        ctx = RuntimeContext(platform=platform, state_dir=home / "state", started_at=0)
        with patch.object(computer_metadata.Path, "home", return_value=home), patch("hermes_runtime.main._refresh_gateway_state", new_callable=AsyncMock), patch("hermes_runtime.main._maybe_start_scheduled_update_check"), patch("hermes_runtime.main._maybe_start_gateway_reconcile"):
            async def walk():
                for phase in ("created", "assigned", "ready"):
                    await _heartbeat_once(ctx)
                    saved = json.loads((home / "tinyhat/computer.json").read_text())
                    assignment = saved["assignment"]
                    print(f'{phase}: creation_ms={saved["creation"]["duration_ms"]} assignment_ms={assignment["duration_ms"] if assignment else None}')
                snapshot = home / "tinyhat/computer.json"
                before = snapshot.stat().st_mtime_ns
                await _heartbeat_once(ctx)
                assert snapshot.stat().st_mtime_ns == before
                print("unchanged heartbeat: file mtime preserved")
            asyncio.run(walk())
        print("files:", ", ".join(sorted(p.name for p in (home / "tinyhat").iterdir())))
        print("Linux heartbeat-to-file smoke: PASS")


if __name__ == "__main__":
    main()
