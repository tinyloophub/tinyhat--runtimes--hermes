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
HERMES_UPSTREAM_COMMIT = "40a53ca0317b0ddc1a79133fb70fc5eb75c3d74b"

# Optional upstream dependencies that Tinyhat promises on a fresh Computer.
DDGS_VERSION = "9.14.4"
EDGE_TTS_VERSION = "7.2.7"

WEB_SEARCH_BACKEND = "ddgs"
BROWSER_CLOUD_PROVIDER = "local"
BROWSER_ENGINE = "chrome"
IMAGE_GENERATION_PROVIDER = "openrouter"
IMAGE_GENERATION_MODEL = "google/gemini-3.1-flash-image"
TTS_PROVIDER = "edge"
TELEGRAM_RICH_MESSAGES = True
TELEGRAM_RICH_DRAFTS = False
