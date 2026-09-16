"""On-demand local session view. Never a heartbeat or command-ledger payload."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from hermes_runtime.channel_agent import control


async def request(ctx, binding, action="status", *, approval_id=None, decision=None):
    if not binding or control.assignment(ctx) != binding:
        raise ValueError("Computer assignment changed.")
    if action == "approve":
        if control.selected(ctx) == "hermes" or decision not in {"allow", "deny"}:
            raise ValueError("Native action is unavailable.")
        return await control.rpc(
            ctx, "approve", approval_id=approval_id, decision=decision
        )
    if action != "status":
        raise ValueError("Unknown live session action.")
    try:
        path = control.directory(ctx) / "status.json"
        with path.open("rb") as file:
            raw = file.read(2**20 + 1)
        if len(raw) > 2**20:
            raise ValueError("Session view is too large.")
        saved = json.loads(raw)
    except FileNotFoundError:
        saved = {}
    # Only a live native receiver can accept an approval. Historical task rows
    # remain readable on this Computer after switching to Hermes.
    current = control.snapshot(ctx)
    if control.assignment(ctx) != binding:
        raise ValueError("Computer assignment changed.")
    return {
        "tasks": [
            {
                key: task.get(key)
                for key in ("id", "title", "status", "framework", "updated", "error")
            }
            for task in saved.get("tasks", [])[:30]
            if isinstance(task, dict)
        ],
        "approvals": saved.get("approvals", [])[:4]
        if current["active"] != "hermes" and current["status"] == "running"
        else [],
    }


def main():
    parser = argparse.ArgumentParser(
        description="Read sessions directly from this Computer."
    )
    parser.add_argument("--assignment", required=True)
    parser.add_argument("--action", choices=("status", "approve"), default="status")
    parser.add_argument("--approval-id")
    parser.add_argument("--decision", choices=("allow", "deny"))
    parser.add_argument(
        "--state-dir",
        default=os.getenv(
            "TINYHAT_RUNTIME_STATE_DIR", "/var/lib/tinyhat-hermes-runtime"
        ),
    )
    args = parser.parse_args()
    try:
        result = asyncio.run(
            request(
                SimpleNamespace(state_dir=Path(args.state_dir)),
                args.assignment,
                args.action,
                approval_id=args.approval_id,
                decision=args.decision,
            )
        )
    except Exception:
        # Do not leak local paths, session text or provider errors to SSH logs.
        print("Session view unavailable.", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
