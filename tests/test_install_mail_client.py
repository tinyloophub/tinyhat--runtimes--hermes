"""Execute installer branches in an isolated filesystem with fake package tools."""

import os
import subprocess
import tempfile
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
KEY = "35BAA0B33E9EB396F59CA838C0BA5CE6DC6315A3"


class InstallMailClientTests(TestCase):
    def run_installer(self, *, fingerprint=KEY, candidate="155.0.1", skip=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        for name in ("sources.list.d", "preferences.d"):
            (root / "etc/apt" / name).mkdir(parents=True)
        source = (ROOT / "hermes_runtime/install_mail_client.sh").read_text()
        # Remap only filesystem destinations; execute the unchanged branch logic.
        for path in ("/usr/lib/thunderbird", "/usr/local/bin", "/etc/apt"):
            source = source.replace(path, str(root) + path)
        script = root / "install.sh"
        script.write_text(source)
        (root / "tinyhat-mail.cfg").write_text((ROOT / "hermes_runtime/tinyhat-mail.cfg").read_text())
        tools = {
            "uname": 'echo Linux',
            "id": 'echo 0',
            "apt-get": '''printf '%s\\n' "$*" >> "$FIXTURE_ROOT/apt.log"
case "$*" in *'install -y --no-install-recommends thunderbird')
mkdir -p "$FIXTURE_ROOT/usr/lib/thunderbird"
printf '#!/bin/sh\\nexit 0\\n' > "$FIXTURE_ROOT/usr/lib/thunderbird/thunderbird"
chmod +x "$FIXTURE_ROOT/usr/lib/thunderbird/thunderbird";; esac''',
            "curl": '''while [ "$#" -gt 0 ]; do
if [ "$1" = -o ]; then shift; printf fixture > "$1"; exit 0; fi
shift
done
exit 1''',
            "gpg": 'printf "fpr:::::::::%s:\\n" "$FIXTURE_FINGERPRINT"',
            "apt-cache": 'printf "Candidate: %s\\n" "$FIXTURE_CANDIDATE"',
        }
        for name, body in tools.items():
            tool = bin_dir / name
            tool.write_text("#!/bin/sh\n" + body + "\n")
            tool.chmod(0o755)
        env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ['PATH'],
               "FIXTURE_ROOT": str(root), "FIXTURE_FINGERPRINT": fingerprint,
               "FIXTURE_CANDIDATE": candidate, "TINYHAT_SKIP_DESKTOP_APPS": "1" if skip else "0"}
        result = subprocess.run(["bash", str(script)], env=env, text=True, capture_output=True, timeout=20)
        return root, result

    def test_opt_out_does_not_install_or_create_launcher(self):
        root, result = self.run_installer(skip=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((root / "apt.log").exists())
        self.assertFalse((root / "usr/local/bin/tinyhat-mail").exists())

    def test_mismatched_signing_key_aborts_before_package_install(self):
        root, result = self.run_installer(fingerprint="UNTRUSTED")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Mozilla signing key mismatch", result.stderr)
        self.assertNotIn("--no-install-recommends thunderbird", (root / "apt.log").read_text())
        self.assertFalse((root / "etc/apt/keyrings/packages.mozilla.org.asc").exists())

    def test_snap_candidate_is_rejected(self):
        root, result = self.run_installer(candidate="2:1snap1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing the Snap stub", result.stderr)
        self.assertFalse((root / "usr/local/bin/tinyhat-mail").exists())

    def test_native_install_creates_private_launcher_and_autoconfig(self):
        root, result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        launcher = root / "usr/local/bin/tinyhat-mail"
        self.assertTrue(os.access(launcher, os.X_OK))
        self.assertIn('TINYHAT_MAIL_SETTINGS_FILE="$HOME/.config/tinyhat/mail/settings.json"', launcher.read_text())
        self.assertIn('general.config.obscure_value", 0', (root / "usr/lib/thunderbird/defaults/pref/tinyhat-mail.js").read_text())
        self.assertEqual((root / "usr/lib/thunderbird/tinyhat-mail.cfg").read_text(), (ROOT / "hermes_runtime/tinyhat-mail.cfg").read_text())
