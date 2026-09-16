"""Detect installed transport changes without interrupting native work."""

import hashlib
from pathlib import Path

from hermes_runtime.plugin_manager import DEFAULT_TINYHAT_PLUGIN_NAME, plugin_dir


def installed_revision():
    digest = hashlib.sha256()
    paths = sorted(Path(__file__).parent.glob("*.py"))
    root = plugin_dir(DEFAULT_TINYHAT_PLUGIN_NAME)
    paths += [
        root / "capabilities/channels/methods.json",
        root / "capabilities/mail/ingress.py",
    ]
    for path in paths:
        digest.update(path.name.encode())
        digest.update(path.read_bytes() if path.exists() else b"missing")
    return digest.hexdigest()
