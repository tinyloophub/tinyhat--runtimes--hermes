#!/usr/bin/env python3
"""Validate the minimal public Tinyhat Hermes runtime repository shape."""

from __future__ import annotations

import re
import sys
from pathlib import Path


REQUIRED_FILES = (
    "AGENTS.md",
    "CHANGELOG.md",
    "LICENSE",
    "README.md",
    "RELEASING.md",
    "VERSION",
    ".github/CODEOWNERS",
    ".github/workflows/ci.yml",
    "scripts/check_dev_skills.py",
    "scripts/check_repo_basics.py",
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
        "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash",
    )
    for phrase in required_readme_phrases:
        if phrase not in readme:
            fail(f"README.md missing phrase: {phrase}")

    agents = read(root, "AGENTS.md")
    for phrase in ("Official Interfaces Only", "platform_repos/runtimes/hermes"):
        if phrase not in agents:
            fail(f"AGENTS.md missing phrase: {phrase}")

    print("repo-basics: ok")


if __name__ == "__main__":
    main()
