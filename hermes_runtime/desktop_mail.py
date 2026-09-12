"""Configure a private Thunderbird profile without installing software at assignment."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from urllib.parse import urlsplit

from hermes_runtime.agent_systems import _write_atomic


def _directory(path: Path, mode: int = 0o700) -> None:
    if path.is_symlink():
        raise ValueError("Mail directory must not be a symbolic link")
    path.mkdir(mode=mode, exist_ok=True)
    if not path.is_dir() or path.stat().st_uid != os.getuid():
        raise ValueError("Mail directory has an unexpected owner")
    path.chmod(mode)


def configure(values: dict[str, str], *, home: Path | None = None) -> bool:
    """Update credentials atomically; keep messages, preferences and drafts intact.

    Credentials stay outside the project and are read by Thunderbird AutoConfig,
    never interpolated into JavaScript or passed on the command line. An already
    open client picks up renamed credentials when its owner next opens it.
    """
    if values.get("TINYHAT_EMAIL_CHANNEL_ENABLED") != "1":
        return False
    if not shutil.which("thunderbird"):
        # Older images can still run the Hermes email channel. The image/desktop
        # installer owns package installation; background assignment never does.
        return False
    address = values.get("TINYHAT_MAILBOX_ADDRESS", "")
    username = values.get("TINYHAT_MAILBOX_USERNAME", "")
    password = values.get("TINYHAT_MAILBOX_PASSWORD", "")
    parsed = urlsplit(values.get("TINYHAT_MAILBOX_JMAP_URL", ""))
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or not address
        or not username
        or not password
        or any(c in address + username for c in "\r\n\x00")
    ):
        raise ValueError("Mail client configuration is incomplete")
    home = home or Path.home()
    for directory in (home / ".config", home / ".config/tinyhat", home / ".config/tinyhat/mail"):
        _directory(directory)
    root = home / ".config/tinyhat/mail"
    _directory(root / "profile")
    settings = json.dumps(
        {"address": address, "username": username, "password": password, "host": parsed.hostname},
        sort_keys=True,
    ) + "\n"
    path = root / "settings.json"
    if path.is_symlink():
        raise ValueError("Mail credentials must not be a symbolic link")
    changed = not path.exists() or path.read_text() != settings
    if changed:
        _write_atomic(path, settings, 0o600)
    else:
        path.chmod(0o600)
    desktop = home / "Desktop"
    _directory(desktop, 0o755)
    entry = desktop / "Tinyhat Mail.desktop"
    _write_atomic(
        entry,
        "[Desktop Entry]\nType=Application\nName=Mail\n"
        "Comment=Your Tinyhat inbox\nExec=/usr/local/bin/tinyhat-mail\n"
        "Icon=thunderbird\nTerminal=false\nCategories=Network;Email;\n",
        0o755,
    )
    return changed
