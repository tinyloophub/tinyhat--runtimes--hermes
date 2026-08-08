"""Tests for Computer-local, exact-repository Hat Git access."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from hermes_runtime import github_credential_helper, hat_repository

GRANT_ID = "rgr_abcdefghijklmnopqrstuvwx"


class GitHubCredentialHelperTests(unittest.TestCase):
    def test_get_emits_lease_only_for_the_exact_repository(self) -> None:
        stdin = io.StringIO(
            "protocol=https\nhost=github.com\npath=tinyhat-ai/example-hat.git\n\n"
        )
        stdout = io.StringIO()
        with (
            mock.patch.object(
                github_credential_helper,
                "_lease",
                mock.AsyncMock(
                    return_value={"username": "x-access-token", "token": "lease-token"}
                ),
            ) as lease,
            mock.patch("sys.stdin", stdin),
            mock.patch("sys.stdout", stdout),
        ):
            code = github_credential_helper.main(
                [
                    "--grant-id",
                    GRANT_ID,
                    "--owner",
                    "tinyhat-ai",
                    "--repo",
                    "example-hat",
                    "get",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(
            stdout.getvalue(),
            "username=x-access-token\npassword=lease-token\n\n",
        )
        lease.assert_awaited_once_with(GRANT_ID)

    def test_get_rejects_a_different_repository_without_minting(self) -> None:
        stdin = io.StringIO(
            "protocol=https\nhost=github.com\npath=tinyhat-ai/other.git\n\n"
        )
        with (
            mock.patch.object(
                github_credential_helper,
                "_lease",
                mock.AsyncMock(),
            ) as lease,
            mock.patch("sys.stdin", stdin),
        ):
            code = github_credential_helper.main(
                [
                    "--grant-id",
                    GRANT_ID,
                    "--owner",
                    "tinyhat-ai",
                    "--repo",
                    "example-hat",
                    "get",
                ]
            )

        self.assertEqual(code, 1)
        lease.assert_not_awaited()


class HatRepositoryTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _create_checkout(
        path: Path,
        handle: str,
        *,
        owner: str = "tinyhat-ai",
        repo: str | None = None,
    ) -> None:
        repo = repo or f"repo-{path.name}"
        subprocess.run(
            ["git", "init", "-q", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
        for key, value in (
            ("tinyhat.hatHandle", handle),
            ("tinyhat.repositoryGrantId", GRANT_ID),
            ("tinyhat.repositoryOwner", owner),
            ("tinyhat.repositoryName", repo),
        ):
            subprocess.run(
                ["git", "-C", str(path), "config", "--local", key, value],
                check=True,
                capture_output=True,
                text=True,
            )
        subprocess.run(
            [
                "git",
                "-C",
                str(path),
                "remote",
                "add",
                "origin",
                f"https://github.com/{owner}/{repo}.git",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    @staticmethod
    def _delete_payload(path: Path) -> dict[str, object]:
        repo = f"repo-{path.name}"
        return {
            "action": "delete_local",
            "identifier": f"itsfaridkia/hats/{path.name}",
            "repository": {
                "owner": "tinyhat-ai",
                "name": repo,
                "url": f"https://github.com/tinyhat-ai/{repo}.git",
            },
        }

    async def test_delete_local_removes_only_the_verified_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            old_checkout = home / "hat-repositories" / "itsfaridkia" / "old-hat"
            current_checkout = home / "hat-repositories" / "itsfaridkia" / "current-hat"
            unrelated = home / "hat-repositories" / "itsfaridkia" / "unrelated"
            self._create_checkout(old_checkout, "itsfaridkia/hats/old-hat")
            self._create_checkout(current_checkout, "itsfaridkia/hats/current-hat")
            self._create_checkout(unrelated, "itsfaridkia/hats/unrelated")

            with mock.patch.dict(os.environ, {"HERMES_HOME": str(home)}):
                old_result = await hat_repository._delete_local(
                    "itsfaridkia/hats/old-hat", self._delete_payload(old_checkout)
                )
                current_result = await hat_repository._delete_local(
                    "itsfaridkia/hats/current-hat",
                    self._delete_payload(current_checkout),
                )

            self.assertTrue(old_result["removed"])
            self.assertTrue(current_result["removed"])
            self.assertFalse(old_checkout.exists())
            self.assertFalse(current_checkout.exists())
            self.assertTrue(unrelated.is_dir())

    async def test_delete_local_is_idempotent_for_an_absent_checkout(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"HERMES_HOME": tmp}),
        ):
            missing = Path(tmp) / "hat-repositories" / "itsfaridkia" / "missing-hat"
            result = await hat_repository._delete_local(
                "itsfaridkia/hats/missing-hat", self._delete_payload(missing)
            )

        self.assertFalse(result["removed"])

    async def test_delete_local_rejects_checkout_with_mismatched_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            checkout = home / "hat-repositories" / "itsfaridkia" / "expected"
            self._create_checkout(checkout, "itsfaridkia/hats/another-hat")
            with (
                mock.patch.dict(os.environ, {"HERMES_HOME": str(home)}),
                self.assertRaisesRegex(
                    hat_repository.HatRepositoryError,
                    "belongs to another Hat",
                ),
            ):
                await hat_repository._delete_local(
                    "itsfaridkia/hats/expected", self._delete_payload(checkout)
                )

            self.assertTrue(checkout.is_dir())

    async def test_delete_local_rejects_symlink_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            root = home / "hat-repositories" / "itsfaridkia"
            unrelated = root / "unrelated"
            self._create_checkout(unrelated, "itsfaridkia/hats/unrelated")
            (root / "expected").symlink_to(unrelated, target_is_directory=True)
            with (
                mock.patch.dict(os.environ, {"HERMES_HOME": str(home)}),
                self.assertRaisesRegex(
                    hat_repository.HatRepositoryError,
                    "unsafe to delete",
                ),
            ):
                await hat_repository._delete_local(
                    "itsfaridkia/hats/expected", self._delete_payload(root / "expected")
                )

            self.assertTrue(unrelated.is_dir())

    async def test_delete_local_rejects_untrusted_repository_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            checkout = home / "hat-repositories" / "itsfaridkia" / "expected"
            self._create_checkout(
                checkout,
                "itsfaridkia/hats/expected",
                owner="attacker",
                repo="unrelated",
            )
            payload = self._delete_payload(checkout)
            with (
                mock.patch.dict(os.environ, {"HERMES_HOME": str(home)}),
                self.assertRaisesRegex(
                    hat_repository.HatRepositoryError,
                    "points at another repository",
                ),
            ):
                await hat_repository._delete_local(
                    "itsfaridkia/hats/expected", payload
                )

            self.assertTrue(checkout.is_dir())

    async def test_delete_local_restores_swapped_checkout_without_deleting_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            parent = home / "hat-repositories" / "itsfaridkia"
            checkout = parent / "expected"
            unrelated = parent / "unrelated"
            validated = parent / "validated"
            self._create_checkout(checkout, "itsfaridkia/hats/expected")
            self._create_checkout(unrelated, "itsfaridkia/hats/unrelated")
            real_rename = os.rename
            swapped = False

            def swap_before_quarantine(
                source: str,
                destination: str,
                *,
                src_dir_fd: int | None = None,
                dst_dir_fd: int | None = None,
            ) -> None:
                nonlocal swapped
                if not swapped and source == "expected":
                    swapped = True
                    real_rename(
                        source,
                        "validated",
                        src_dir_fd=src_dir_fd,
                        dst_dir_fd=dst_dir_fd,
                    )
                    real_rename(
                        "unrelated",
                        source,
                        src_dir_fd=src_dir_fd,
                        dst_dir_fd=dst_dir_fd,
                    )
                real_rename(
                    source,
                    destination,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )

            with (
                mock.patch.dict(os.environ, {"HERMES_HOME": str(home)}),
                mock.patch.object(hat_repository.os, "rename", swap_before_quarantine),
                self.assertRaisesRegex(
                    hat_repository.HatRepositoryError,
                    "changed during deletion",
                ),
            ):
                await hat_repository._delete_local(
                    "itsfaridkia/hats/expected", self._delete_payload(checkout)
                )

            self.assertTrue(swapped)
            self.assertTrue(checkout.is_dir())
            self.assertTrue(validated.is_dir())

    def test_checkout_mutation_lock_serializes_processes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            target = home / "hat-repositories" / "itsfaridkia" / "example-hat"
            log = Path(tmp) / "order.log"
            ready = Path(tmp) / "first-ready"
            release = Path(tmp) / "release-first"
            script = textwrap.dedent(
                """
                import asyncio
                import sys
                from pathlib import Path

                from hermes_runtime import hat_repository

                async def main():
                    target = Path(sys.argv[1])
                    log = Path(sys.argv[2])
                    label = sys.argv[3]
                    ready = Path(sys.argv[4])
                    release = Path(sys.argv[5])
                    async with hat_repository._checkout_mutation_lock(target):
                        with log.open("a", encoding="utf-8") as stream:
                            stream.write(f"{label}:start\\n")
                            stream.flush()
                        if label == "first":
                            ready.write_text("ready", encoding="utf-8")
                            for _ in range(500):
                                if release.exists():
                                    break
                                await asyncio.sleep(0.01)
                            else:
                                raise RuntimeError("release was not signalled")
                        with log.open("a", encoding="utf-8") as stream:
                            stream.write(f"{label}:end\\n")

                asyncio.run(main())
                """
            )
            env = dict(os.environ)
            env["HERMES_HOME"] = str(home)
            first = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(target),
                    str(log),
                    "first",
                    str(ready),
                    str(release),
                ],
                cwd=str(Path(__file__).resolve().parents[1]),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "first process never acquired the lock")
            second = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(target),
                    str(log),
                    "second",
                    str(ready),
                    str(release),
                ],
                cwd=str(Path(__file__).resolve().parents[1]),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            time.sleep(0.2)
            blocked_order = log.read_text(encoding="utf-8")
            release.write_text("release", encoding="utf-8")
            first_stdout, first_stderr = first.communicate(timeout=5)
            second_stdout, second_stderr = second.communicate(timeout=5)
            final_order = log.read_text(encoding="utf-8").splitlines()

        self.assertEqual(blocked_order, "first:start\n")
        self.assertEqual(
            (first.returncode, first_stdout, first_stderr),
            (0, "", ""),
        )
        self.assertEqual(
            (second.returncode, second_stdout, second_stderr),
            (0, "", ""),
        )
        self.assertEqual(
            final_order,
            ["first:start", "first:end", "second:start", "second:end"],
        )

    def test_bounded_helper_shadows_global_and_reset_stays_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = root / "checkout"
            global_config = root / "global.gitconfig"
            broad_marker = root / "broad-helper-ran"
            broad_helper = root / "broad-helper"
            safe_helper = root / "safe-helper"
            broad_helper.write_text(
                "#!/bin/sh\n"
                f"touch {broad_marker.as_posix()}\n"
                "printf 'username=broad\\npassword=broad\\n\\n'\n",
                encoding="utf-8",
            )
            safe_helper.write_text(
                "#!/bin/sh\nprintf 'username=safe\\npassword=safe\\n\\n'\n",
                encoding="utf-8",
            )
            broad_helper.chmod(0o755)
            safe_helper.chmod(0o755)
            subprocess.run(
                ["git", "init", "-q", str(checkout)],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    "git",
                    "config",
                    "--file",
                    str(global_config),
                    "credential.helper",
                    f"!{broad_helper}",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            git_env = {
                **os.environ,
                "GIT_CONFIG_GLOBAL": str(global_config),
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "GCM_INTERACTIVE": "never",
            }
            credential_input = (
                "protocol=https\nhost=github.com\npath=tinyhat-ai/example-hat.git\n\n"
            )
            with (
                mock.patch.dict(os.environ, git_env, clear=True),
                mock.patch.object(
                    hat_repository,
                    "_credential_helper_command",
                    return_value=f"!{safe_helper}",
                ),
            ):
                hat_repository._configure_checkout(
                    checkout,
                    handle="itsfaridkia/hats/example-hat",
                    grant_id=GRANT_ID,
                    owner="tinyhat-ai",
                    repo="example-hat",
                    branch="main",
                )
                active = subprocess.run(
                    ["git", "-C", str(checkout), "credential", "fill"],
                    input=credential_input,
                    env=git_env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(active.returncode, 0)
                self.assertIn("username=safe", active.stdout)
                self.assertFalse(broad_marker.exists())

                hat_repository._disable_checkout_credentials(
                    checkout,
                    owner="tinyhat-ai",
                    repo="example-hat",
                )
                reset = subprocess.run(
                    ["git", "-C", str(checkout), "credential", "fill"],
                    input=credential_input,
                    env=git_env,
                    capture_output=True,
                    text=True,
                    check=False,
                )

            self.assertNotEqual(reset.returncode, 0)
            self.assertNotIn("username=broad", reset.stdout)
            self.assertFalse(broad_marker.exists())

    def test_local_git_config_contains_a_grant_but_no_credential(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            subprocess.run(
                ["git", "init", "-q", str(checkout)],
                check=True,
                capture_output=True,
                text=True,
            )
            hat_repository._configure_checkout(
                checkout,
                handle="itsfaridkia/hats/example-hat",
                grant_id=GRANT_ID,
                owner="tinyhat-ai",
                repo="example-hat",
                branch="main",
            )
            config = (checkout / ".git" / "config").read_text(encoding="utf-8")
            matched_helper = subprocess.run(
                [
                    "git",
                    "-C",
                    str(checkout),
                    "config",
                    "--get-urlmatch",
                    "credential.helper",
                    "https://github.com/tinyhat-ai/example-hat.git",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout

        self.assertIn(GRANT_ID, config)
        self.assertIn("github_credential_helper", config)
        self.assertIn("github_credential_helper", matched_helper)
        self.assertIn("itsfaridkia/hats/example-hat", config)
        self.assertNotIn("password", config.casefold())
        self.assertNotIn("x-access-token", config)

    def test_sync_paths_block_credentials_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            (checkout / "skills").mkdir()
            outside = checkout.parent / "outside-secret"
            (checkout / "skills" / "linked").symlink_to(outside)

            self.assertEqual(
                hat_repository._safe_sync_path(
                    "skills/forecasting/SKILL.md",
                    checkout=checkout,
                ),
                "skills/forecasting/SKILL.md",
            )
            for unsafe in (".env", ".tinyhat/credentials.json", "private.pem"):
                with self.assertRaises(hat_repository.HatRepositoryError):
                    hat_repository._safe_sync_path(unsafe, checkout=checkout)
            with self.assertRaises(hat_repository.HatRepositoryError):
                hat_repository._safe_sync_path("skills/linked", checkout=checkout)

    async def test_checkout_returns_no_grant_or_token(self) -> None:
        prepared = {
            "grant_id": GRANT_ID,
            "hat_handle": "itsfaridkia/hats/example-hat",
            "repository": {
                "owner": "tinyhat-ai",
                "name": "tld--itsfaridkia--hats--example-hat",
                "default_branch": "main",
                "url": (
                    "https://github.com/tinyhat-ai/"
                    "tld--itsfaridkia--hats--example-hat.git"
                ),
            },
        }
        fake_git = mock.Mock(stdout="a" * 40 + "\n")
        context = mock.Mock()
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"HERMES_HOME": tmp}),
            mock.patch.object(
                hat_repository,
                "_prepare",
                mock.AsyncMock(return_value=prepared),
            ),
            mock.patch.object(
                hat_repository,
                "_clone_or_refresh",
                return_value=True,
            ) as clone,
            mock.patch.object(hat_repository, "_run_git", return_value=fake_git),
        ):
            result = await hat_repository._checkout(context, "example-hat")

        self.assertTrue(result["created"])
        self.assertFalse(result["credential_persisted"])
        self.assertNotIn("grant_id", result)
        self.assertNotIn("token", result)
        self.assertEqual(clone.call_args.kwargs["grant_id"], GRANT_ID)
        self.assertEqual(
            result["repository"],
            {
                "owner": "tinyhat-ai",
                "name": "tld--itsfaridkia--hats--example-hat",
            },
        )

    async def test_status_is_local_only_and_does_not_prepare_access(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"HERMES_HOME": tmp}),
            mock.patch.object(
                hat_repository,
                "_prepare",
                mock.AsyncMock(),
            ) as prepare,
        ):
            checkout = Path(tmp) / "hat-repositories" / "itsfaridkia" / "example-hat"
            subprocess.run(
                ["git", "init", "-q", str(checkout)],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(checkout),
                    "config",
                    "--local",
                    "tinyhat.hatHandle",
                    "itsfaridkia/hats/example-hat",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            for key, value in (
                ("tinyhat.repositoryGrantId", GRANT_ID),
                ("tinyhat.repositoryOwner", "tinyhat-ai"),
                ("tinyhat.repositoryName", "example-hat"),
            ):
                subprocess.run(
                    ["git", "-C", str(checkout), "config", "--local", key, value],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            subprocess.run(
                ["git", "-C", str(checkout), "config", "user.name", "Test"],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(checkout),
                    "config",
                    "user.email",
                    "test@example.com",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "-C", str(checkout), "commit", "--allow-empty", "-qm", "init"],
                check=True,
                capture_output=True,
                text=True,
            )
            result = await hat_repository._status("example-hat")

        self.assertTrue(result["clean"])
        self.assertEqual(
            result["repository"],
            {"owner": "tinyhat-ai", "name": "example-hat"},
        )
        prepare.assert_not_awaited()

    async def test_reset_removes_helper_but_retains_local_clone(self) -> None:
        prepared = {
            "grant_id": GRANT_ID,
            "hat_handle": "itsfaridkia/hats/example-hat",
            "repository": {
                "owner": "tinyhat-ai",
                "name": "tld--itsfaridkia--hats--example-hat",
                "default_branch": "main",
                "url": (
                    "https://github.com/tinyhat-ai/"
                    "tld--itsfaridkia--hats--example-hat.git"
                ),
            },
        }
        context = mock.Mock()
        context.client.delete_json = mock.AsyncMock(
            return_value={
                "renewal_stopped": True,
                "residual_access_expires_at": "2026-08-07T20:00:00+00:00",
            }
        )
        context.computer_path.side_effect = lambda value: f"/hapi/v1/{value}"
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"HERMES_HOME": tmp}),
            mock.patch.object(
                hat_repository,
                "_prepare",
                mock.AsyncMock(return_value=prepared),
            ),
            mock.patch.object(hat_repository, "_run_git") as run_git,
        ):
            checkout = Path(tmp) / "hat-repositories" / "itsfaridkia" / "example-hat"
            (checkout / ".git").mkdir(parents=True)
            local_values = {
                "tinyhat.hatHandle": "itsfaridkia/hats/example-hat",
                "tinyhat.repositoryGrantId": GRANT_ID,
                "tinyhat.repositoryOwner": "tinyhat-ai",
                "tinyhat.repositoryName": "example-hat",
            }
            run_git.side_effect = lambda args, **_kwargs: mock.Mock(
                returncode=0,
                stdout=(
                    local_values.get(args[-1], "") + "\n"
                    if args[:4] == ["config", "--local", "--get", args[-1]]
                    else ""
                ),
            )
            result = await hat_repository._reset(context, "example-hat")

        self.assertTrue(result["renewal_stopped"])
        self.assertTrue(result["local_clone_retained"])
        self.assertTrue(result["credential_helper_removed"])
        self.assertGreaterEqual(run_git.call_count, 5)

    async def test_failed_push_restores_changes_for_successful_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            seed = root / "seed"
            home = root / "home"
            target = home / "hat-repositories" / "itsfaridkia" / "example-hat"
            subprocess.run(
                ["git", "init", "--bare", "-q", "--initial-branch=main", str(remote)],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "init", "-q", "--initial-branch=main", str(seed)],
                check=True,
                capture_output=True,
                text=True,
            )
            for key, value in (
                ("user.name", "Test"),
                ("user.email", "test@example.com"),
            ):
                subprocess.run(
                    ["git", "-C", str(seed), "config", key, value],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            (seed / "README.md").write_text("initial\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(seed), "add", "README.md"],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "-C", str(seed), "commit", "-qm", "initial"],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "-C", str(seed), "remote", "add", "origin", str(remote)],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "-C", str(seed), "push", "-q", "origin", "main"],
                check=True,
                capture_output=True,
                text=True,
            )
            target.parent.mkdir(parents=True)
            subprocess.run(
                ["git", "clone", "-q", str(remote), str(target)],
                check=True,
                capture_output=True,
                text=True,
            )
            reject_once = root / "reject-once"
            reject_once.touch()
            hook = remote / "hooks" / "pre-receive"
            hook.write_text(
                "#!/bin/sh\n"
                f"if [ -f {reject_once.as_posix()} ]; then\n"
                f"  rm {reject_once.as_posix()}\n"
                "  exit 1\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            hook.chmod(0o755)
            (target / "notes.md").write_text("retry me\n", encoding="utf-8")
            prepared = {
                "grant_id": GRANT_ID,
                "hat_handle": "itsfaridkia/hats/example-hat",
            }
            context = mock.Mock()
            context.client.post_json = mock.AsyncMock(return_value={"verified": True})
            context.computer_path.side_effect = lambda value: f"/hapi/v1/{value}"
            with (
                mock.patch.dict(os.environ, {"HERMES_HOME": str(home)}),
                mock.patch.object(
                    hat_repository,
                    "_prepare",
                    mock.AsyncMock(return_value=prepared),
                ),
                mock.patch.object(
                    hat_repository,
                    "_repository_payload",
                    return_value=(
                        "tinyhat-ai",
                        "example-hat",
                        "main",
                        str(remote),
                    ),
                ),
            ):
                with self.assertRaisesRegex(
                    hat_repository.HatRepositoryError,
                    "remain in the worktree",
                ):
                    await hat_repository._sync(
                        context,
                        "example-hat",
                        raw_paths=["notes.md"],
                        message="test: retry rejected push",
                    )

                local_after_failure = subprocess.run(
                    ["git", "-C", str(target), "rev-parse", "HEAD"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                remote_after_failure = subprocess.run(
                    ["git", "-C", str(target), "rev-parse", "origin/main"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                self.assertEqual(local_after_failure, remote_after_failure)
                self.assertIn(
                    "notes.md",
                    subprocess.run(
                        ["git", "-C", str(target), "status", "--porcelain"],
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout,
                )

                result = await hat_repository._sync(
                    context,
                    "example-hat",
                    raw_paths=["notes.md"],
                    message="test: retry rejected push",
                )

            self.assertTrue(result["pushed"])
            self.assertFalse(reject_once.exists())
            self.assertEqual(
                subprocess.run(
                    ["git", "--git-dir", str(remote), "rev-parse", "main"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                result["head_sha"],
            )

    async def test_pending_sync_resumes_after_push_and_fetch_outage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            target = root / "checkout"
            subprocess.run(
                ["git", "init", "--bare", "-q", "--initial-branch=main", str(remote)],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "clone", "-q", str(remote), str(target)],
                check=True,
                capture_output=True,
                text=True,
            )
            for key, value in (
                ("user.name", "Test"),
                ("user.email", "test@example.com"),
            ):
                subprocess.run(
                    ["git", "-C", str(target), "config", key, value],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            (target / "README.md").write_text("initial\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(target), "add", "README.md"],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "-C", str(target), "commit", "-qm", "initial"],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "-C", str(target), "push", "-q", "origin", "main"],
                check=True,
                capture_output=True,
                text=True,
            )
            base = subprocess.run(
                ["git", "-C", str(target), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            (target / "notes.md").write_text("pending\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(target), "add", "notes.md"],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "-C", str(target), "commit", "-qm", "pending"],
                check=True,
                capture_output=True,
                text=True,
            )
            head = subprocess.run(
                ["git", "-C", str(target), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            hat_repository._set_pending_sync(
                target,
                head=head,
                base=base,
                branch="main",
            )
            context = mock.Mock()
            context.client.post_json = mock.AsyncMock(return_value={"verified": True})
            context.computer_path.side_effect = lambda value: f"/hapi/v1/{value}"
            original_run_git = hat_repository._run_git
            fetch_failed = False

            def fail_first_fetch(args, **kwargs):
                nonlocal fetch_failed
                if args and args[0] == "fetch" and not fetch_failed:
                    fetch_failed = True
                    return subprocess.CompletedProcess(
                        args=args,
                        returncode=1,
                        stdout="",
                        stderr="offline",
                    )
                return original_run_git(args, **kwargs)

            with mock.patch.object(
                hat_repository,
                "_run_git",
                side_effect=fail_first_fetch,
            ):
                with self.assertRaisesRegex(
                    hat_repository.HatRepositoryError,
                    "pending Hat sync could not reach",
                ):
                    await hat_repository._resume_pending_sync(
                        context,
                        target=target,
                        handle="itsfaridkia/hats/example-hat",
                        grant_id=GRANT_ID,
                        owner="tinyhat-ai",
                        repo="example-hat",
                        branch="main",
                    )

            self.assertEqual(hat_repository._pending_sync(target), (head, base, "main"))
            result = await hat_repository._resume_pending_sync(
                context,
                target=target,
                handle="itsfaridkia/hats/example-hat",
                grant_id=GRANT_ID,
                owner="tinyhat-ai",
                repo="example-hat",
                branch="main",
            )

            self.assertIsNotNone(result)
            self.assertEqual(result["head_sha"], head)
            self.assertEqual(result["synced_paths"], ["notes.md"])
            self.assertIsNone(hat_repository._pending_sync(target))
            self.assertEqual(
                subprocess.run(
                    ["git", "--git-dir", str(remote), "rev-parse", "main"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                head,
            )


if __name__ == "__main__":
    unittest.main()
