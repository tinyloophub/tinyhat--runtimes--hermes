"""Focused tests for the desktop-app runtime command and installer."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hermes_runtime.commands import run_command  # noqa: E402


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


class InstallDesktopAppsCommandTests(TestCase):
    def test_installer_configures_resource_conscious_stable_browser(self) -> None:
        installer = (
            ROOT / "hermes_runtime" / "install_desktop_apps.sh"
        ).read_text(encoding="utf-8")

        self.assertIn("google-chrome-stable_current_${architecture}.deb", installer)
        self.assertIn("TINYHAT_CHROME_RENDERER_PROCESS_LIMIT:-4", installer)
        self.assertIn("TINYHAT_CHROME_DISABLE_GPU:-1", installer)
        self.assertIn("--disable-background-mode", installer)
        self.assertIn("gpu_args+=(--disable-gpu)", installer)
        self.assertIn("--renderer-process-limit=", installer)
        self.assertNotIn("google-chrome-canary", installer)

    def test_shared_installer_reports_intentional_chrome_skip_honestly(self) -> None:
        installer = ROOT / "hermes_runtime" / "install_desktop_apps.sh"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bin_dir = base / "bin"
            bin_dir.mkdir()
            for utility in ("bash", "cat", "chmod", "head", "tr"):
                source = shutil.which(utility)
                self.assertIsNotNone(source, utility)
                (bin_dir / utility).symlink_to(str(source))
            _write_executable(
                bin_dir / "thunar",
                "#!/usr/bin/env bash\nprintf 'Thunar 4.18.4\\n'\n",
            )
            env = dict(os.environ)
            env["PATH"] = str(bin_dir)
            env["TINYHAT_SKIP_GOOGLE_CHROME"] = "1"
            env["TINYHAT_BROWSER_LAUNCHER_PATH"] = str(base / "tinyhat-browser")

            result = subprocess.run(
                ["bash", str(installer)],
                check=True,
                text=True,
                capture_output=True,
                env=env,
            )

        self.assertIn("Google Chrome installation skipped", result.stdout)
        self.assertNotIn("Google Chrome is already installed", result.stdout)

    def test_browser_launcher_defaults_to_software_rendering_with_opt_out(
        self,
    ) -> None:
        installer = ROOT / "hermes_runtime" / "install_desktop_apps.sh"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bin_dir = base / "bin"
            bin_dir.mkdir()
            chrome_args = base / "chrome-args.txt"
            for utility in ("bash", "cat", "chmod", "head", "tr"):
                source = shutil.which(utility)
                self.assertIsNotNone(source, utility)
                (bin_dir / utility).symlink_to(str(source))
            _write_executable(
                bin_dir / "google-chrome-stable",
                f"""#!/usr/bin/env bash
if [[ "${{1:-}}" == "--version" ]]; then
  printf 'Google Chrome 152.0.7977.64\\n'
  exit 0
fi
printf '%s\\n' "$@" > {chrome_args}
""",
            )
            _write_executable(
                bin_dir / "thunar",
                "#!/usr/bin/env bash\nprintf 'Thunar 4.18.4\\n'\n",
            )
            env = dict(os.environ)
            env["PATH"] = str(bin_dir)
            launcher = base / "tinyhat-browser"
            env["TINYHAT_BROWSER_LAUNCHER_PATH"] = str(launcher)

            subprocess.run(
                ["bash", str(installer)],
                check=True,
                text=True,
                capture_output=True,
                env=env,
            )
            subprocess.run(
                ["bash", str(launcher)],
                check=True,
                env=env,
            )
            self.assertIn(
                "--disable-gpu", chrome_args.read_text(encoding="utf-8").splitlines()
            )

            env["TINYHAT_CHROME_DISABLE_GPU"] = "0"
            subprocess.run(
                ["bash", str(launcher)],
                check=True,
                env=env,
            )
            self.assertNotIn(
                "--disable-gpu", chrome_args.read_text(encoding="utf-8").splitlines()
            )

    def test_shared_installer_explains_non_root_skip_option(self) -> None:
        installer = (
            ROOT / "hermes_runtime" / "install_desktop_apps.sh"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "installation requires root; set TINYHAT_SKIP_DESKTOP_APPS=1 to skip it",
            installer,
        )

    def test_command_uses_shared_installer_and_reports_change(self) -> None:
        missing = {"installed": False, "binary": None, "version": None}
        before = {"chrome": missing, "thunar": missing}
        after = {
            "chrome": {
                "installed": True,
                "binary": "/usr/bin/google-chrome-stable",
                "version": "Google Chrome 152.0.7977.64",
            },
            "thunar": {
                "installed": True,
                "binary": "/usr/bin/thunar",
                "version": "Thunar 4.18.4",
            },
        }
        with (
            patch(
                "hermes_runtime.commands.install_desktop_apps._desktop_status",
                AsyncMock(side_effect=[before, after]),
            ),
            patch(
                "hermes_runtime.commands.install_desktop_apps._supported",
                return_value=True,
            ),
            patch(
                "hermes_runtime.commands.install_desktop_apps.run_process",
                AsyncMock(return_value={"ok": True, "returncode": 0}),
            ) as install,
        ):
            result = asyncio.run(
                run_command(SimpleNamespace(), {"kind": "install_desktop_apps"})
            )

        install.assert_awaited_once()
        argv = install.await_args.args[0]
        self.assertEqual(argv[0], "bash")
        self.assertTrue(argv[1].endswith("hermes_runtime/install_desktop_apps.sh"))
        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        self.assertTrue(result["installed_after"])
        self.assertEqual(
            result["after"]["chrome"]["version"],
            "Google Chrome 152.0.7977.64",
        )
        self.assertEqual(result["after"]["thunar"]["version"], "Thunar 4.18.4")

    def test_command_is_idempotent_when_chrome_is_present(self) -> None:
        status = {
            "chrome": {
                "installed": True,
                "binary": "/usr/bin/google-chrome-stable",
                "version": "Google Chrome 152.0.7977.64",
            },
            "thunar": {
                "installed": True,
                "binary": "/usr/bin/thunar",
                "version": "Thunar 4.18.4",
            },
        }
        with (
            patch(
                "hermes_runtime.commands.install_desktop_apps._desktop_status",
                AsyncMock(side_effect=[status, status]),
            ),
            patch(
                "hermes_runtime.commands.install_desktop_apps._supported",
                return_value=True,
            ),
            patch(
                "hermes_runtime.commands.install_desktop_apps.run_process",
                AsyncMock(),
            ) as install,
        ):
            result = asyncio.run(
                run_command(SimpleNamespace(), {"kind": "install_desktop_apps"})
            )

        install.assert_not_awaited()
        self.assertTrue(result["ok"])
        self.assertFalse(result["changed"])
        self.assertTrue(result["installed_before"])

    def test_shared_installer_uses_official_deb_for_supported_architectures(self) -> None:
        installer = ROOT / "hermes_runtime" / "install_desktop_apps.sh"
        for architecture in ("amd64", "arm64"):
            with self.subTest(architecture=architecture), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                bin_dir = base / "bin"
                bin_dir.mkdir()
                curl_args = base / "curl-args.txt"
                apt_args = base / "apt-args.txt"
                chrome_bin = bin_dir / "google-chrome-stable"
                thunar_bin = bin_dir / "thunar"

                # Keep a preinstalled runner Chrome out of PATH so this test
                # always exercises the fresh-Computer download path.
                for utility in (
                    "bash",
                    "cat",
                    "chmod",
                    "head",
                    "mktemp",
                    "rm",
                    "tr",
                ):
                    source = shutil.which(utility)
                    self.assertIsNotNone(source, utility)
                    (bin_dir / utility).symlink_to(str(source))

                _write_executable(bin_dir / "uname", "#!/bin/sh\nprintf 'Linux\\n'\n")
                _write_executable(bin_dir / "id", "#!/bin/sh\nprintf '0\\n'\n")
                _write_executable(
                    bin_dir / "dpkg",
                    f"#!/bin/sh\nprintf '{architecture}\\n'\n",
                )
                _write_executable(
                    bin_dir / "curl",
                    f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$@" > {curl_args}
output=""
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "-o" ]]; then
    output="$2"
    shift 2
  else
    shift
  fi
done
printf 'test deb' > "$output"
""",
                )
                _write_executable(
                    bin_dir / "apt-get",
                    f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> {apt_args}
if [[ " $* " == *" install "* ]]; then
  cat > {chrome_bin} <<'CHROME'
#!/usr/bin/env bash
printf 'Google Chrome 152.0.7977.64\\n'
CHROME
  chmod +x {chrome_bin}
  cat > {thunar_bin} <<'THUNAR'
#!/usr/bin/env bash
printf 'Thunar 4.18.4\\n'
THUNAR
  chmod +x {thunar_bin}
fi
""",
                )
                env = dict(os.environ)
                env["PATH"] = str(bin_dir)
                browser_launcher = base / "tinyhat-browser"
                env["TINYHAT_BROWSER_LAUNCHER_PATH"] = str(browser_launcher)

                result = subprocess.run(
                    ["bash", str(installer)],
                    check=True,
                    text=True,
                    capture_output=True,
                    env=env,
                )

                curl_text = curl_args.read_text(encoding="utf-8")
                self.assertIn(
                    f"google-chrome-stable_current_{architecture}.deb",
                    curl_text,
                )
                apt_text = apt_args.read_text(encoding="utf-8")
                self.assertIn("DPkg::Lock::Timeout=300 update", apt_text)
                self.assertIn("install -y --no-install-recommends", apt_text)
                self.assertIn("thunar", apt_text)
                self.assertIn("Google Chrome ready: Google Chrome 152.0.7977.64", result.stdout)
                self.assertIn("Thunar ready: Thunar 4.18.4", result.stdout)
                launcher_text = browser_launcher.read_text(encoding="utf-8")
                self.assertIn("--disable-background-mode", launcher_text)
                self.assertIn("TINYHAT_CHROME_DISABLE_GPU:-1", launcher_text)
                self.assertIn("gpu_args+=(--disable-gpu)", launcher_text)
                self.assertIn("--renderer-process-limit=", launcher_text)


if __name__ == "__main__":
    import unittest

    unittest.main()
