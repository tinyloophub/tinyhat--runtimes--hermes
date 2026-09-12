"""Credential safety and non-destructive mail-profile reconciliation."""

import json
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from hermes_runtime import desktop_mail


VALUES = {
    "TINYHAT_EMAIL_CHANNEL_ENABLED": "1",
    "TINYHAT_MAILBOX_ADDRESS": "agent@example.test",
    "TINYHAT_MAILBOX_USERNAME": "agent@example.test",
    "TINYHAT_MAILBOX_PASSWORD": 'private-"\\\n-value',
    "TINYHAT_MAILBOX_JMAP_URL": "https://mail.example.test/jmap",
}


class DesktopMailTests(TestCase):
    def test_assignment_and_rename_keep_profile_and_secrets_private(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            desktop_mail.shutil, "which", return_value="/usr/bin/thunderbird"
        ):
            home = Path(temporary)
            self.assertTrue(desktop_mail.configure(VALUES, home=home))
            root = home / ".config/tinyhat/mail"
            config = root / "settings.json"
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(json.loads(config.read_text())["password"], VALUES["TINYHAT_MAILBOX_PASSWORD"])
            shortcut = (home / "Desktop/Tinyhat Mail.desktop").read_text()
            self.assertNotIn(VALUES["TINYHAT_MAILBOX_PASSWORD"], shortcut)
            self.assertNotIn(VALUES["TINYHAT_MAILBOX_ADDRESS"], shortcut)
            draft = root / "profile/draft.eml"
            draft.write_text("An unsent draft")
            self.assertFalse(desktop_mail.configure(VALUES, home=home))
            renamed = {**VALUES, "TINYHAT_MAILBOX_ADDRESS": "renamed@example.test", "TINYHAT_MAILBOX_USERNAME": "renamed@example.test"}
            self.assertTrue(desktop_mail.configure(renamed, home=home))
            self.assertEqual(draft.read_text(), "An unsent draft")
            self.assertEqual(json.loads(config.read_text())["address"], "renamed@example.test")

    def test_symlink_cannot_redirect_credentials_into_a_project(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            desktop_mail.shutil, "which", return_value="/usr/bin/thunderbird"
        ):
            home = Path(temporary)
            project = home / "project"
            project.mkdir()
            (home / ".config").symlink_to(project, target_is_directory=True)
            with self.assertRaises(ValueError):
                desktop_mail.configure(VALUES, home=home)
            self.assertEqual(list(project.iterdir()), [])

    def test_invalid_transport_never_writes_credentials(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            desktop_mail.shutil, "which", return_value="/usr/bin/thunderbird"
        ):
            for url in ("http://mail.example.test/jmap", "https://user:secret@mail.example.test/jmap", ""):
                with self.assertRaises(ValueError):
                    desktop_mail.configure({**VALUES, "TINYHAT_MAILBOX_JMAP_URL": url}, home=Path(temporary))
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_old_image_does_not_install_packages_during_assignment(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            desktop_mail.shutil, "which", return_value=None
        ):
            self.assertFalse(desktop_mail.configure(VALUES, home=Path(temporary)))
            self.assertEqual(list(Path(temporary).iterdir()), [])
