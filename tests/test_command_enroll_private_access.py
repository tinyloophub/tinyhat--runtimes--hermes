"""Tests for private Computer network enrollment."""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from hermes_runtime import private_access
from hermes_runtime.commands import run_command


class FakePlatform:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.gets: list[str] = []

    async def get_json(self, path: str) -> dict[str, object]:
        self.gets.append(path)
        return self.payload


class PrivateAccessCommandTests(TestCase):
    def test_command_pulls_secret_directly_and_returns_only_safe_state(self) -> None:
        platform = FakePlatform(
            {
                "provider": "tailscale",
                "tailscale_auth_key": "tskey-auth-secret",
                "tailscale_node_name": "tinyhat-computer-123",
            }
        )
        ctx = SimpleNamespace(platform=platform, platform_auth="gcloud")
        enrollment = {
            "provider": "tailscale",
            "state": "ready",
            "node_name": "tinyhat-computer-123",
        }
        report = {
            **enrollment,
            "tailnet_ip": "100.101.102.103",
            "diagnostic_code": "ready",
        }

        with (
            patch(
                "hermes_runtime.commands.enroll_private_access.private_access.enroll_from_payload",
                return_value=enrollment,
            ) as enroll,
            patch(
                "hermes_runtime.commands.enroll_private_access.private_access.private_access_report",
                return_value=report,
            ),
        ):
            result = asyncio.run(
                run_command(
                    ctx,
                    {
                        "kind": "enroll_private_access",
                        "spec": {"reason": "desktop_access"},
                    },
                )
            )

        self.assertEqual(
            platform.gets,
            ["/hapi/v1/computers/me/private-access/enrollment"],
        )
        enroll.assert_called_once_with(platform.payload)
        self.assertEqual(result["private_access"]["tailnet_ip"], "100.101.102.103")
        self.assertNotIn("tailscale_auth_key", str(result))

    def test_enrollment_uses_auth_key_file_then_deletes_it(self) -> None:
        calls: list[list[str]] = []

        def runner(
            args: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            status_path = Path(tmp) / "status.json"
            with (
                patch.dict(
                    os.environ,
                    {"TINYHAT_PRIVATE_ACCESS_STATUS_PATH": str(status_path)},
                    clear=False,
                ),
                patch(
                    "hermes_runtime.private_access.shutil.which",
                    side_effect=lambda name: f"/usr/bin/{name}",
                ),
            ):
                result = private_access.enroll_from_payload(
                    {
                        "provider": "tailscale",
                        "tailscale_auth_key": "tskey-auth-secret",
                        "tailscale_node_name": "tinyhat-computer-123",
                        "tailscale_tags": ["tag:tinyhat-computer"],
                        "tailscale_ssh": True,
                    },
                    runner=runner,
                )

            up = next(args for args in calls if args[:2] == ["tailscale", "up"])
            auth_arg = next(arg for arg in up if arg.startswith("--auth-key=file:"))
            auth_path = Path(auth_arg.removeprefix("--auth-key=file:"))
            self.assertFalse(auth_path.exists())
            self.assertNotIn("tskey-auth-secret", " ".join(up))
            self.assertIn("--ssh", up)
            self.assertIn("--advertise-tags=tag:tinyhat-computer", up)
            self.assertEqual(result["state"], "ready")

    def test_command_fails_when_tailscale_does_not_become_ready(self) -> None:
        platform = FakePlatform({"provider": "tailscale"})
        ctx = SimpleNamespace(platform=platform, platform_auth="gcloud")
        with (
            patch(
                "hermes_runtime.commands.enroll_private_access.private_access.enroll_from_payload",
                return_value={"state": "config_missing"},
            ),
            patch(
                "hermes_runtime.commands.enroll_private_access.private_access.private_access_report",
                return_value={"state": "unreachable", "diagnostic": "not running"},
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "not running"):
                asyncio.run(
                    run_command(ctx, {"kind": "enroll_private_access", "spec": {}})
                )

    def test_startup_restores_userspace_daemon_without_reenrolling(self) -> None:
        calls: list[list[str]] = []
        starts: list[list[str]] = []
        status_calls = 0

        def runner(
            args: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            nonlocal status_calls
            calls.append(list(args))
            if args[:2] == ["tailscale", "status"]:
                status_calls += 1
                if status_calls == 1:
                    return subprocess.CompletedProcess(
                        args, 1, stdout="", stderr="not running"
                    )
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout=(
                        '{"BackendState":"Running","Self":'
                        '{"HostName":"tinyhat-computer-123",'
                        '"Online":true,"TailscaleIPs":["100.101.102.103"]}}'
                    ),
                    stderr="",
                )
            if args[:3] == ["/usr/bin/systemctl", "enable", "--now"]:
                return subprocess.CompletedProcess(
                    args, 1, stdout="", stderr="systemd unavailable"
                )
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        def starter(args: list[str], **_kwargs: object) -> SimpleNamespace:
            starts.append(list(args))
            return SimpleNamespace(pid=123)

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            status_path = base / "private-access" / "status.json"
            status_path.parent.mkdir(parents=True)
            status_path.write_text(
                '{"provider":"tailscale","state":"ready",'
                '"node_name":"tinyhat-computer-123"}\n',
                encoding="utf-8",
            )
            with (
                patch.dict(
                    os.environ,
                    {"TINYHAT_PRIVATE_ACCESS_STATUS_PATH": str(status_path)},
                    clear=False,
                ),
                patch(
                    "hermes_runtime.private_access.shutil.which",
                    side_effect=lambda name: {
                        "tailscale": "/usr/bin/tailscale",
                        "tailscaled": "/usr/sbin/tailscaled",
                        "systemctl": "/usr/bin/systemctl",
                    }.get(name),
                ),
                patch.object(private_access, "TAILSCALE_STATE_DIR", base / "tailscale"),
                patch.object(
                    private_access,
                    "TAILSCALE_SOCKET_PATH",
                    base / "run" / "tailscaled.sock",
                ),
                patch.object(
                    private_access,
                    "TAILSCALE_LOG_PATH",
                    base / "logs" / "tailscaled.log",
                ),
            ):
                result = private_access.restore_private_access_on_startup(
                    runner=runner,
                    starter=starter,
                    sleeper=lambda _seconds: None,
                )

        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["start_mode"], "userspace")
        self.assertEqual(result["tailnet_ip"], "100.101.102.103")
        self.assertGreaterEqual(status_calls, 3)
        self.assertEqual(len(starts), 1)
        self.assertIn("--tun=userspace-networking", starts[0])
        self.assertFalse(any(args[:2] == ["tailscale", "up"] for args in calls))
        self.assertFalse(any(args[:2] == ["tailscale", "logout"] for args in calls))
