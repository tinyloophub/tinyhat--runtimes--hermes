"""Git credential helper for one Computer-scoped Tinyhat repository grant.

The helper receives no token in arguments or configuration. It asks the
platform broker for a short-lived exact-repository lease only when Git invokes
the ``get`` operation, writes the credential to Git's private helper pipe, and
does not persist it.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from typing import Any
from urllib.parse import quote

from hermes_runtime.platform_context import build_standalone_platform_context

_GRANT_ID_RE = re.compile(r"^rgr_[A-Za-z0-9_-]{16,56}$")
_OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$")


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--grant-id", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repo", required=True)
    parsed = parser.parse_args(argv)
    if _GRANT_ID_RE.fullmatch(parsed.grant_id) is None:
        raise RuntimeError("The repository credential grant id is invalid.")
    if _OWNER_RE.fullmatch(parsed.owner) is None:
        raise RuntimeError("The repository credential owner is invalid.")
    if _REPO_RE.fullmatch(parsed.repo) is None:
        raise RuntimeError("The repository credential name is invalid.")
    return parsed


def _credential_request(raw: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in raw.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        fields[key.strip()] = value.strip()
    return fields


def _matches_repository(
    fields: dict[str, str], *, owner: str, repo: str
) -> bool:
    protocol = fields.get("protocol", "").casefold()
    host = fields.get("host", "").casefold()
    path = fields.get("path", "").strip().strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return (
        protocol == "https"
        and host == "github.com"
        and path.casefold() == f"{owner}/{repo}".casefold()
    )


async def _lease(grant_id: str) -> dict[str, Any]:
    context = build_standalone_platform_context()
    return await context.client.post_json(
        context.computer_path(
            f"repository-grants/{quote(grant_id, safe='')}/leases"
        ),
        {},
    )


def main(argv: list[str] | None = None) -> int:
    try:
        explicit = list(sys.argv[1:] if argv is None else argv)
        # Git appends the operation after the configured helper command.
        operation = "get"
        if explicit and explicit[-1] in {"get", "store", "erase"}:
            operation = explicit.pop()
        args = _arguments(explicit)
        if operation != "get":
            return 0
        fields = _credential_request(sys.stdin.read())
        if not _matches_repository(fields, owner=args.owner, repo=args.repo):
            return 1
        payload = asyncio.run(_lease(args.grant_id))
        username = str(payload.get("username") or "").strip()
        token = str(payload.get("token") or "")
        if not username or not token:
            return 1
        # stdout is Git's credential-helper pipe. Do not log or echo this
        # payload anywhere else.
        sys.stdout.write(f"username={username}\npassword={token}\n\n")
        sys.stdout.flush()
        return 0
    except Exception:
        # A credential helper failure should be opaque: provider/platform
        # details can contain sensitive request context and Git will report a
        # normal authentication failure to the caller.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
