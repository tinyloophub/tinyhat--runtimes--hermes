"""Export Tinyhat-saved secrets into Hermes terminal login shells.

Hermes deliberately strips its own provider/tool credentials from terminal
subprocess environments, so a secret a user saves through Tinyhat (platform
runtime secrets or the private secret handoff) never reaches the agent's
exec shell through process inheritance — the only supported way in is
sourcing a file inside the login shell that builds the terminal session
snapshot. This module is that file's engine: it prints ``export`` lines for
exactly the names Tinyhat manages, reading current values from Hermes' env
files at snapshot time so there is no second on-disk copy of any value.

Exported names come from two places:

- the ``# tinyhat runtime secrets`` managed block that ``apply_config``
  writes into the Hermes env files (platform-synced runtime secrets); and
- the names manifest at ``<hermes home>/tinyhat/terminal-env-names``, which
  the Tinyhat plugin registers after a private secret handoff saves a value
  with ``hermes config set`` (Tinyhat never sees those values, so the
  manifest holds names only).

Gateway-internal Hermes secrets (bot tokens, relay keys) stay unexported:
they are neither in the managed block nor in the manifest.

Usage (both are safe to run from a login shell hook):

    python3 -m hermes_runtime.terminal_env_export print-exports
    python3 -m hermes_runtime.terminal_env_export register EXA_API_KEY
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

from hermes_runtime.runtime_env import (
    env_file_candidates,
    hermes_home,
    read_env_values,
    read_managed_secret_names,
)

ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MANIFEST_RELATIVE_PATH = ("tinyhat", "terminal-env-names")


def manifest_path() -> Path:
    return hermes_home().joinpath(*MANIFEST_RELATIVE_PATH)


def read_manifest_names(path: Path | None = None) -> list[str]:
    manifest = path or manifest_path()
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    names: list[str] = []
    for line in lines:
        name = line.strip()
        if not name or name.startswith("#"):
            continue
        if ENV_NAME_RE.fullmatch(name) and name not in names:
            names.append(name)
    return names


def register_name(name: str) -> dict[str, object]:
    """Add one env name to the terminal export manifest (idempotent)."""
    clean = str(name or "").strip()
    if not ENV_NAME_RE.fullmatch(clean):
        raise ValueError(
            "Secret names must look like EXA_API_KEY (letters, digits, underscores)."
        )
    manifest = manifest_path()
    existing = read_manifest_names(manifest)
    added = clean not in existing
    if added:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        with manifest.open("a", encoding="utf-8") as handle:
            handle.write(f"{clean}\n")
    try:
        manifest.chmod(0o600)
    except OSError:
        pass
    return {
        "schema": "tinyhat_hermes_terminal_env_register_v1",
        "name": clean,
        "added": added,
        "manifest": str(manifest),
        "names": read_manifest_names(manifest),
        "terminal_env_hook": _refresh_terminal_env_hook(),
    }


def _refresh_terminal_env_hook() -> dict[str, object]:
    """Refresh the login-shell hook after a private-handoff registration.

    Fresh Computers install this hook during ``configure_telegram``. Calling it
    here as well covers upgraded Computers whose runtime is newer than the hook
    file already on disk. Registration must remain best-effort because older
    or non-root installs may not be able to write every activation path.
    """
    try:
        from hermes_runtime.terminal_env_hook import install_terminal_env_reload_hook

        return install_terminal_env_reload_hook()
    except Exception as exc:  # noqa: BLE001 - registration itself should survive.
        return {
            "installed": False,
            "error": str(exc)[:200],
            "failure_code": exc.__class__.__name__,
        }


def exportable_names() -> list[str]:
    """Names Tinyhat manages: managed-block names plus manifest names."""
    names: list[str] = []
    for path in env_file_candidates():
        try:
            lines = path.expanduser().read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for name in sorted(read_managed_secret_names(lines)):
            if name not in names:
                names.append(name)
    for name in read_manifest_names():
        if name not in names:
            names.append(name)
    return [name for name in names if ENV_NAME_RE.fullmatch(name)]


def render_export_lines() -> str:
    """Return ``export NAME=<quoted value>`` lines for shell ``eval``."""
    names = exportable_names()
    if not names:
        return ""
    values = read_env_values(env_file_candidates(), names=names)
    lines = [
        f"export {name}={shlex.quote(values[name])}"
        for name in names
        if name in values
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0] if args else "print-exports"
    if command == "print-exports":
        output = render_export_lines()
        if output:
            print(output)
        return 0
    if command == "register":
        if len(args) != 2:
            print("usage: terminal_env_export register <ENV_NAME>", file=sys.stderr)
            return 2
        try:
            result = register_name(args[1])
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps(result, sort_keys=True))
        return 0
    print(f"unknown terminal_env_export command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover - exercised via CLI tests
    raise SystemExit(main())
