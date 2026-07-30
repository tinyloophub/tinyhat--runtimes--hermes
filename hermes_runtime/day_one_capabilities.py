"""Versioned defaults for a newly provisioned Tinyhat Hermes Computer.

These values are intentionally public and centralized. Computer creation uses
the public runtime's ``install_hermes`` command before assignment, so the user
does not wait for browser binaries or optional Python packages when their agent
is attached later.
"""

from __future__ import annotations

BASELINE_ID = "tinyhat-hermes-day-one-v1"

# Runtime releases deliberately advance this value after capability
# verification instead of letting fresh Computers follow upstream ``main``.
HERMES_UPSTREAM_COMMIT = "646761c7831ff4c4cd0d6ac711ed791d487fb665"

# Optional upstream dependencies that Tinyhat promises on a fresh Computer.
DDGS_VERSION = "9.14.4"
EDGE_TTS_VERSION = "7.2.7"
PLAYWRIGHT_VERSION = "1.58.2"

# Google Workspace CLI is installed from its official, statically linked musl
# release assets so the same binary works on both the Debian local-Computer
# image and Ubuntu GCE Computers. The archive digest is verified before the
# executable is installed.
GOOGLE_WORKSPACE_CLI_VERSION = "0.22.5"
GOOGLE_WORKSPACE_CLI_RELEASE_TAG = f"v{GOOGLE_WORKSPACE_CLI_VERSION}"
GOOGLE_WORKSPACE_CLI_LINUX_ASSETS = {
    "x86_64": {
        "target": "x86_64-unknown-linux-musl",
        "sha256": (
            "4db473dde4b1ab872e4ff35d769b0d4a"
            "f1f1a6441a605e79d5cf8ada9c87e920"
        ),
    },
    "aarch64": {
        "target": "aarch64-unknown-linux-musl",
        "sha256": (
            "e700fe63524932b10ec2130b47ece90a"
            "a850e66005fe52ccfc4cf8767bf9919a"
        ),
    },
}

WEB_SEARCH_BACKEND = "ddgs"
BROWSER_CLOUD_PROVIDER = "local"
BROWSER_ENGINE = "chrome"
IMAGE_GENERATION_PROVIDER = "openrouter"
IMAGE_GENERATION_MODEL = "google/gemini-3.1-flash-image"
TTS_PROVIDER = "edge"
TELEGRAM_RICH_MESSAGES = True
TELEGRAM_RICH_DRAFTS = False
