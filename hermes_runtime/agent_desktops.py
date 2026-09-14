"""Official Linux desktop installation and launchers; never reads login material.

Run explicitly with ``python -m hermes_runtime.agent_desktops --install`` inside
the Computer or image builder. Installation is separate from provider sign-in.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

DESKTOP_APPS = {
    "codex": ("chatgpt", "ChatGPT", "chatgpt"),
    "claude_code": ("claude-desktop", "Claude", "claude-desktop"),
}


def inventory() -> dict[str, dict[str, bool]]:
    # Launching Electron with --version can open a GUI or hang. Presence only;
    # model login and a working GUI must be verified interactively by the owner.
    return {
        system: {"installed": bool(shutil.which(command))}
        for system, (command, _, _) in DESKTOP_APPS.items()
    }


def write_launchers(home: Path, system: str) -> None:
    from hermes_runtime.agent_systems import SYSTEM_COMMANDS, _write_atomic

    if system not in SYSTEM_COMMANDS:
        return
    for other_system, (_, other_label, _) in DESKTOP_APPS.items():
        entry = home / "Desktop" / (other_label + ".desktop")
        if other_system != system and entry.is_file():
            if b"X-Tinyhat-Managed=true\n" in entry.read_bytes():
                entry.unlink()
    app = DESKTOP_APPS.get(system)
    if app is None or not shutil.which(app[0]):
        return
    command, label, icon = app
    launcher = home / ".local/bin" / ("tinyhat-" + command)
    # Tinyhat's existing private XFCE/VNC session runs as the Computer owner
    # (root on current images). Electron requires this flag in that session.
    _write_atomic(
        launcher,
        "#!/bin/sh\nset -eu\n"
        'if [ "$(id -u)" = 0 ]; then set -- --no-sandbox "$@"; fi\n'
        f'exec {command} --disable-dev-shm-usage --disable-gpu "$@"\n',
        0o700,
    )
    desktop_exec = str(launcher).replace("\\", "\\\\").replace('"', '\\"')
    _write_atomic(
        home / "Desktop" / (label + ".desktop"),
        "[Desktop Entry]\nVersion=1.0\nType=Application\nX-Tinyhat-Managed=true\n"
        f"Name={label}\nComment=Open {label} desktop\n"
        f'Exec="{desktop_exec}"\nIcon={icon}\nTerminal=false\nCategories=Development;\n',
        0o755,
    )


def main() -> None:
    from hermes_runtime.agent_systems import SYSTEM_COMMANDS

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--system", choices=list(SYSTEM_COMMANDS))
    args = parser.parse_args()
    if args.install:
        subprocess.run(
            ["bash", str(Path(__file__).with_name("install_coding_agent_apps.sh"))],
            check=True,
            timeout=1800,
        )
    if args.system:
        write_launchers(Path.home(), args.system)
    print(
        json.dumps(
            {"desktop_apps": inventory(), "authentication": "verify_in_provider_app"}
        )
    )


if __name__ == "__main__":
    main()
