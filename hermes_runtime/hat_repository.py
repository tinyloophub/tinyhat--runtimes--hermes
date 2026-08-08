"""Computer-local Git workflow for one explicitly granted Hat repository."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator
from urllib.parse import quote, urlsplit

from hermes_runtime.platform_context import StandalonePlatformContext
from hermes_runtime.runtime_env import hermes_home

SCHEMA = "tinyhat_hat_repository_v1"
_HANDLE_PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,126}$")
_PROVIDER_PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$")
_GRANT_ID_RE = re.compile(r"^rgr_[A-Za-z0-9_-]{16,56}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
_BLOCKED_NAMES = {
    ".env",
    ".env.local",
    ".npmrc",
    ".pypirc",
    "credentials.json",
    "id_rsa",
    "id_ed25519",
}
_BLOCKED_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
_CONFIG_HANDLE = "tinyhat.hatHandle"
_CONFIG_GRANT_ID = "tinyhat.repositoryGrantId"
_CONFIG_OWNER = "tinyhat.repositoryOwner"
_CONFIG_REPO = "tinyhat.repositoryName"
_CONFIG_BRANCH = "tinyhat.repositoryBranch"
_CONFIG_PENDING_HEAD = "tinyhat.pendingSyncHead"
_CONFIG_PENDING_BASE = "tinyhat.pendingSyncBase"
_CONFIG_PENDING_BRANCH = "tinyhat.pendingSyncBranch"
_DELETION_JOURNAL_SCHEMA = "tinyhat_hat_deletion_journal_v1"
_CLONE_RETRY_DELAYS_SECONDS = (1.0, 2.0, 4.0, 8.0, 15.0)


class HatRepositoryError(RuntimeError):
    """One safe, user-actionable local repository error."""


def _required_text(
    payload: dict[str, Any], key: str, *, max_length: int
) -> str:
    value = str(payload.get(key) or "").strip()
    if not value or len(value) > max_length:
        raise HatRepositoryError(f"{key} is required and must be valid.")
    return value


def _repository_root() -> Path:
    return hermes_home() / "hat-repositories"


def _acquire_checkout_lock(target: Path) -> int:
    """Acquire one process-wide mutation lock for a canonical checkout."""

    root = _repository_root().expanduser().resolve()
    try:
        relative = target.resolve().relative_to(root)
    except ValueError as exc:
        raise HatRepositoryError("The Hat checkout lock path is unsafe.") from exc
    lock_directory = root / ".locks"
    lock_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if lock_directory.is_symlink() or lock_directory.resolve() != lock_directory:
        raise HatRepositoryError("The Hat checkout lock directory is unsafe.")
    os.chmod(lock_directory, 0o700)
    lock_name = hashlib.sha256(relative.as_posix().encode("utf-8")).hexdigest()
    flags = os.O_CREAT | os.O_RDWR
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_directory / f"{lock_name}.lock", flags, 0o600)
    except OSError as exc:
        raise HatRepositoryError("The Hat checkout lock could not be opened.") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise HatRepositoryError("The Hat checkout lock file is unsafe.")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _release_checkout_lock(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


@asynccontextmanager
async def _checkout_mutation_lock(target: Path) -> AsyncIterator[None]:
    descriptor = await asyncio.to_thread(_acquire_checkout_lock, target)
    try:
        yield
    finally:
        await asyncio.to_thread(_release_checkout_lock, descriptor)


def _canonical_handle(value: Any) -> tuple[str, str, str]:
    handle = str(value or "").strip()
    parts = handle.split("/")
    if (
        len(handle) > 255
        or len(parts) != 3
        or parts[1] != "hats"
        or any(_HANDLE_PART_RE.fullmatch(part) is None for part in (parts[0], parts[2]))
    ):
        raise HatRepositoryError("The platform returned an invalid Hat handle.")
    return handle, parts[0], parts[2]


def _repository_payload(payload: dict[str, Any]) -> tuple[str, str, str, str]:
    repository = payload.get("repository")
    if not isinstance(repository, dict):
        raise HatRepositoryError("The platform returned no Hat repository.")
    owner = str(repository.get("owner") or "").strip()
    name = str(repository.get("name") or "").strip()
    branch = str(repository.get("default_branch") or "main").strip()
    url = str(repository.get("url") or "").strip()
    if (
        _PROVIDER_PART_RE.fullmatch(owner) is None
        or _PROVIDER_PART_RE.fullmatch(name) is None
        or _BRANCH_RE.fullmatch(branch) is None
    ):
        raise HatRepositoryError("The platform returned invalid repository metadata.")
    parsed = urlsplit(url)
    expected_path = f"/{owner}/{name}.git"
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.casefold() != expected_path.casefold()
    ):
        raise HatRepositoryError("The platform returned an unsafe repository URL.")
    return owner, name, branch, url


def _deletion_repository_payload(payload: dict[str, Any]) -> tuple[str, str, str]:
    """Return the control-plane identity required for destructive cleanup."""

    repository = payload.get("repository")
    if not isinstance(repository, dict) or set(repository) != {
        "owner",
        "name",
        "url",
    }:
        raise HatRepositoryError(
            "Trusted repository metadata is required to delete a Hat checkout."
        )
    owner = str(repository.get("owner") or "").strip()
    name = str(repository.get("name") or "").strip()
    url = str(repository.get("url") or "").strip()
    if (
        _PROVIDER_PART_RE.fullmatch(owner) is None
        or _PROVIDER_PART_RE.fullmatch(name) is None
    ):
        raise HatRepositoryError("The platform returned invalid repository metadata.")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.casefold() != f"/{owner}/{name}.git".casefold()
    ):
        raise HatRepositoryError("The platform returned an unsafe repository URL.")
    return owner, name, url


def _grant_id(payload: dict[str, Any]) -> str:
    value = str(payload.get("grant_id") or "").strip()
    if _GRANT_ID_RE.fullmatch(value) is None:
        raise HatRepositoryError("The platform returned an invalid repository grant.")
    return value


def _checkout_path(handle: str) -> Path:
    _, namespace, key = _canonical_handle(handle)
    root = _repository_root().expanduser().resolve()
    target = (root / namespace / key).resolve()
    if target != root and root not in target.parents:
        raise HatRepositoryError("The Hat checkout path is unsafe.")
    return target


def _deletion_checkout_path(handle: str) -> Path:
    """Return the lexical checkout path so symlink components stay detectable."""

    _, namespace, key = _canonical_handle(handle)
    root = _repository_root().expanduser().resolve()
    namespace_path = root / namespace
    target = namespace_path / key
    if namespace_path.is_symlink() or target.is_symlink():
        raise HatRepositoryError("The local Hat checkout is unsafe to delete.")
    return target


def _existing_checkout(identifier: str) -> Path:
    """Resolve a local clone without asking the platform to create a grant."""

    clean = str(identifier or "").strip()
    if "/" in clean:
        target = _checkout_path(clean)
        candidates = [target]
    else:
        if _HANDLE_PART_RE.fullmatch(clean) is None:
            raise HatRepositoryError("The Hat identifier is invalid.")
        root = _repository_root().expanduser().resolve()
        candidates = [
            candidate
            for candidate in root.glob(f"*/{clean}")
            if candidate.is_dir()
        ]
    repositories = [path for path in candidates if (path / ".git").is_dir()]
    if not repositories:
        raise HatRepositoryError("Check out this Hat repository first.")
    if len(repositories) != 1:
        raise HatRepositoryError(
            "Use the full Hat handle because this local repository name is ambiguous."
        )
    return repositories[0]


def _local_config(path: Path, key: str) -> str:
    result = _run_git(["config", "--local", "--get", key], cwd=path, check=False)
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        raise HatRepositoryError(
            "The local Hat checkout is missing Tinyhat repository metadata."
        )
    return value


def _optional_local_config(path: Path, key: str) -> str | None:
    result = _run_git(["config", "--local", "--get", key], cwd=path, check=False)
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _local_grant_metadata(path: Path) -> tuple[str, str, str, str]:
    handle, _, _ = _canonical_handle(_local_config(path, _CONFIG_HANDLE))
    grant_id = _grant_id({"grant_id": _local_config(path, _CONFIG_GRANT_ID)})
    owner = _local_config(path, _CONFIG_OWNER)
    repo = _local_config(path, _CONFIG_REPO)
    if (
        _PROVIDER_PART_RE.fullmatch(owner) is None
        or _PROVIDER_PART_RE.fullmatch(repo) is None
    ):
        raise HatRepositoryError("The local Hat repository metadata is invalid.")
    return handle, grant_id, owner, repo


def _credential_helper_command(*, grant_id: str, owner: str, repo: str) -> str:
    runtime_root = Path(__file__).resolve().parents[1]
    python = Path(sys.executable).resolve()
    python_path = shlex.quote(str(runtime_root))
    return (
        "!PYTHONPATH="
        f"{python_path}${{PYTHONPATH:+:$PYTHONPATH}} "
        f"{shlex.quote(str(python))} -m "
        "hermes_runtime.github_credential_helper "
        f"--grant-id {shlex.quote(grant_id)} "
        f"--owner {shlex.quote(owner)} --repo {shlex.quote(repo)}"
    )


def _credential_key(owner: str, repo: str) -> str:
    # Git includes the literal ``.git`` suffix from the remote URL in the
    # credential context path.  The URL-scoped helper must match that exact
    # context when ``credential.useHttpPath`` is enabled, otherwise Git skips
    # the helper and falls back to an interactive username prompt.
    return f"credential.https://github.com/{owner}/{repo}.git.helper"


def _run_git(
    args: list[str],
    *,
    cwd: Path | None = None,
    extra_config: list[tuple[str, str]] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git")
    if not git:
        raise HatRepositoryError("Git is not installed on this Computer.")
    command = [git]
    for key, value in extra_config or []:
        command.extend(["-c", f"{key}={value}"])
    command.extend(args)
    env = dict(os.environ)
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
        }
    )
    completed = subprocess.run(
        command,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if check and completed.returncode != 0:
        raise HatRepositoryError(
            "Git could not complete the repository operation. The checkout was "
            "left intact for inspection."
        )
    return completed


def _configure_checkout(
    path: Path,
    *,
    handle: str,
    grant_id: str,
    owner: str,
    repo: str,
    branch: str,
) -> None:
    helper = _credential_helper_command(
        grant_id=grant_id,
        owner=owner,
        repo=repo,
    )
    _disable_checkout_credentials(path, owner=owner, repo=repo)
    _run_git(
        [
            "config",
            "--local",
            "--add",
            _credential_key(owner, repo),
            helper,
        ],
        cwd=path,
    )
    _configure_checkout_identity(
        path,
        handle=handle,
        grant_id=grant_id,
        owner=owner,
        repo=repo,
        branch=branch,
    )


def _disable_checkout_credentials(path: Path, *, owner: str, repo: str) -> None:
    """Shadow every inherited helper and leave this repository fail-closed."""

    _run_git(["config", "--local", "credential.useHttpPath", "true"], cwd=path)
    # An empty helper resets Git's accumulated system/global helper list. The
    # exact URL sentinel does the same after URL matching and remains in place
    # after reset, so Git cannot fall through to a broad GitHub credential.
    _run_git(
        ["config", "--local", "--unset-all", "credential.helper"],
        cwd=path,
        check=False,
    )
    _run_git(
        ["config", "--local", "--add", "credential.helper", ""],
        cwd=path,
    )
    _run_git(
        ["config", "--local", "--unset-all", _credential_key(owner, repo)],
        cwd=path,
        check=False,
    )
    _run_git(
        [
            "config",
            "--local",
            "--add",
            _credential_key(owner, repo),
            "",
        ],
        cwd=path,
    )


def _configure_checkout_identity(
    path: Path,
    *,
    handle: str,
    grant_id: str,
    owner: str,
    repo: str,
    branch: str,
) -> None:
    _run_git(["config", "--local", "user.name", "Tinyhat Agent"], cwd=path)
    _run_git(
        ["config", "--local", "user.email", "agent@tinyhat.ai"], cwd=path
    )
    for key, value in (
        (_CONFIG_HANDLE, handle),
        (_CONFIG_GRANT_ID, grant_id),
        (_CONFIG_OWNER, owner),
        (_CONFIG_REPO, repo),
        (_CONFIG_BRANCH, branch),
    ):
        _run_git(["config", "--local", key, value], cwd=path)


def _set_pending_sync(path: Path, *, head: str, base: str, branch: str) -> None:
    for key, value in (
        (_CONFIG_PENDING_HEAD, head),
        (_CONFIG_PENDING_BASE, base),
        (_CONFIG_PENDING_BRANCH, branch),
    ):
        _run_git(["config", "--local", key, value], cwd=path)


def _clear_pending_sync(path: Path) -> None:
    for key in (
        _CONFIG_PENDING_HEAD,
        _CONFIG_PENDING_BASE,
        _CONFIG_PENDING_BRANCH,
    ):
        _run_git(
            ["config", "--local", "--unset-all", key],
            cwd=path,
            check=False,
        )


def _pending_sync(path: Path) -> tuple[str, str, str] | None:
    values = tuple(
        _optional_local_config(path, key)
        for key in (
            _CONFIG_PENDING_HEAD,
            _CONFIG_PENDING_BASE,
            _CONFIG_PENDING_BRANCH,
        )
    )
    if values == (None, None, None):
        return None
    if any(value is None for value in values):
        raise HatRepositoryError(
            "The local Hat checkout has incomplete pending-sync metadata."
        )
    head, base, branch = values
    if (
        _SHA_RE.fullmatch(head or "") is None
        or _SHA_RE.fullmatch(base or "") is None
        or _BRANCH_RE.fullmatch(branch or "") is None
    ):
        raise HatRepositoryError(
            "The local Hat checkout has invalid pending-sync metadata."
        )
    return str(head), str(base), str(branch)


def _clone_repository(
    *,
    target: Path,
    url: str,
    branch: str,
    owner: str,
    repo: str,
    helper: str,
) -> None:
    """Clone once GitHub's renamed-repository access has propagated."""

    staging_name = hashlib.sha256(str(target).encode("utf-8")).hexdigest()[:24]
    temporary = target.parent / f".tinyhat-clone-{staging_name}"

    def remove_failed_clone(path: Path) -> None:
        try:
            shutil.rmtree(path)
        except OSError as exc:
            raise HatRepositoryError(
                "A failed Hat clone could not be cleaned up. The repository "
                "operation stopped for Computer repair."
            ) from exc
        if path.exists():
            raise HatRepositoryError(
                "A failed Hat clone could not be cleaned up. The repository "
                "operation stopped for Computer repair."
            )

    # The staging path is deterministic for this checkout and every caller holds
    # its cross-process mutation lock.  Reconcile a clone left by an earlier
    # invocation before starting new network work so private data cannot become
    # unreachable under a random temporary name.
    if temporary.exists() or temporary.is_symlink():
        remove_failed_clone(temporary)

    delays: tuple[float | None, ...] = (*_CLONE_RETRY_DELAYS_SECONDS, None)
    for delay in delays:
        temporary.mkdir(mode=0o700)
        try:
            _run_git(
                ["clone", "--branch", branch, "--single-branch", url, str(temporary)],
                extra_config=[
                    ("credential.useHttpPath", "true"),
                    ("credential.helper", ""),
                    (_credential_key(owner, repo), ""),
                    (_credential_key(owner, repo), helper),
                ],
            )
            temporary.replace(target)
            return
        except HatRepositoryError as clone_error:
            try:
                remove_failed_clone(temporary)
            except HatRepositoryError as cleanup_error:
                raise cleanup_error from clone_error
            if delay is None:
                raise
            time.sleep(delay)
        except Exception as clone_error:
            try:
                remove_failed_clone(temporary)
            except HatRepositoryError as cleanup_error:
                raise cleanup_error from clone_error
            raise


def _clone_or_refresh(
    *,
    target: Path,
    url: str,
    branch: str,
    handle: str,
    grant_id: str,
    owner: str,
    repo: str,
) -> bool:
    target.parent.mkdir(parents=True, exist_ok=True)
    helper = _credential_helper_command(
        grant_id=grant_id,
        owner=owner,
        repo=repo,
    )
    created = False
    if not target.exists():
        _clone_repository(
            target=target,
            url=url,
            branch=branch,
            owner=owner,
            repo=repo,
            helper=helper,
        )
        created = True
    if not (target / ".git").is_dir():
        raise HatRepositoryError("The Hat checkout path is not a Git repository.")
    remote = _run_git(["remote", "get-url", "origin"], cwd=target).stdout.strip()
    if remote != url:
        raise HatRepositoryError("The local Hat checkout points at another repository.")
    _configure_checkout(
        target,
        handle=handle,
        grant_id=grant_id,
        owner=owner,
        repo=repo,
        branch=branch,
    )
    _run_git(["fetch", "--prune", "origin"], cwd=target)
    current_branch = _run_git(
        ["branch", "--show-current"], cwd=target
    ).stdout.strip()
    if current_branch != branch:
        raise HatRepositoryError(
            "The local Hat checkout is on a different branch. Review it before syncing."
        )
    if not _status_paths(target):
        _run_git(["merge", "--ff-only", f"origin/{branch}"], cwd=target)
    return created


def _status_paths(path: Path) -> list[str]:
    raw = _run_git(
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=path
    ).stdout
    entries = raw.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        if len(entry) < 4:
            raise HatRepositoryError("Git returned invalid repository status output.")
        status = entry[:2]
        paths.append(entry[3:])
        # With -z, rename/copy entries put the destination in the first record
        # and the original path in the following NUL-delimited record.
        if "R" in status or "C" in status:
            index += 1
    return paths


def _safe_sync_path(value: Any, *, checkout: Path) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    candidate = PurePosixPath(raw)
    parts = candidate.parts
    lowered = [part.casefold() for part in parts]
    filename = lowered[-1] if lowered else ""
    if (
        not raw
        or len(raw) > 240
        or candidate.is_absolute()
        or ".." in parts
        or ".git" in lowered
        or filename in _BLOCKED_NAMES
        or filename.endswith(_BLOCKED_SUFFIXES)
        or "secret" in filename
        or "credential" in filename
    ):
        raise HatRepositoryError(
            f"Refusing to sync unsafe or credential-shaped path: {raw or '<empty>'}."
        )
    local = checkout.joinpath(*parts)
    parent = local.parent.resolve()
    checkout_resolved = checkout.resolve()
    if parent != checkout_resolved and checkout_resolved not in parent.parents:
        raise HatRepositoryError("A requested sync path escapes the Hat checkout.")
    if local.is_symlink():
        raise HatRepositoryError("Symlink paths cannot be synced through Tinyhat.")
    return candidate.as_posix()


async def _prepare(
    context: StandalonePlatformContext, identifier: str
) -> dict[str, Any]:
    return await context.client.post_json(
        context.computer_path("hats/v1/repository-access"),
        {"identifier": identifier},
    )


async def _checkout(
    context: StandalonePlatformContext, identifier: str
) -> dict[str, Any]:
    prepared = await _prepare(context, identifier)
    handle, _, _ = _canonical_handle(prepared.get("hat_handle"))
    grant_id = _grant_id(prepared)
    owner, repo, branch, url = _repository_payload(prepared)
    target = _checkout_path(handle)
    async with _checkout_mutation_lock(target):
        created = await asyncio.to_thread(
            _clone_or_refresh,
            target=target,
            url=url,
            branch=branch,
            handle=handle,
            grant_id=grant_id,
            owner=owner,
            repo=repo,
        )
        head = (
            await asyncio.to_thread(_run_git, ["rev-parse", "HEAD"], cwd=target)
        ).stdout.strip()
    return {
        "schema": SCHEMA,
        "action": "checkout",
        "hat_handle": handle,
        "repository": {"owner": owner, "name": repo},
        "path": str(target),
        "branch": branch,
        "head_sha": head,
        "created": created,
        "credential_persisted": False,
    }


async def _status(identifier: str) -> dict[str, Any]:
    # Status is deliberately local-only. A read must never reactivate a grant
    # that the user previously reset.
    target = _existing_checkout(identifier)
    handle, _, owner, repo = _local_grant_metadata(target)
    changed = await asyncio.to_thread(_status_paths, target)
    head = (
        await asyncio.to_thread(_run_git, ["rev-parse", "HEAD"], cwd=target)
    ).stdout.strip()
    return {
        "schema": SCHEMA,
        "action": "status",
        "hat_handle": handle,
        "repository": {"owner": owner, "name": repo},
        "path": str(target),
        "head_sha": head,
        "changed_paths": changed,
        "clean": not changed,
    }


async def _record_synced_head(
    context: StandalonePlatformContext, *, grant_id: str, head: str
) -> None:
    recorded = await context.client.post_json(
        context.computer_path(
            f"repository-grants/{quote(grant_id, safe='')}/sync"
        ),
        {"head_sha": head},
    )
    if recorded.get("verified") is not True:
        raise HatRepositoryError("Tinyhat could not verify the pushed Hat commit.")


async def _resume_pending_sync(
    context: StandalonePlatformContext,
    *,
    target: Path,
    handle: str,
    grant_id: str,
    owner: str,
    repo: str,
    branch: str,
) -> dict[str, Any] | None:
    pending = await asyncio.to_thread(_pending_sync, target)
    if pending is None:
        return None
    pending_head, pending_base, pending_branch = pending
    if pending_branch != branch:
        raise HatRepositoryError(
            "The pending Hat sync belongs to another branch. Review the checkout."
        )
    current_head = await asyncio.to_thread(
        _run_git,
        ["rev-parse", "HEAD"],
        cwd=target,
        check=False,
    )
    current_parent = await asyncio.to_thread(
        _run_git,
        ["rev-parse", "HEAD^"],
        cwd=target,
        check=False,
    )
    if (
        current_head.returncode != 0
        or current_parent.returncode != 0
        or current_head.stdout.strip().casefold() != pending_head
        or current_parent.stdout.strip().casefold() != pending_base
    ):
        raise HatRepositoryError(
            "The local Hat checkout changed after a pending sync. Review it before retrying."
        )
    fetched = await asyncio.to_thread(
        _run_git,
        ["fetch", "--prune", "origin"],
        cwd=target,
        check=False,
    )
    if fetched.returncode != 0:
        raise HatRepositoryError(
            "The pending Hat sync could not reach the remote. Retry when access is available."
        )
    remote = await asyncio.to_thread(
        _run_git,
        ["rev-parse", f"origin/{branch}"],
        cwd=target,
        check=False,
    )
    remote_head = remote.stdout.strip().casefold()
    if remote.returncode != 0:
        raise HatRepositoryError(
            "The pending Hat sync could not read the remote branch."
        )
    if remote_head == pending_base:
        pushed = await asyncio.to_thread(
            _run_git,
            ["push", "origin", f"HEAD:refs/heads/{branch}"],
            cwd=target,
            check=False,
        )
        if pushed.returncode != 0:
            raise HatRepositoryError(
                "The pending Hat sync could not be pushed. Retry when access is available."
            )
    elif remote_head != pending_head:
        raise HatRepositoryError(
            "The remote Hat changed while a sync was pending. Review both versions."
        )
    changed = await asyncio.to_thread(
        _run_git,
        ["diff", "--name-only", "-z", pending_base, pending_head, "--"],
        cwd=target,
    )
    synced_paths = [item for item in changed.stdout.split("\0") if item]
    await _record_synced_head(context, grant_id=grant_id, head=pending_head)
    await asyncio.to_thread(_clear_pending_sync, target)
    return {
        "schema": SCHEMA,
        "action": "sync",
        "hat_handle": handle,
        "repository": {"owner": owner, "name": repo},
        "path": str(target),
        "head_sha": pending_head,
        "changed": True,
        "pushed": True,
        "synced_paths": synced_paths,
        "credential_persisted": False,
    }


async def _sync(
    context: StandalonePlatformContext,
    identifier: str,
    *,
    raw_paths: Any,
    message: str,
) -> dict[str, Any]:
    prepared = await _prepare(context, identifier)
    handle, _, _ = _canonical_handle(prepared.get("hat_handle"))
    grant_id = _grant_id(prepared)
    owner, repo, branch, url = _repository_payload(prepared)
    target = _checkout_path(handle)
    async with _checkout_mutation_lock(target):
        return await _sync_locked(
            context,
            target=target,
            handle=handle,
            grant_id=grant_id,
            owner=owner,
            repo=repo,
            branch=branch,
            url=url,
            raw_paths=raw_paths,
            message=message,
        )


async def _sync_locked(
    context: StandalonePlatformContext,
    *,
    target: Path,
    handle: str,
    grant_id: str,
    owner: str,
    repo: str,
    branch: str,
    url: str,
    raw_paths: Any,
    message: str,
) -> dict[str, Any]:
    if not (target / ".git").is_dir():
        raise HatRepositoryError("Check out this Hat repository before syncing it.")
    remote = (
        await asyncio.to_thread(_run_git, ["remote", "get-url", "origin"], cwd=target)
    ).stdout.strip()
    if remote != url:
        raise HatRepositoryError("The local Hat checkout points at another repository.")
    await asyncio.to_thread(
        _configure_checkout,
        target,
        handle=handle,
        grant_id=grant_id,
        owner=owner,
        repo=repo,
        branch=branch,
    )
    if not isinstance(raw_paths, list) or not 1 <= len(raw_paths) <= 100:
        raise HatRepositoryError("Sync requires between 1 and 100 explicit paths.")
    paths = [_safe_sync_path(value, checkout=target) for value in raw_paths]
    if len(paths) != len(set(paths)):
        raise HatRepositoryError("Sync paths must be unique.")
    clean_message = str(message or "").strip()
    if not clean_message or len(clean_message) > 200 or "\n" in clean_message:
        raise HatRepositoryError(
            "The Git commit message must be one line under 200 characters."
        )

    resumed = await _resume_pending_sync(
        context,
        target=target,
        handle=handle,
        grant_id=grant_id,
        owner=owner,
        repo=repo,
        branch=branch,
    )
    if resumed is not None:
        return resumed

    await asyncio.to_thread(_run_git, ["fetch", "--prune", "origin"], cwd=target)
    local_head = (
        await asyncio.to_thread(_run_git, ["rev-parse", "HEAD"], cwd=target)
    ).stdout.strip()
    remote_head = (
        await asyncio.to_thread(_run_git, ["rev-parse", f"origin/{branch}"], cwd=target)
    ).stdout.strip()
    if local_head != remote_head:
        raise HatRepositoryError(
            "The remote Hat changed after checkout. Refresh it and review the changes "
            "before creating a commit."
        )
    await asyncio.to_thread(_run_git, ["add", "--", *paths], cwd=target)
    staged = await asyncio.to_thread(
        _run_git,
        ["diff", "--cached", "--name-only", "-z"],
        cwd=target,
    )
    staged_paths = [item for item in staged.stdout.split("\0") if item]
    unexpected = sorted(set(staged_paths) - set(paths))
    if unexpected:
        await asyncio.to_thread(
            _run_git, ["reset", "--quiet", "HEAD", "--", *paths], cwd=target
        )
        raise HatRepositoryError(
            "Git staged an unexpected path; the commit was cancelled."
        )
    if not staged_paths:
        return {
            "schema": SCHEMA,
            "action": "sync",
            "hat_handle": handle,
            "repository": {"owner": owner, "name": repo},
            "path": str(target),
            "head_sha": local_head,
            "changed": False,
            "pushed": False,
            "synced_paths": [],
        }
    await asyncio.to_thread(_run_git, ["commit", "-m", clean_message], cwd=target)
    head = (
        (await asyncio.to_thread(_run_git, ["rev-parse", "HEAD"], cwd=target))
        .stdout.strip()
        .casefold()
    )
    parent_head = (
        (await asyncio.to_thread(_run_git, ["rev-parse", "HEAD^"], cwd=target))
        .stdout.strip()
        .casefold()
    )
    if _SHA_RE.fullmatch(head) is None or parent_head != local_head:
        raise HatRepositoryError(
            "Git returned an invalid commit relationship before push."
        )
    await asyncio.to_thread(
        _set_pending_sync,
        target,
        head=head,
        base=local_head,
        branch=branch,
    )
    pushed = await asyncio.to_thread(
        _run_git,
        ["push", "origin", f"HEAD:refs/heads/{branch}"],
        cwd=target,
        check=False,
    )
    if pushed.returncode != 0:
        fetched = await asyncio.to_thread(
            _run_git,
            ["fetch", "--prune", "origin"],
            cwd=target,
            check=False,
        )
        remote_after = await asyncio.to_thread(
            _run_git,
            ["rev-parse", f"origin/{branch}"],
            cwd=target,
            check=False,
        )
        remote_after_sha = remote_after.stdout.strip().casefold()
        if (
            fetched.returncode == 0
            and remote_after.returncode == 0
            and remote_after_sha == head
        ):
            # The server accepted the commit but the client lost the success
            # response. Continue to platform verification instead of creating
            # a duplicate commit on retry.
            pass
        elif (
            fetched.returncode == 0
            and remote_after.returncode == 0
            and remote_after_sha == local_head
        ):
            current_head = await asyncio.to_thread(
                _run_git,
                ["rev-parse", "HEAD"],
                cwd=target,
                check=False,
            )
            current_parent = await asyncio.to_thread(
                _run_git,
                ["rev-parse", "HEAD^"],
                cwd=target,
                check=False,
            )
            if (
                current_head.returncode == 0
                and current_parent.returncode == 0
                and current_head.stdout.strip().casefold() == head
                and current_parent.stdout.strip().casefold() == local_head
            ):
                restored = await asyncio.to_thread(
                    _run_git,
                    ["reset", "--mixed", local_head],
                    cwd=target,
                    check=False,
                )
                if restored.returncode == 0:
                    await asyncio.to_thread(_clear_pending_sync, target)
                    raise HatRepositoryError(
                        "Git could not push the Hat commit. The selected changes "
                        "remain in the worktree so the sync can be retried."
                    )
            raise HatRepositoryError(
                "Git could not push or safely restore the local Hat commit. "
                "Review the checkout before retrying."
            )
        else:
            raise HatRepositoryError(
                "Git could not confirm whether the Hat commit reached the remote. "
                "Review the checkout before retrying."
            )
    await _record_synced_head(context, grant_id=grant_id, head=head)
    await asyncio.to_thread(_clear_pending_sync, target)
    return {
        "schema": SCHEMA,
        "action": "sync",
        "hat_handle": handle,
        "repository": {"owner": owner, "name": repo},
        "path": str(target),
        "head_sha": head,
        "changed": True,
        "pushed": True,
        "synced_paths": staged_paths,
        "credential_persisted": False,
    }


async def _reset(
    context: StandalonePlatformContext, identifier: str
) -> dict[str, Any]:
    # Reset consumes the already-persisted opaque grant id. Calling prepare
    # here would reactivate a revoked grant immediately before revoking it.
    target = _existing_checkout(identifier)
    async with _checkout_mutation_lock(target):
        handle, grant_id, owner, repo = _local_grant_metadata(target)
        await asyncio.to_thread(
            _disable_checkout_credentials,
            target,
            owner=owner,
            repo=repo,
        )
        result = await context.client.delete_json(
            context.computer_path(f"repository-grants/{quote(grant_id, safe='')}")
        )
    return {
        "schema": SCHEMA,
        "action": "reset",
        "hat_handle": handle,
        "repository": {"owner": owner, "name": repo},
        "path": str(target),
        "renewal_stopped": bool(result.get("renewal_stopped")),
        "residual_access_expires_at": result.get("residual_access_expires_at"),
        "local_clone_retained": target.exists(),
        "credential_helper_removed": True,
    }


def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
    return first.st_dev == second.st_dev and first.st_ino == second.st_ino


def _restore_quarantined_checkout(
    parent_descriptor: int, *, quarantine_name: str, target_name: str
) -> None:
    """Best-effort restore after a path swap; never overwrite a new target."""

    try:
        os.stat(target_name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        os.rename(
            quarantine_name,
            target_name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )


def _pending_quarantine_name(target: Path) -> str:
    """Return the deterministic recovery name for one canonical checkout."""

    return f".tinyhat-delete-{target.name}"


def _deletion_journal_location(target: Path) -> tuple[Path, str, str]:
    """Return the protected journal directory, file name, and target identity."""

    root = _repository_root().expanduser().resolve()
    try:
        relative = target.relative_to(root).as_posix()
    except ValueError as exc:
        raise HatRepositoryError("The Hat deletion journal path is unsafe.") from exc
    journal_directory = root / ".deletions"
    journal_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (
        journal_directory.is_symlink()
        or journal_directory.resolve() != journal_directory
    ):
        raise HatRepositoryError("The Hat deletion journal directory is unsafe.")
    metadata = journal_directory.stat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise HatRepositoryError("The Hat deletion journal directory is unsafe.")
    os.chmod(journal_directory, 0o700)
    name = hashlib.sha256(relative.encode("utf-8")).hexdigest() + ".json"
    return journal_directory, name, relative


def _read_deletion_journal(target: Path) -> dict[str, Any] | None:
    directory, name, _relative = _deletion_journal_location(target)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(directory / name, flags)
    except FileNotFoundError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_size > 4096
        ):
            raise HatRepositoryError("The Hat deletion journal is unsafe.")
        raw = bytearray()
        while True:
            chunk = os.read(descriptor, 4096 - len(raw) + 1)
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > 4096:
                raise HatRepositoryError("The Hat deletion journal is unsafe.")
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HatRepositoryError("The Hat deletion journal is invalid.") from exc
    if not isinstance(payload, dict):
        raise HatRepositoryError("The Hat deletion journal is invalid.")
    return payload


def _write_deletion_journal(
    target: Path,
    *,
    metadata: os.stat_result,
    handle: str,
    expected_owner: str,
    expected_repo: str,
    expected_url: str,
    phase: str,
) -> None:
    """Atomically persist the verified inode before destructive traversal."""

    directory, name, relative = _deletion_journal_location(target)
    payload = {
        "schema": _DELETION_JOURNAL_SCHEMA,
        "relative_path": relative,
        "handle": handle,
        "owner": expected_owner,
        "repo": expected_repo,
        "url": expected_url,
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "phase": phase,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    temporary_descriptor, temporary_path = tempfile.mkstemp(
        prefix=f".{name}.", suffix=".tmp", dir=directory
    )
    try:
        os.fchmod(temporary_descriptor, 0o600)
        remaining = memoryview(encoded)
        while remaining:
            written = os.write(temporary_descriptor, remaining)
            if written <= 0:
                raise OSError("The deletion journal write did not make progress.")
            remaining = remaining[written:]
        os.fsync(temporary_descriptor)
        os.close(temporary_descriptor)
        temporary_descriptor = -1
        os.replace(temporary_path, directory / name)
        directory_descriptor = os.open(
            directory,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass


def _clear_deletion_journal(target: Path) -> None:
    directory, name, _relative = _deletion_journal_location(target)
    try:
        os.unlink(directory / name)
    except FileNotFoundError:
        return


def _validated_deletion_journal(
    target: Path,
    *,
    handle: str,
    expected_owner: str,
    expected_repo: str,
    expected_url: str,
) -> dict[str, Any] | None:
    journal = _read_deletion_journal(target)
    if journal is None:
        return None
    _directory, _name, relative = _deletion_journal_location(target)
    expected = {
        "schema": _DELETION_JOURNAL_SCHEMA,
        "relative_path": relative,
        "handle": handle,
        "owner": expected_owner,
        "repo": expected_repo,
        "url": expected_url,
    }
    if set(journal) != {*expected, "device", "inode", "phase"} or any(
        journal.get(key) != value for key, value in expected.items()
    ):
        raise HatRepositoryError(
            "The pending Hat deletion belongs to another repository."
        )
    if not all(
        type(journal.get(key)) is int and journal[key] >= 0
        for key in ("device", "inode")
    ):
        raise HatRepositoryError("The Hat deletion journal is invalid.")
    if journal.get("phase") not in {"validated", "quarantined", "removed"}:
        raise HatRepositoryError("The Hat deletion journal is invalid.")
    return journal


def _resume_pending_deletion(
    target: Path,
    *,
    handle: str,
    expected_owner: str,
    expected_repo: str,
    expected_url: str,
) -> bool | None:
    """Resume only the inode already authenticated in the trusted journal."""

    journal = _validated_deletion_journal(
        target,
        handle=handle,
        expected_owner=expected_owner,
        expected_repo=expected_repo,
        expected_url=expected_url,
    )
    if journal is None:
        return None

    parent_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    parent_flags |= getattr(os, "O_CLOEXEC", 0)
    parent_flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        parent_descriptor = os.open(target.parent, parent_flags)
    except FileNotFoundError as exc:
        raise HatRepositoryError(
            "The pending local Hat checkout deletion is unsafe to resume."
        ) from exc
    quarantine_name = _pending_quarantine_name(target)
    try:
        try:
            target_metadata = os.stat(
                target.name, dir_fd=parent_descriptor, follow_symlinks=False
            )
        except FileNotFoundError:
            target_metadata = None
        try:
            quarantined = os.stat(
                quarantine_name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            quarantined = None
        expected_identity = (journal["device"], journal["inode"])
        if target_metadata is not None and quarantined is not None:
            raise HatRepositoryError(
                "The pending local Hat checkout deletion is unsafe to resume."
            )
        if target_metadata is None and quarantined is None:
            if journal["phase"] == "removed":
                _clear_deletion_journal(target)
                return True
            raise HatRepositoryError(
                "The pending local Hat checkout deletion cannot be proven complete."
            )
        if journal["phase"] == "removed":
            raise HatRepositoryError(
                "The completed Hat deletion journal still has local data."
            )
        pending = quarantined if quarantined is not None else target_metadata
        assert pending is not None
        if (
            not stat.S_ISDIR(pending.st_mode)
            or pending.st_uid != os.geteuid()
            or (pending.st_dev, pending.st_ino) != expected_identity
        ):
            raise HatRepositoryError(
                "The pending local Hat checkout deletion is unsafe to resume."
            )
        if quarantined is None:
            os.rename(
                target.name,
                quarantine_name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
        _write_deletion_journal(
            target,
            metadata=pending,
            handle=handle,
            expected_owner=expected_owner,
            expected_repo=expected_repo,
            expected_url=expected_url,
            phase="quarantined",
        )
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        checkout_descriptor = os.open(
            quarantine_name, directory_flags, dir_fd=parent_descriptor
        )
        try:
            opened = os.fstat(checkout_descriptor)
            if (opened.st_dev, opened.st_ino) != expected_identity:
                raise HatRepositoryError(
                    "The pending local Hat checkout deletion is unsafe to resume."
                )
            _remove_directory_contents(checkout_descriptor)
        finally:
            os.close(checkout_descriptor)
        os.rmdir(quarantine_name, dir_fd=parent_descriptor)
        _write_deletion_journal(
            target,
            metadata=pending,
            handle=handle,
            expected_owner=expected_owner,
            expected_repo=expected_repo,
            expected_url=expected_url,
            phase="removed",
        )
        _clear_deletion_journal(target)
        return True
    except OSError as exc:
        raise HatRepositoryError(
            "The pending local Hat checkout deletion could not be safely resumed."
        ) from exc
    finally:
        os.close(parent_descriptor)


def _remove_directory_contents(descriptor: int) -> None:
    """Remove one opened directory tree without following path swaps or symlinks."""

    for entry in os.listdir(descriptor):
        metadata = os.stat(entry, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            child = os.open(entry, flags, dir_fd=descriptor)
            try:
                if not _same_file(metadata, os.fstat(child)):
                    raise HatRepositoryError(
                        "The local Hat checkout changed during deletion."
                    )
                _remove_directory_contents(child)
            finally:
                os.close(child)
            os.rmdir(entry, dir_fd=descriptor)
        else:
            os.unlink(entry, dir_fd=descriptor)


def _delete_verified_checkout(
    target: Path,
    *,
    handle: str,
    expected_owner: str,
    expected_repo: str,
    expected_url: str,
) -> None:
    """Validate, atomically quarantine, then fd-delete one exact checkout."""

    parent_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    parent_flags |= getattr(os, "O_CLOEXEC", 0)
    parent_flags |= getattr(os, "O_NOFOLLOW", 0)
    parent_descriptor = os.open(target.parent, parent_flags)
    quarantine_name = ""
    try:
        parent_metadata = os.fstat(parent_descriptor)
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or parent_metadata.st_uid != os.geteuid()
        ):
            raise HatRepositoryError("The local Hat checkout is unsafe to delete.")
        metadata = os.stat(
            target.name, dir_fd=parent_descriptor, follow_symlinks=False
        )
        git_directory = target / ".git"
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or git_directory.is_symlink()
            or not git_directory.is_dir()
        ):
            raise HatRepositoryError("The local Hat checkout is unsafe to delete.")
        local_handle, _grant_id_value, owner, repo = _local_grant_metadata(target)
        remote = _run_git(["remote", "get-url", "origin"], cwd=target).stdout.strip()
        if local_handle != handle:
            raise HatRepositoryError("The local Hat checkout belongs to another Hat.")
        if owner != expected_owner or repo != expected_repo or remote != expected_url:
            raise HatRepositoryError(
                "The local Hat checkout points at another repository."
            )
        _disable_checkout_credentials(target, owner=owner, repo=repo)

        after_validation = os.stat(
            target.name, dir_fd=parent_descriptor, follow_symlinks=False
        )
        if not _same_file(metadata, after_validation):
            raise HatRepositoryError("The local Hat checkout changed during deletion.")

        _write_deletion_journal(
            target,
            metadata=metadata,
            handle=handle,
            expected_owner=expected_owner,
            expected_repo=expected_repo,
            expected_url=expected_url,
            phase="validated",
        )
        quarantine_name = _pending_quarantine_name(target)
        os.rename(
            target.name,
            quarantine_name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        quarantined = os.stat(
            quarantine_name, dir_fd=parent_descriptor, follow_symlinks=False
        )
        if not _same_file(metadata, quarantined):
            _restore_quarantined_checkout(
                parent_descriptor,
                quarantine_name=quarantine_name,
                target_name=target.name,
            )
            _clear_deletion_journal(target)
            raise HatRepositoryError("The local Hat checkout changed during deletion.")
        _write_deletion_journal(
            target,
            metadata=metadata,
            handle=handle,
            expected_owner=expected_owner,
            expected_repo=expected_repo,
            expected_url=expected_url,
            phase="quarantined",
        )

        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        checkout_descriptor = os.open(
            quarantine_name, directory_flags, dir_fd=parent_descriptor
        )
        try:
            if not _same_file(metadata, os.fstat(checkout_descriptor)):
                _restore_quarantined_checkout(
                    parent_descriptor,
                    quarantine_name=quarantine_name,
                    target_name=target.name,
                )
                _clear_deletion_journal(target)
                raise HatRepositoryError(
                    "The local Hat checkout changed during deletion."
                )
            _remove_directory_contents(checkout_descriptor)
        finally:
            os.close(checkout_descriptor)
        os.rmdir(quarantine_name, dir_fd=parent_descriptor)
        _write_deletion_journal(
            target,
            metadata=metadata,
            handle=handle,
            expected_owner=expected_owner,
            expected_repo=expected_repo,
            expected_url=expected_url,
            phase="removed",
        )
        _clear_deletion_journal(target)
    except HatRepositoryError:
        raise
    except OSError as exc:
        raise HatRepositoryError(
            "The local Hat checkout could not be safely deleted."
        ) from exc
    finally:
        os.close(parent_descriptor)


async def _delete_local(identifier: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Delete only the verified checkout for one canonical Hat handle."""

    handle, _, _ = _canonical_handle(identifier)
    expected_owner, expected_repo, expected_url = _deletion_repository_payload(payload)
    target = _deletion_checkout_path(handle)
    async with _checkout_mutation_lock(target):
        resumed = await asyncio.to_thread(
            _resume_pending_deletion,
            target,
            handle=handle,
            expected_owner=expected_owner,
            expected_repo=expected_repo,
            expected_url=expected_url,
        )
        if resumed is not None:
            return {
                "schema": SCHEMA,
                "action": "delete_local",
                "hat_handle": handle,
                "path": str(target),
                "removed": resumed,
            }
        quarantine = target.with_name(_pending_quarantine_name(target))
        if quarantine.exists():
            raise HatRepositoryError(
                "An untrusted pending Hat deletion requires manual inspection."
            )
        if not target.exists():
            return {
                "schema": SCHEMA,
                "action": "delete_local",
                "hat_handle": handle,
                "path": str(target),
                "removed": False,
            }
        await asyncio.to_thread(
            _delete_verified_checkout,
            target,
            handle=handle,
            expected_owner=expected_owner,
            expected_repo=expected_repo,
            expected_url=expected_url,
        )
        if target.exists():
            raise HatRepositoryError("The local Hat checkout could not be deleted.")
    return {
        "schema": SCHEMA,
        "action": "delete_local",
        "hat_handle": handle,
        "path": str(target),
        "removed": True,
    }


async def run(
    context: StandalonePlatformContext, payload: dict[str, Any]
) -> dict[str, Any]:
    """Execute one bounded repository action without returning credentials."""

    action = _required_text(payload, "action", max_length=31).casefold()
    identifier = _required_text(payload, "identifier", max_length=255)
    if action == "checkout":
        return await _checkout(context, identifier)
    if action == "status":
        return await _status(identifier)
    if action == "sync":
        return await _sync(
            context,
            identifier,
            raw_paths=payload.get("paths"),
            message=str(payload.get("message") or ""),
        )
    if action == "reset":
        return await _reset(context, identifier)
    if action == "delete_local":
        return await _delete_local(identifier, payload)
    raise HatRepositoryError("Unsupported Hat repository action.")


def safe_error_payload(exc: Exception) -> dict[str, Any]:
    message = (
        str(exc)
        if isinstance(exc, HatRepositoryError)
        else ("The Hat repository operation failed without changing repository access.")
    )
    return {
        "schema": SCHEMA,
        "status": "error",
        "error": "hat_repository_failed",
        "message": message,
    }


__all__ = ["HatRepositoryError", "run", "safe_error_payload"]
