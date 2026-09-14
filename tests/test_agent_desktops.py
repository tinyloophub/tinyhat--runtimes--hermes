"""Usage: python -m unittest discover -s tests -p 'test_agent_desktops.py'."""

import os
import subprocess
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from hermes_runtime import agent_desktops


class AgentDesktopTests(TestCase):
    def test_reassignment_replaces_only_tinyhat_managed_app_icons(self):
        with (
            tempfile.TemporaryDirectory() as path,
            patch.object(agent_desktops.shutil, "which", return_value="/usr/bin/app"),
        ):
            home = Path(path)
            agent_desktops.write_launchers(home, "codex")
            agent_desktops.write_launchers(home, "claude_code")
            self.assertFalse((home / "Desktop/ChatGPT.desktop").exists())
            self.assertIn(
                "tinyhat-claude-desktop", (home / "Desktop/Claude.desktop").read_text()
            )
            custom = home / "Desktop/ChatGPT.desktop"
            custom.write_text("my own shortcut")
            agent_desktops.write_launchers(home, "hermes")
            self.assertFalse((home / "Desktop/Claude.desktop").exists())
            self.assertEqual(custom.read_text(), "my own shortcut")
            agent_desktops.write_launchers(home, "codex")
            agent_desktops.write_launchers(home, "openclaw")
            self.assertFalse(custom.exists())
            custom.write_bytes(b"my own non-UTF-8 shortcut: \xff")
            agent_desktops.write_launchers(home, "openclaw")
            self.assertEqual(custom.read_bytes(), b"my own non-UTF-8 shortcut: \xff")

    def test_install_has_no_implicit_shortcuts_and_repair_uses_explicit_system(self):
        with (
            patch.object(subprocess, "run") as install,
            patch.object(agent_desktops, "write_launchers") as launchers,
            patch.object(agent_desktops, "inventory", return_value={}),
        ):
            with patch("sys.argv", ["agent_desktops", "--install"]):
                agent_desktops.main()
            launchers.assert_not_called()
            self.assertTrue(
                install.call_args.args[0][1].endswith("install_coding_agent_apps.sh")
            )
            with patch(
                "sys.argv", ["agent_desktops", "--install", "--system", "claude_code"]
            ):
                agent_desktops.main()
            launchers.assert_called_once_with(Path.home(), "claude_code")

    def test_launchers_disable_sandbox_only_for_root_and_preserve_arguments(self):
        with (
            tempfile.TemporaryDirectory() as path,
            patch.object(agent_desktops.shutil, "which", return_value="/usr/bin/app"),
        ):
            home = Path(path)
            stub = home / "stubs"
            stub.mkdir()
            for system, (command, _, _) in agent_desktops.DESKTOP_APPS.items():
                app = stub / command
                app.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
                app.chmod(0o755)
                agent_desktops.write_launchers(home, system)
                for uid in (0, 1000):
                    identity = stub / "id"
                    identity.write_text(f"#!/bin/sh\necho {uid}\n")
                    identity.chmod(0o755)
                    result = subprocess.run(
                        [
                            str(home / ".local/bin" / ("tinyhat-" + command)),
                            "space in argument",
                        ],
                        env={**os.environ, "PATH": str(stub)},
                        text=True,
                        capture_output=True,
                        check=True,
                    )
                    args = result.stdout.splitlines()
                    self.assertEqual("--no-sandbox" in args, uid == 0)
                    self.assertEqual(args[-1], "space in argument")

    def test_inventory_does_not_launch_apps_or_read_provider_credentials(self):
        with (
            patch.object(
                agent_desktops.shutil,
                "which",
                side_effect=lambda name: (
                    "/usr/bin/chatgpt" if name == "chatgpt" else None
                ),
            ),
            patch.object(
                subprocess,
                "run",
                side_effect=AssertionError("Do not launch or authenticate"),
            ),
        ):
            self.assertEqual(
                agent_desktops.inventory(),
                {"codex": {"installed": True}, "claude_code": {"installed": False}},
            )

    def test_selected_app_launcher_uses_real_desktop_and_preserves_cli(self):
        with (
            tempfile.TemporaryDirectory() as path,
            patch.object(
                agent_desktops.shutil, "which", return_value="/usr/bin/chatgpt"
            ),
        ):
            home = Path(path)
            cli = home / ".local/bin/tinyhat-agent"
            cli.parent.mkdir(parents=True)
            cli.write_text("my CLI customization")
            agent_desktops.write_launchers(home, "codex")
            self.assertEqual(cli.read_text(), "my CLI customization")
            entry = (home / "Desktop/ChatGPT.desktop").read_text()
            self.assertIn("Terminal=false", entry)
            self.assertIn("tinyhat-chatgpt", entry)
            self.assertFalse((home / "Desktop/Claude.desktop").exists())
            launcher = home / ".local/bin/tinyhat-chatgpt"
            self.assertIn("exec chatgpt ", launcher.read_text())
            self.assertEqual(launcher.stat().st_mode & 0o777, 0o700)
            subprocess.run(["sh", "-n", str(launcher)], check=True)

    def test_missing_app_and_unknown_system_do_not_make_fake_shortcuts(self):
        with (
            tempfile.TemporaryDirectory() as path,
            patch.object(agent_desktops.shutil, "which", return_value=None),
        ):
            for system in ("codex", "claude_code", "hermes", "anything;malicious"):
                agent_desktops.write_launchers(Path(path), system)
            self.assertEqual(list(Path(path).iterdir()), [])
