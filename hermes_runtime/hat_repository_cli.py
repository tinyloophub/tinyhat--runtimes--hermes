"""JSON-stdin CLI for Tinyhat's Computer-local Hat Git workflow."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from hermes_runtime.hat_repository import run, safe_error_payload
from hermes_runtime.platform_context import build_standalone_platform_context


async def _main() -> int:
    try:
        payload: Any = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            raise RuntimeError("The Hat repository command must be an object.")
        result = await run(build_standalone_platform_context(), payload)
    except Exception as exc:
        result = safe_error_payload(exc)
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0 if result.get("status") != "error" else 1


def main() -> None:
    raise SystemExit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
