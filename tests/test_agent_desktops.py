"""Usage: python -m unittest discover -s tests -p 'test_agent_desktops.py'."""

import subprocess
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from hermes_runtime import agent_desktops


class AgentDesktopTests(TestCase):
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
