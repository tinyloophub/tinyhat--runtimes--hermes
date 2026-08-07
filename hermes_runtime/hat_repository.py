"""Computer-local Git workflow for one explicitly granted Hat repository."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any
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
    _run_git(["config", "--local", "credential.useHttpPath", "true"], cwd=path)
    _run_git(
        ["config", "--local", "--unset-all", _credential_key(owner, repo)],
        cwd=path,
        check=False,
    )
    _run_git(
        ["config", "--local", _credential_key(owner, repo), helper], cwd=path
    )
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
        temporary = Path(
            tempfile.mkdtemp(prefix=".tinyhat-clone-", dir=str(target.parent))
        )
        try:
            _run_git(
                ["clone", "--branch", branch, "--single-branch", url, str(temporary)],
                extra_config=[
                    ("credential.useHttpPath", "true"),
                    (_credential_key(owner, repo), helper),
                ],
            )
            temporary.replace(target)
            created = True
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
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
        raise HatRepositoryError("The Git commit message must be one line under 200 characters.")

    await asyncio.to_thread(_run_git, ["fetch", "--prune", "origin"], cwd=target)
    local_head = (
        await asyncio.to_thread(_run_git, ["rev-parse", "HEAD"], cwd=target)
    ).stdout.strip()
    remote_head = (
        await asyncio.to_thread(
            _run_git, ["rev-parse", f"origin/{branch}"], cwd=target
        )
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
        await asyncio.to_thread(_run_git, ["reset", "--quiet", "HEAD", "--", *paths], cwd=target)
        raise HatRepositoryError("Git staged an unexpected path; the commit was cancelled.")
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
    await asyncio.to_thread(
        _run_git,
        ["push", "origin", f"HEAD:refs/heads/{branch}"],
        cwd=target,
    )
    head = (
        await asyncio.to_thread(_run_git, ["rev-parse", "HEAD"], cwd=target)
    ).stdout.strip().casefold()
    if _SHA_RE.fullmatch(head) is None:
        raise HatRepositoryError("Git returned an invalid commit id after push.")
    recorded = await context.client.post_json(
        context.computer_path(
            f"repository-grants/{quote(grant_id, safe='')}/sync"
        ),
        {"head_sha": head},
    )
    if recorded.get("verified") is not True:
        raise HatRepositoryError("Tinyhat could not verify the pushed Hat commit.")
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
    handle, grant_id, owner, repo = _local_grant_metadata(target)
    await asyncio.to_thread(
        _run_git,
        ["config", "--local", "--unset-all", _credential_key(owner, repo)],
        cwd=target,
        check=False,
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
    raise HatRepositoryError("Unsupported Hat repository action.")


def safe_error_payload(exc: Exception) -> dict[str, Any]:
    message = str(exc) if isinstance(exc, HatRepositoryError) else (
        "The Hat repository operation failed without changing repository access."
    )
    return {
        "schema": SCHEMA,
        "status": "error",
        "error": "hat_repository_failed",
        "message": message,
    }


__all__ = ["HatRepositoryError", "run", "safe_error_payload"]
