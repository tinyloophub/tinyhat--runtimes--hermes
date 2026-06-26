"""Smoke tests for the public install.sh bootstrap surface."""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
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
            self.assertEqual((state_dir / "current" / "VERSION").read_text().strip(), ref)
            self.assertTrue((prefix / "hermes_runtime" / "main.py").is_file())
            self.assertTrue(
                (prefix / "tinyhat_hermes_runtime_bootstrap.py").is_file()
            )
            self.assertTrue((prefix / "bin" / "tinyhat-hermes-runtime").is_file())
            self.assertIn(
                "tinyhat_hermes_runtime_bootstrap.py",
                (prefix / "bin" / "tinyhat-hermes-runtime").read_text(
                    encoding="utf-8"
                ),
            )
            self.assertTrue((state_dir).is_dir())
            expected_sha = subprocess.run(
                ["git", "-C", str(ROOT), "rev-parse", "--verify", "HEAD"],
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            self.assertEqual(
                (state_dir / "current" / "COMMIT_SHA").read_text().strip(),
                expected_sha,
            )

    def test_installer_documents_foreground_runtime_mode(self) -> None:
        script = (ROOT / "install.sh").read_text(encoding="utf-8")

        self.assertIn("--run-foreground", script)
        self.assertIn("run_runtime_foreground()", script)
        self.assertIn("tinyhat-hermes-runtime exited with status", script)

        help_result = subprocess.run(
            ["bash", str(ROOT / "install.sh"), "--help"],
            check=True,
            text=True,
            capture_output=True,
        )

        self.assertIn("--run-foreground", help_result.stdout)
        self.assertIn("local Docker", help_result.stdout)

    def test_run_foreground_restarts_and_forwards_stop_signal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "source"
            prefix = base / "prefix"
            state_dir = base / "state"
            counter = base / "counter"
            stopped = base / "stopped"
            pid_file = base / "child.pid"
            source.joinpath("hermes_runtime").mkdir(parents=True)
            source.joinpath("hermes_runtime", "__init__.py").write_text(
                "", encoding="utf-8"
            )
            source.joinpath("tinyhat_hermes_runtime_bootstrap.py").write_text(
                """
from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

counter = Path(os.environ["STUB_COUNTER"])
stopped = Path(os.environ["STUB_STOPPED"])
pid_file = Path(os.environ["STUB_PID"])
count = int(counter.read_text(encoding="utf-8")) if counter.exists() else 0
counter.write_text(str(count + 1), encoding="utf-8")
pid_file.write_text(str(os.getpid()), encoding="utf-8")

def handle_stop(signum, frame):
    stopped.write_text(f"stopped:{signum}", encoding="utf-8")
    raise SystemExit(0)

signal.signal(signal.SIGTERM, handle_stop)
signal.signal(signal.SIGINT, handle_stop)
if count == 0:
    raise SystemExit(23)
while True:
    time.sleep(0.1)
""".strip()
                + "\n",
                encoding="utf-8",
            )
            env = {
                "PATH": os.environ["PATH"],
                "STUB_COUNTER": str(counter),
                "STUB_STOPPED": str(stopped),
                "STUB_PID": str(pid_file),
            }
            proc = subprocess.Popen(
                [
                    "bash",
                    str(ROOT / "install.sh"),
                    "--source-dir",
                    str(source),
                    "--prefix",
                    str(prefix),
                    "--state-dir",
                    str(state_dir),
                    "--ref",
                    "v0.20.0-dev.foreground-test",
                    "--run-foreground",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            try:
                deadline = time.time() + 10
                while time.time() < deadline:
                    if counter.exists() and int(counter.read_text()) >= 2:
                        break
                    time.sleep(0.05)
                self.assertTrue(counter.exists(), "runtime stub did not start")
                self.assertGreaterEqual(int(counter.read_text()), 2)
                proc.terminate()
                stdout, stderr = proc.communicate(timeout=5)
            finally:
                if proc.poll() is None:
                    proc.kill()
                if pid_file.exists():
                    subprocess.run(
                        ["kill", "-TERM", pid_file.read_text().strip()],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )

            self.assertEqual(proc.returncode, 0)
            self.assertIn(
                "install.sh: running tinyhat-hermes-runtime in foreground restart mode",
                stdout,
            )
            self.assertIn(
                "tinyhat-hermes-runtime exited with status 23; restarting in 2s",
                stderr,
            )
            self.assertTrue(stopped.exists(), "foreground mode did not stop child")
            self.assertNotIn("systemd", stdout + stderr)


if __name__ == "__main__":
    import unittest

    unittest.main()
