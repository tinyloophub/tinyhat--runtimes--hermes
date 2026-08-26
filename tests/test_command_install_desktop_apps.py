"""Focused tests for the desktop-app runtime command and installer."""

from __future__ import annotations

import asyncio
import os
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
                env["PATH"] = f"{bin_dir}{os.pathsep}/usr/bin:/bin"

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


if __name__ == "__main__":
    import unittest

    unittest.main()
