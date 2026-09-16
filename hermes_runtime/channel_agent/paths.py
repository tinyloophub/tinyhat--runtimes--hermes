"""Keep IPC on the local filesystem, separate from persistent mounted state."""

import hashlib
import os
from pathlib import Path


def socket_path(directory):
    identity = hashlib.sha256(str(Path(directory).resolve()).encode()).hexdigest()[:24]
    # A macOS bind mount in a Linux Computer cannot store UNIX sockets. /tmp is
    # local to the Computer and also keeps the path below UNIX socket limits.
    return Path(f"/tmp/tinyhat-channel-{os.getuid()}-{identity}") / "agent.sock"


def prepare_socket_directory(directory):
    parent = socket_path(directory).parent
    parent.mkdir(mode=0o700, exist_ok=True)
    stat = parent.lstat()
    if parent.is_symlink() or not parent.is_dir() or stat.st_uid != os.getuid():
        raise RuntimeError("Channel socket directory is not owned by this user.")
    parent.chmod(0o700)
