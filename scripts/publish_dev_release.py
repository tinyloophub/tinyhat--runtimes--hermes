#!/usr/bin/env python3
"""Publish a secondary dev release tag for PR-branch runtime testing."""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path


REPO = "tinyloophub/tinyhat--runtimes--hermes"
RAW_BASE = f"https://raw.githubusercontent.com/{REPO}"
TAG_RE = re.compile(
    r"^v\d+\.\d+\.\d+-dev\.\d{8}T\d{6}Z(?:\.[0-9A-Za-z][0-9A-Za-z.-]*)?$"
)
BASE_RE = re.compile(r"^v\d+\.\d+\.\d+$")
SUFFIX_RE = re.compile(r"[^0-9A-Za-z-]+")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def version_from_file(root: Path) -> str:
    value = (root / "VERSION").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise SystemExit("VERSION must be shaped X.Y.Z")
    return f"v{value}"


def normalize_base(value: str) -> str:
    clean = value.strip()
    if re.fullmatch(r"\d+\.\d+\.\d+", clean):
        clean = f"v{clean}"
    if not BASE_RE.fullmatch(clean):
        raise SystemExit("--base must be shaped vX.Y.Z")
    return clean


def clean_suffix(value: str | None) -> str:
    cleaned = SUFFIX_RE.sub("-", (value or "dev").strip()).strip("-")
    return cleaned or "dev"


def make_tag(*, base: str, suffix: str, now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tag = f"{normalize_base(base)}-dev.{stamp}.{clean_suffix(suffix)}"
    if not TAG_RE.fullmatch(tag):
        raise SystemExit(f"generated invalid dev tag: {tag}")
    return tag


def installer_command(tag: str) -> str:
    if not TAG_RE.fullmatch(tag):
        raise SystemExit(f"invalid dev tag: {tag}")
    return f"curl -fsSL {RAW_BASE}/{tag}/install.sh | bash -s -- --ref {tag}"


def default_notes(*, tag: str, target: str) -> str:
    return "\n".join(
        [
            f"Secondary dev runtime release `{tag}`.",
            "",
            "Purpose: test this exact runtime from GitHub before the PR branch is merged.",
            f"Target ref: `{target}`",
            "",
            "Install:",
            "```bash",
            installer_command(tag),
            "```",
            "",
            "This dev release must not move `channels/latest` or `channels/lts`.",
        ]
    )


def run(
    args: list[str],
    *,
    cwd: Path,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - argv list, caller controls command.
        args,
        cwd=cwd,
        check=True,
        text=True,
        capture_output=capture,
    )


def git_output(args: list[str], *, cwd: Path) -> str:
    return run(["git", *args], cwd=cwd, capture=True).stdout.strip()


def ensure_clean(root: Path) -> None:
    status = git_output(["status", "--porcelain"], cwd=root)
    if status:
        raise SystemExit(
            "Working tree is dirty. Commit the runtime change before publishing "
            "a dev release, or pass --skip-clean-check when tagging an explicit "
            "target ref intentionally."
        )


def ensure_tag_available(root: Path, *, remote: str, tag: str) -> None:
    local = subprocess.run(  # noqa: S603,S607 - fixed git invocation.
        ["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if local.returncode == 0:
        raise SystemExit(f"Local tag already exists: {tag}")

    remote_result = run(
        ["git", "ls-remote", "--tags", remote, f"refs/tags/{tag}"],
        cwd=root,
        capture=True,
    )
    if remote_result.stdout.strip():
        raise SystemExit(f"Remote tag already exists on {remote}: {tag}")


def print_plan(*, tag: str, target: str, remote: str, repo: str) -> None:
    print(f"release_ref={tag}")
    print(f"target={target}")
    print(f"remote={remote}")
    print(f"repo={repo}")
    print("installer_command:")
    print(installer_command(tag))


def publish(
    *,
    root: Path,
    tag: str,
    target: str,
    remote: str,
    repo: str,
    notes: str,
    skip_clean_check: bool,
) -> None:
    if not skip_clean_check:
        ensure_clean(root)
    ensure_tag_available(root, remote=remote, tag=tag)

    run(["git", "tag", "-a", tag, target, "-m", f"Dev runtime release {tag}"], cwd=root)
    run(["git", "push", remote, f"refs/tags/{tag}"], cwd=root)
    run(
        [
            "gh",
            "release",
            "create",
            tag,
            "--repo",
            repo,
            "--title",
            tag,
            "--prerelease",
            "--latest=false",
            "--verify-tag",
            "--notes",
            notes,
        ],
        cwd=root,
    )
    print("published=true")


def parse_args() -> argparse.Namespace:
    root = repo_root()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        default=version_from_file(root),
        help="Base version for the dev tag, shaped vX.Y.Z. Defaults to VERSION.",
    )
    parser.add_argument(
        "--suffix",
        default="dev",
        help="Short reason suffix, for example smoke or heartbeat.",
    )
    parser.add_argument(
        "--target",
        default="HEAD",
        help="Git ref to tag. Defaults to HEAD, including PR branch HEAD.",
    )
    parser.add_argument(
        "--remote",
        default="origin",
        help="Git remote to push the tag to. Defaults to origin.",
    )
    parser.add_argument(
        "--repo",
        default=REPO,
        help="GitHub repo for the prerelease.",
    )
    parser.add_argument(
        "--notes",
        help="Release notes. Defaults to a public-safe dev testing note.",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Actually tag, push, and create the GitHub prerelease.",
    )
    parser.add_argument(
        "--skip-clean-check",
        action="store_true",
        help="Allow tagging while the working tree is dirty.",
    )
    return parser.parse_args()


def main() -> None:
    root = repo_root()
    args = parse_args()
    tag = make_tag(base=args.base, suffix=args.suffix)
    notes = args.notes or default_notes(tag=tag, target=args.target)

    print_plan(tag=tag, target=args.target, remote=args.remote, repo=args.repo)
    if not args.publish:
        print("dry_run=true")
        print(
            "publish_command="
            + shlex.join(
                [
                    "python3",
                    "scripts/publish_dev_release.py",
                    "--base",
                    normalize_base(args.base),
                    "--suffix",
                    clean_suffix(args.suffix),
                    "--target",
                    args.target,
                    "--remote",
                    args.remote,
                    "--repo",
                    args.repo,
                    "--publish",
                ]
            )
        )
        return

    publish(
        root=root,
        tag=tag,
        target=args.target,
        remote=args.remote,
        repo=args.repo,
        notes=notes,
        skip_clean_check=args.skip_clean_check,
    )


if __name__ == "__main__":
    main()
