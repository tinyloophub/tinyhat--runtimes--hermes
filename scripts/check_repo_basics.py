#!/usr/bin/env python3
"""Validate the minimal public Tinyhat Hermes runtime repository shape."""

from __future__ import annotations

import re
import sys
from pathlib import Path


REQUIRED_FILES = (
    "AGENTS.md",
    "CHANGELOG.md",
    "Dockerfile",
    "LICENSE",
    "README.md",
    "RELEASING.md",
    "VERSION",
    "VERSIONING.md",
    "install.sh",
    ".github/CODEOWNERS",
    ".github/workflows/ci.yml",
    ".github/workflows/dev-release.yml",
    "hermes_runtime/__init__.py",
    "hermes_runtime/client.py",
    "hermes_runtime/main.py",
    "hermes_runtime/update_check.py",
    "hermes_runtime/commands/__init__.py",
    "hermes_runtime/commands/ping.py",
    "hermes_runtime/commands/whoami.py",
    "hermes_runtime/commands/check_update.py",
    "hermes_runtime/commands/update_status.py",
    "hermes_runtime/commands/recent_commands.py",
    "hermes_runtime/commands/setup_snapshot.py",
    "hermes_runtime/commands/stage_update.py",
    "hermes_runtime/commands/activate_update.py",
    "scripts/check_dev_skills.py",
    "scripts/check_repo_basics.py",
    "scripts/make_dev_release_tag.py",
    "scripts/publish_dev_release.py",
    "tests/test_commands.py",
    "tests/test_dev_release_script.py",
    "tests/test_install_script.py",
)


def fail(message: str) -> None:
    print(f"repo-basics: {message}", file=sys.stderr)
    raise SystemExit(1)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def read(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


def main() -> None:
    root = repo_root()
    for rel in REQUIRED_FILES:
        if not (root / rel).is_file():
            fail(f"missing {rel}")

    version = read(root, "VERSION").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        fail("VERSION must be shaped X.Y.Z")

    codeowners = read(root, ".github/CODEOWNERS")
    if "@farid-tinyloop" not in codeowners:
        fail("CODEOWNERS must require the Tinyloop maintainer")

    readme = read(root, "README.md")
    required_readme_phrases = (
        "tinyhat/runtimes/hermes",
        "tinyloophub/tinyhat--runtimes--hermes",
        "raw.githubusercontent.com/tinyloophub/tinyhat--runtimes--hermes/channels/lts/install.sh",
        "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash",
        "## How a Computer is set up",
        "## Heartbeat protection",
        "## Transparency and trust layer",
        "systemctl cat tinyhat-hermes-runtime.service",
        "OOMScoreAdjust=-900",
        "## Command whitelist",
        "`ping`",
        "`whoami`",
        "`check_update`",
        "`update_status`",
        "`recent_commands`",
        "`setup_snapshot`",
        "`stage_update`",
        "`activate_update`",
        "config/update_check_time",
        "config/update_check_timezone",
        "## Update channels",
        "vX.Y.Z-dev.YYYYMMDDTHHMMSSZ",
        "publish_dev_release.py",
        "channels/latest",
        "channels/lts",
    )
    for phrase in required_readme_phrases:
        if phrase not in readme:
            fail(f"README.md missing phrase: {phrase}")

    agents = read(root, "AGENTS.md")
    for phrase in ("Official Interfaces Only", "platform_repos/runtimes/hermes"):
        if phrase not in agents:
            fail(f"AGENTS.md missing phrase: {phrase}")

    versioning = read(root, "VERSIONING.md")
    for phrase in (
        "Release lifecycle",
        "before the PR branch is merged",
        "channels/latest",
        "channels/lts",
    ):
        if phrase not in versioning:
            fail(f"VERSIONING.md missing phrase: {phrase}")

    release_skill = read(root, ".agents/skills/release/SKILL.md")
    for phrase in (
        "secondary development releases",
        "publish_dev_release.py",
        "README.md command whitelist",
        "channels/latest",
        "channels/lts",
    ):
        if phrase not in release_skill:
            fail(f".agents/skills/release/SKILL.md missing phrase: {phrase}")

    commands = read(root, "hermes_runtime/commands/__init__.py")
    for command in re.findall(r'"([a-z_]+)"\s*:', commands):
        if f"`{command}`" not in readme:
            fail(f"README.md command table missing `{command}`")

    dev_release_workflow = read(root, ".github/workflows/dev-release.yml")
    for phrase in ("workflow_dispatch", "publish_dev_release.py", "GH_TOKEN"):
        if phrase not in dev_release_workflow:
            fail(f".github/workflows/dev-release.yml missing phrase: {phrase}")

    print("repo-basics: ok")


if __name__ == "__main__":
    main()
