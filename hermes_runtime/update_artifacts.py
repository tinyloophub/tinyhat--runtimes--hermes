"""Prepare and activate Hermes runtime code updates."""

from __future__ import annotations

import os
import re
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Any
from urllib import parse, request


REPO_SLUG = "tinyloophub/tinyhat--runtimes--hermes"
BOOTSTRAP_FILENAME = "tinyhat_hermes_runtime_bootstrap.py"
_SAFE_REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_SAFE_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def install_prefix() -> Path:
    configured = (os.getenv("TINYHAT_RUNTIME_PREFIX") or "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[1]


def installed_package_dir() -> Path:
    return install_prefix() / "hermes_runtime"


def staged_runtime_dir(state_dir: Path) -> Path:
    return state_dir / "staged" / "runtime"


def staged_package_dir(state_dir: Path) -> Path:
    return staged_runtime_dir(state_dir) / "hermes_runtime"


def staged_bootstrap_file(state_dir: Path) -> Path:
    return staged_runtime_dir(state_dir) / BOOTSTRAP_FILENAME


def installed_bootstrap_file() -> Path:
    return install_prefix() / BOOTSTRAP_FILENAME


def _validate_download_ref(value: str, *, field: str) -> str:
    clean = value.strip()
    if not clean:
        raise ValueError(f"{field} is required")
    if "\\" in clean or clean.startswith("/") or ".." in Path(clean).parts:
        raise ValueError(f"{field} contains an unsafe path segment")
    if not _SAFE_REF_RE.fullmatch(clean):
        raise ValueError(f"{field} contains unsupported characters")
    return clean


def _validate_target_sha(value: str | None) -> str | None:
    if value is None:
        return None
    clean = value.strip()
    if not clean:
        return None
    if not _SAFE_SHA_RE.fullmatch(clean):
        raise ValueError("target_sha must be a git commit sha")
    return clean


def _copy_package(source_root: Path, destination: Path) -> None:
    package = source_root / "hermes_runtime"
    if not package.is_dir():
        raise ValueError(f"hermes_runtime package not found in {source_root}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(package, destination)


def _copy_bootstrap(source_root: Path, destination: Path) -> bool:
    bootstrap = source_root / BOOTSTRAP_FILENAME
    if not bootstrap.is_file():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(bootstrap, destination)
    return True


def _safe_extract_tarball(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            member_path = Path(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise ValueError(f"unsafe tarball path: {member.name}")
            parts = member_path.parts
            if len(parts) <= 1:
                continue
            relative = Path(*parts[1:])
            target = destination / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                continue
            with source, target.open("wb") as out:
                shutil.copyfileobj(source, out)


def _download_source_ref(target_ref: str, destination: Path) -> None:
    encoded_ref = parse.quote(target_ref, safe="/")
    url = f"https://codeload.github.com/{REPO_SLUG}/tar.gz/{encoded_ref}"
    with tempfile.TemporaryDirectory(prefix="tinyhat-runtime-download-") as tmp:
        archive_path = Path(tmp) / "runtime.tar.gz"
        with request.urlopen(url, timeout=60) as response:
            archive_path.write_bytes(response.read())
        _safe_extract_tarball(archive_path, destination)


def prepare_staged_runtime(
    *,
    state_dir: Path,
    target_ref: str,
    target_sha: str | None = None,
) -> dict[str, Any]:
    """Stage the target runtime package without touching the running package."""
    target_ref = _validate_download_ref(target_ref, field="target_ref")
    target_sha = _validate_target_sha(target_sha)
    staging_dir = staged_runtime_dir(state_dir)
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)

    source_override = (os.getenv("TINYHAT_RUNTIME_UPDATE_SOURCE_DIR") or "").strip()
    if source_override:
        source_root = Path(source_override)
        _copy_package(source_root, staged_package_dir(state_dir))
        bootstrap_staged = _copy_bootstrap(source_root, staged_bootstrap_file(state_dir))
        source = {"kind": "local_source", "path": str(source_root)}
    else:
        download_ref = target_sha or target_ref
        _validate_download_ref(download_ref, field="download_ref")
        source_root = staging_dir / "source"
        _download_source_ref(download_ref, source_root)
        _copy_package(source_root, staged_package_dir(state_dir))
        bootstrap_staged = _copy_bootstrap(source_root, staged_bootstrap_file(state_dir))
        source = {
            "kind": "github_tarball",
            "repo": REPO_SLUG,
            "ref": target_ref,
            "download_ref": download_ref,
            "target_sha": target_sha,
        }

    return {
        "code_staged": True,
        "package_dir": str(staged_package_dir(state_dir)),
        "bootstrap_staged": bootstrap_staged,
        "bootstrap_file": (
            str(staged_bootstrap_file(state_dir)) if bootstrap_staged else None
        ),
        "source": source,
    }


def _recover_interrupted_package_swap(prefix: Path) -> None:
    target_package = prefix / "hermes_runtime"
    next_package = prefix / "hermes_runtime.next"
    previous_package = prefix / "hermes_runtime.previous"

    if target_package.exists():
        shutil.rmtree(next_package, ignore_errors=True)
        shutil.rmtree(previous_package, ignore_errors=True)
        return

    if next_package.exists():
        next_package.rename(target_package)
        shutil.rmtree(previous_package, ignore_errors=True)
        return

    if previous_package.exists():
        previous_package.rename(target_package)


def activate_staged_runtime_code(*, state_dir: Path) -> bool:
    """Swap staged package code into the install prefix if staged code exists."""
    prefix = install_prefix()
    _recover_interrupted_package_swap(prefix)

    staged_package = staged_package_dir(state_dir)
    if not staged_package.is_dir():
        return False

    target_package = installed_package_dir()
    next_package = prefix / "hermes_runtime.next"
    previous_package = prefix / "hermes_runtime.previous"

    if next_package.exists():
        shutil.rmtree(next_package)
    shutil.copytree(staged_package, next_package)

    staged_bootstrap = staged_bootstrap_file(state_dir)
    if staged_bootstrap.is_file():
        bootstrap_next = prefix / f"{BOOTSTRAP_FILENAME}.next"
        shutil.copy2(staged_bootstrap, bootstrap_next)

    if previous_package.exists():
        shutil.rmtree(previous_package)
    if target_package.exists():
        target_package.rename(previous_package)
    next_package.rename(target_package)
    shutil.rmtree(previous_package, ignore_errors=True)
    if staged_bootstrap.is_file():
        os.replace(prefix / f"{BOOTSTRAP_FILENAME}.next", installed_bootstrap_file())
    return True
