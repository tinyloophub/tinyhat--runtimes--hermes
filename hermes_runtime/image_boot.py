"""Initialize a clean preinstalled Computer without downloading software.

Invoked by the platform's per-instance startup launcher. Image provenance is
checked before writing machine configuration. All provider/user authentication
stays outside this module and outside the reusable image.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from hermes_runtime.agent_systems import _write_atomic

PREFIX = Path("/opt/tinyhat-hermes-runtime")
STATE = Path("/var/lib/tinyhat-hermes-runtime")
MANIFEST = Path("/opt/tinyhat-agent-image/manifest.json")


def _https_origin(value: str) -> str:
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError("The platform address must not contain whitespace or control characters")
    parsed = urlsplit(value)
    if re.fullmatch(r"[A-Za-z0-9.-]+", parsed.hostname or "") is None:
        raise ValueError("The platform address must use a valid hostname")
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("The platform address must be an HTTPS origin")
    return value.rstrip("/")


def configure(*, platform_url: str, audience: str, computer_id: str,
              runtime_sha: str, manifest_sha256: str, root: Path = Path("/")) -> None:
    platform_url = _https_origin(platform_url)
    audience = _https_origin(audience)
    if re.fullmatch(r"[1-9][0-9]{0,18}", computer_id) is None:
        raise ValueError("Invalid Computer id")
    if re.fullmatch(r"[0-9a-f]{40}", runtime_sha) is None or re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None:
        raise ValueError("Image provenance must use exact digests")
    def local(path: Path) -> Path:
        return root / path.relative_to("/")
    manifest_bytes = local(MANIFEST).read_bytes()
    manifest = json.loads(manifest_bytes)
    if hashlib.sha256(manifest_bytes).hexdigest() != manifest_sha256 or manifest.get("runtime_sha") != runtime_sha or manifest.get("schema") != "tinyhat.agent-image.v1":
        raise ValueError("Image provenance does not match the requested runtime")
    if local(STATE / "current/COMMIT_SHA").read_text().strip() != runtime_sha:
        raise ValueError("Installed runtime differs from image provenance")
    if not local(PREFIX / "bin/tinyhat-hermes-runtime").is_file():
        raise ValueError("The preinstalled runtime launcher is missing")
    env = {
        "TINYHAT_RUNTIME_REF": runtime_sha,
        "TINYHAT_RUNTIME_PREFIX": str(PREFIX),
        "TINYHAT_RUNTIME_STATE_DIR": str(STATE),
        "TINYHAT_PLATFORM_URL": platform_url,
        "TINYHAT_COMPUTER_TOKEN_AUDIENCE": audience,
        "TINYHAT_COMPUTER_ID": computer_id,
        "TINYHAT_AGENT_SYSTEMS_PREINSTALLED": "1",
        "TINYHAT_UNASSIGNED_HEARTBEAT_INTERVAL_SECONDS": "2",
        "TINYHAT_ASSIGNED_HEARTBEAT_INTERVAL_SECONDS": "2",
    }
    # Validated values contain no whitespace/quotes and work in both the
    # systemd EnvironmentFile and installer-compatible shell environment.
    content = "".join(f"{key}={value}\n" for key, value in env.items())
    _write_atomic(local(PREFIX / "env/runtime.env"), content, 0o600)
    _write_atomic(local(Path("/etc/tinyhat/hermes-runtime.env")), content, 0o600)
    unit = """[Unit]
Description=Tinyhat preinstalled Hermes runtime
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/tinyhat/hermes-runtime.env
ExecStart=/opt/tinyhat-hermes-runtime/bin/tinyhat-hermes-runtime
Restart=always
RestartSec=2
Nice=-5
OOMScoreAdjust=-900

[Install]
WantedBy=multi-user.target
"""
    _write_atomic(local(Path("/etc/systemd/system/tinyhat-hermes-runtime.service")), unit, 0o644)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ("platform-url", "audience", "computer-id", "runtime-sha", "manifest-sha256"):
        parser.add_argument("--" + argument, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0 or not Path("/run/systemd/system").is_dir():
        raise SystemExit("Preinstalled image startup requires Linux systemd and root")
    configure(**vars(args))
    # Clean-image preparation removes these identities. Generate per-clone
    # values through the supported OS tools; never replace existing host keys.
    subprocess.run(["systemd-machine-id-setup"], check=True)
    subprocess.run(["ssh-keygen", "-A"], check=True)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", "tinyhat-hermes-runtime.service"], check=True)


if __name__ == "__main__":
    main()
