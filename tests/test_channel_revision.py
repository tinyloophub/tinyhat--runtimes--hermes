"""Plugin updates must refresh every dynamically imported channel module."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_runtime.channel_agent.revision import installed_revision


class RevisionTests(unittest.TestCase):
    def test_session_link_and_owner_policy_changes_refresh_the_receiver(self):
        with tempfile.TemporaryDirectory() as directory, patch("hermes_runtime.channel_agent.revision.plugin_dir", return_value=Path(directory)):
            for relative in ("capabilities/channels/sessions.py", "capabilities/mail/owner.py"):
                before = installed_revision()
                path = Path(directory) / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("updated module")
                self.assertNotEqual(installed_revision(), before)


if __name__ == "__main__":
    unittest.main()
