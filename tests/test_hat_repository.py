"""Tests for Computer-local, exact-repository Hat Git access."""

from __future__ import annotations

import io
import os
import subprocess
import tempfile
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


if __name__ == "__main__":
    unittest.main()
