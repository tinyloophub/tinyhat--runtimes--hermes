"""Smoke tests for the public install.sh bootstrap surface."""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[1]


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _env_with_fake_codex(bin_dir: Path) -> dict[str, str]:
    bin_dir.mkdir(parents=True, exist_ok=True)
    _write_executable(
        bin_dir / "codex",
        "#!/usr/bin/env bash\nprintf 'codex-cli test\\n'\n",
    )
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    return env


class InstallScriptTests(TestCase):
    def test_install_from_local_source_writes_launcher_and_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            prefix = base / "prefix"
            state_dir = base / "state"
            env = _env_with_fake_codex(base / "fake-bin")
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
                env=env,
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
            self.assertIn(
                "/usr/local/bin:/usr/bin:/bin",
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

    def test_installer_installs_codex_cli_from_npm_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            prefix = base / "prefix"
            state_dir = base / "state"
            bin_dir = base / "fake-bin"
            bin_dir.mkdir()
            npm_args = base / "npm-args.txt"
            fake_codex = bin_dir / "codex"
            fake_npm = bin_dir / "npm"
            _write_executable(
                bin_dir / "node",
                "#!/usr/bin/env bash\nprintf 'v22.12.0\\n'\n",
            )
            _write_executable(
                fake_npm,
                f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$@" > {npm_args}
if [[ "$1" != "install" || "$2" != "-g" ]]; then
  exit 22
fi
cat > {fake_codex} <<'CODEX'
#!/usr/bin/env bash
printf 'codex-cli installed-by-test\\n'
CODEX
chmod +x {fake_codex}
""",
            )
            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}{os.pathsep}/usr/bin:/bin:/usr/sbin:/sbin"
            env["TINYHAT_CODEX_NPM_PACKAGE"] = "@openai/codex"

            result = subprocess.run(
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
                    "v0.20.0-dev.codex-cli-install-test",
                    "--no-systemd",
                ],
                check=True,
                text=True,
                capture_output=True,
                env=env,
            )

            self.assertTrue(fake_codex.is_file())
            self.assertEqual(npm_args.read_text(encoding="utf-8").splitlines(), [
                "install",
                "-g",
                "@openai/codex",
            ])
            self.assertIn(
                "install.sh: installing Codex CLI from npm package @openai/codex",
                result.stdout,
            )
            self.assertIn("codex-cli installed-by-test", result.stdout)

    def test_installer_can_skip_codex_cli_for_local_dev(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            prefix = base / "prefix"
            state_dir = base / "state"
            env = dict(os.environ)
            env["TINYHAT_SKIP_CODEX_CLI"] = "1"

            result = subprocess.run(
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
                    "v0.20.0-dev.codex-cli-skip-test",
                    "--no-systemd",
                ],
                check=True,
                text=True,
                capture_output=True,
                env=env,
            )

            self.assertIn(
                "install.sh: skipping Codex CLI install because TINYHAT_SKIP_CODEX_CLI is set",
                result.stdout,
            )
            self.assertEqual(
                (state_dir / "current" / "VERSION").read_text().strip(),
                "v0.20.0-dev.codex-cli-skip-test",
            )

    def test_installer_uses_nodesource_when_node_is_missing_on_apt_linux(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            prefix = base / "prefix"
            state_dir = base / "state"
            bin_dir = base / "fake-bin"
            bin_dir.mkdir()
            apt_args = base / "apt-args.txt"
            curl_args = base / "curl-args.txt"
            npm_args = base / "npm-args.txt"
            fake_codex = bin_dir / "codex"
            fake_npm = bin_dir / "npm"
            fake_node = bin_dir / "node"
            _write_executable(bin_dir / "uname", "#!/bin/sh\nprintf 'Linux\\n'\n")
            _write_executable(bin_dir / "id", "#!/bin/sh\nprintf '0\\n'\n")
            _write_executable(
                bin_dir / "curl",
                f"#!/bin/sh\nprintf '%s\\n' \"$@\" >> {curl_args}\nprintf 'true\\n'\n",
            )
            _write_executable(
                bin_dir / "apt-get",
                f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> {apt_args}
if [[ "$1" == "install" && " $* " == *" nodejs "* ]]; then
  cat > {fake_node} <<'NODE'
#!/usr/bin/env bash
printf 'v22.12.0\\n'
NODE
  chmod +x {fake_node}
  cat > {fake_npm} <<'NPM'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$@" > {npm_args}
if [[ "$1" == "install" && "$2" == "-g" ]]; then
  cat > {fake_codex} <<'CODEX'
#!/usr/bin/env bash
printf 'codex-cli installed-by-nodesource-test\\n'
CODEX
  chmod +x {fake_codex}
fi
NPM
  chmod +x {fake_npm}
fi
""",
            )
            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}{os.pathsep}/usr/bin:/bin:/usr/sbin:/sbin"

            result = subprocess.run(
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
                    "v0.20.0-dev.nodesource-test",
                    "--no-systemd",
                ],
                check=True,
                text=True,
                capture_output=True,
                env=env,
            )

            self.assertIn("install.sh: installing Node.js 22.x", result.stdout)
            self.assertIn(
                "https://deb.nodesource.com/setup_22.x",
                curl_args.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "install -y ca-certificates curl gnupg",
                apt_args.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "install -y nodejs",
                apt_args.read_text(encoding="utf-8"),
            )
            self.assertEqual(npm_args.read_text(encoding="utf-8").splitlines(), [
                "install",
                "-g",
                "@openai/codex",
            ])
            self.assertIn("codex-cli installed-by-nodesource-test", result.stdout)

    def test_installer_documents_foreground_runtime_mode(self) -> None:
        script = (ROOT / "install.sh").read_text(encoding="utf-8")

        self.assertIn("--run-foreground", script)
        self.assertIn("run_runtime_foreground()", script)
        self.assertIn("tinyhat-hermes-runtime exited with status", script)
        self.assertIn("@openai/codex", script)
        self.assertIn("TINYHAT_SKIP_CODEX_CLI", script)
        self.assertIn("setup_${codex_node_major}.x", script)

        help_result = subprocess.run(
            ["bash", str(ROOT / "install.sh"), "--help"],
            check=True,
            text=True,
            capture_output=True,
        )

        self.assertIn("--run-foreground", help_result.stdout)
        self.assertIn("local Docker", help_result.stdout)
        self.assertIn("Codex CLI", help_result.stdout)
        self.assertIn("TINYHAT_SKIP_CODEX_CLI", help_result.stdout)
        self.assertIn("TINYHAT_CODEX_NODE_MAJOR", help_result.stdout)

    def _run_foreground_until_signal(
        self,
        stop_signal: signal.Signals,
    ) -> dict[str, str]:
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
                **_env_with_fake_codex(base / "fake-bin"),
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
                proc.send_signal(stop_signal)
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
            return {
                "stdout": stdout,
                "stderr": stderr,
                "stopped": stopped.read_text(encoding="utf-8"),
            }

    def test_run_foreground_restarts_and_forwards_term_signal(self) -> None:
        result = self._run_foreground_until_signal(signal.SIGTERM)

        self.assertEqual(result["stopped"], f"stopped:{signal.SIGTERM.value}")

    def test_run_foreground_forwards_int_signal_as_int(self) -> None:
        result = self._run_foreground_until_signal(signal.SIGINT)

        self.assertEqual(result["stopped"], f"stopped:{signal.SIGINT.value}")


if __name__ == "__main__":
    import unittest

    unittest.main()
