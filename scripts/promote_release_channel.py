#!/usr/bin/env python3
"""Promote a final runtime release to maintainer-owned channel refs."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path


REPO = "tinyloophub/tinyhat--runtimes--hermes"
DEFAULT_MAINTAINER = "farid-tinyloop"
FINAL_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")
CHANNEL_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def run(
    args: list[str],
    *,
    cwd: Path,
    capture: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - argv list, caller controls command.
        args,
        cwd=cwd,
        check=True,
        text=True,
        capture_output=capture,
        env=env,
    )


def output(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    return run(args, cwd=cwd, capture=True, env=env).stdout.strip()


def normalize_final_tag(value: str) -> str:
    tag = value.strip()
    if re.fullmatch(r"\d+\.\d+\.\d+", tag):
        tag = f"v{tag}"
    if not FINAL_TAG_RE.fullmatch(tag):
        raise SystemExit("release tag must be a final vX.Y.Z tag")
    return tag


def parse_channels(values: list[str]) -> list[str]:
    channels: list[str] = []
    for value in values:
        for raw in value.split(","):
            channel = raw.strip()
            if not channel:
                continue
            if not CHANNEL_RE.fullmatch(channel):
                raise SystemExit(f"invalid channel name: {channel}")
            if channel not in channels:
                channels.append(channel)
    if not channels:
        raise SystemExit("at least one --channel is required")
    return channels


def channel_ref(channel: str) -> str:
    if not CHANNEL_RE.fullmatch(channel):
        raise SystemExit(f"invalid channel name: {channel}")
    return f"heads/channels/{channel}"


def assert_maintainer_actor(*, expected_actor: str, root: Path, env: dict[str, str]) -> None:
    actor = output(["gh", "api", "user", "--jq", ".login"], cwd=root, env=env)
    if actor != expected_actor:
        raise SystemExit(
            f"refusing promotion as {actor!r}; authenticate gh as {expected_actor!r}"
        )


def release_metadata(*, repo: str, tag: str, root: Path, env: dict[str, str]) -> dict[str, object]:
    raw = output(
        [
            "gh",
            "release",
            "view",
            tag,
            "--repo",
            repo,
            "--json",
            "tagName,name,isPrerelease,isDraft",
        ],
        cwd=root,
        env=env,
    )
    return json.loads(raw)


def assert_final_release(*, repo: str, tag: str, root: Path, env: dict[str, str]) -> None:
    metadata = release_metadata(repo=repo, tag=tag, root=root, env=env)
    if metadata.get("tagName") != tag or metadata.get("name") != tag:
        raise SystemExit(f"release title/tag mismatch for {tag}")
    if metadata.get("isDraft"):
        raise SystemExit(f"release {tag} is still a draft")
    if metadata.get("isPrerelease"):
        raise SystemExit(f"release {tag} is a prerelease; channels require a final release")


def resolve_tag_commit(*, tag: str, root: Path) -> str:
    run(["git", "fetch", "origin", "main", "--tags"], cwd=root)
    return output(["git", "rev-parse", f"{tag}^{{commit}}"], cwd=root)


def assert_on_main(*, sha: str, root: Path) -> None:
    run(["git", "fetch", "origin", "main"], cwd=root)
    result = subprocess.run(  # noqa: S603 - fixed git command.
        ["git", "merge-base", "--is-ancestor", sha, "origin/main"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"release commit {sha} is not contained in origin/main")


def update_channel_ref(
    *,
    repo: str,
    channel: str,
    sha: str,
    root: Path,
    env: dict[str, str],
    dry_run: bool,
) -> None:
    ref = channel_ref(channel)
    if dry_run:
        print(f"dry_run: would update {ref} -> {sha}")
        return
    run(
        [
            "gh",
            "api",
            "-X",
            "PATCH",
            f"repos/{repo}/git/refs/{ref}",
            "-f",
            f"sha={sha}",
            "-F",
            "force=true",
        ],
        cwd=root,
        env=env,
        capture=True,
    )
    current = output(
        ["gh", "api", f"repos/{repo}/git/refs/{ref}", "--jq", ".object.sha"],
        cwd=root,
        env=env,
    )
    if current != sha:
        raise SystemExit(f"{ref} points at {current}, expected {sha}")
    print(f"promoted {ref} -> {sha}")


def edit_latest_marker(
    *,
    repo: str,
    tag: str,
    root: Path,
    env: dict[str, str],
    dry_run: bool,
) -> None:
    if dry_run:
        print(f"dry_run: would mark {tag} as GitHub Latest")
        return
    run(
        [
            "gh",
            "release",
            "edit",
            tag,
            "--repo",
            repo,
            "--latest",
            "--prerelease=false",
            "--draft=false",
        ],
        cwd=root,
        env=env,
        capture=True,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="Final release tag, shaped vX.Y.Z.")
    parser.add_argument(
        "--channel",
        action="append",
        default=[],
        help="Channel to move, for example latest or lts. May be repeated or comma-separated.",
    )
    parser.add_argument("--repo", default=REPO, help="GitHub repo to update.")
    parser.add_argument(
        "--expected-actor",
        default=DEFAULT_MAINTAINER,
        help="Required gh authenticated user. Defaults to farid-tinyloop.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print planned writes only.")
    return parser.parse_args()


def main() -> None:
    root = repo_root()
    args = parse_args()
    tag = normalize_final_tag(args.tag)
    channels = parse_channels(args.channel)
    env = os.environ.copy()

    assert_maintainer_actor(expected_actor=args.expected_actor, root=root, env=env)
    assert_final_release(repo=args.repo, tag=tag, root=root, env=env)
    sha = resolve_tag_commit(tag=tag, root=root)
    assert_on_main(sha=sha, root=root)

    if "latest" in channels:
        edit_latest_marker(repo=args.repo, tag=tag, root=root, env=env, dry_run=args.dry_run)

    for channel in channels:
        update_channel_ref(
            repo=args.repo,
            channel=channel,
            sha=sha,
            root=root,
            env=env,
            dry_run=args.dry_run,
        )


if __name__ == "__main__":
    main()
