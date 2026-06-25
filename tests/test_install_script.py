"""Smoke tests for the public install.sh bootstrap surface."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[1]


class InstallScriptTests(TestCase):
    def test_install_from_local_source_writes_launcher_and_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            prefix = base / "prefix"
            state_dir = base / "state"
            ref = "v0.20.0-dev.20260625T173000Z.install-test"

            subprocess.run(
                [
                    "bash",
                    str(ROOT / "install.sh"),
                    "--source-dir",
                    str(ROOT),
                    "--prefix",
                    str(prefix),
                    "--state-dir",
                    str(state_dir),
                    "--ref",
                    ref,
                    "--no-systemd",
                ],
                check=True,
                text=True,
                capture_output=True,
            )

            self.assertEqual((prefix / "INSTALL_REF").read_text().strip(), ref)
            self.assertTrue((prefix / "hermes_runtime" / "main.py").is_file())
            self.assertTrue((prefix / "bin" / "tinyhat-hermes-runtime").is_file())
            self.assertTrue((state_dir).is_dir())


if __name__ == "__main__":
    import unittest

    unittest.main()
