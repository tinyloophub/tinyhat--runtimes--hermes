#!/usr/bin/env python3
"""Print or create a secondary dev release tag for local runtime testing."""

from __future__ import annotations

import argparse
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path


SUFFIX_RE = re.compile(r"[^0-9A-Za-z-]+")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def version_from_file(root: Path) -> str:
    value = (root / "VERSION").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise SystemExit("VERSION must be shaped X.Y.Z")
    return f"v{value}"


def clean_suffix(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = SUFFIX_RE.sub("-", value.strip()).strip("-")
    return cleaned or None


def make_tag(*, base: str, suffix: str | None) -> str:
    if not re.fullmatch(r"v\d+\.\d+\.\d+", base):
        raise SystemExit("--base must be shaped vX.Y.Z")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tag = f"{base}-dev.{stamp}"
    if suffix:
        tag = f"{tag}.{suffix}"
    return tag


def run_git(args: list[str], *, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True)  # noqa: S603,S607


def main() -> None:
    root = repo_root()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        default=version_from_file(root),
        help="Base final version for the dev tag, shaped vX.Y.Z.",
    )
    parser.add_argument(
        "--suffix",
        help="Optional prerelease suffix, for example smoke or local-heartbeat.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create an annotated local git tag instead of only printing it.",
    )
    args = parser.parse_args()

    tag = make_tag(base=args.base, suffix=clean_suffix(args.suffix))
    print(tag)
    if args.apply:
        run_git(["tag", "-a", tag, "-m", f"Dev runtime release {tag}"], cwd=root)


if __name__ == "__main__":
    main()
