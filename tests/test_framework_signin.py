"""Browser launch readiness and login desktop shortcuts.

Usage: python -m unittest discover -s tests -p test_framework_signin.py -v
"""
import asyncio
import json
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from hermes_runtime import agent_desktops
from hermes_runtime.channel_agent import control, signin

class LoginBrowserTests(unittest.TestCase):
    def test_worker_and_authenticated_retry_can_publish_state_concurrently(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = SimpleNamespace(state_dir=Path(directory))
            with ThreadPoolExecutor(max_workers=3) as workers:
                updates = [workers.submit(signin.save, ctx, "codex", state)
                           for state in ["waiting", "authenticated"] * 20]
                for update in updates:
                    update.result(timeout=5)
            self.assertIn(signin.status(ctx, "codex")["status"], {"waiting", "authenticated"})

    def test_concurrent_provider_choices_preserve_both_login_states(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = SimpleNamespace(state_dir=Path(directory))
            for framework in signin.COMMANDS:
                signin.save(ctx, framework, "waiting")
            with ThreadPoolExecutor(max_workers=4) as workers:
                choices = [workers.submit(signin.select, ctx, framework)
                           for framework in list(signin.COMMANDS) * 20]
                for choice in choices:
                    choice.result(timeout=5)
            self.assertIn(signin.status(ctx)["framework"], signin.COMMANDS)
            for framework in signin.COMMANDS:
                self.assertEqual(signin.status(ctx, framework), {
                    "framework": framework, "status": "waiting",
                })

    def test_cli_hook_and_fallback_share_one_browser_launch(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(signin, "launch_browser") as launch:
            receipt = Path(directory) / "opened"
            with ThreadPoolExecutor(max_workers=2) as workers:
                futures = [workers.submit(signin.open_browser, "https://claude.com/cai/oauth/authorize", receipt) for _ in range(2)]
                for future in futures:
                    future.result(timeout=2)
            launch.assert_called_once()

    def test_minimal_desktop_uses_installed_xset_when_xdpyinfo_is_absent(self):
        with patch.object(signin.shutil, "which", side_effect=lambda name: "/usr/bin/xset" if name == "xset" else None):
            self.assertEqual(signin.display_probe(), ["xset", "q"])

    def test_browser_receipt_requires_a_successful_launch(self):
        with tempfile.TemporaryDirectory() as path, patch.object(signin.shutil, "which", return_value="/usr/bin/browser"), patch.object(signin.subprocess, "Popen") as launch:
            receipt = Path(path) / "opened"
            launch.return_value.wait.return_value = 1
            with self.assertRaises(RuntimeError):
                signin.open_browser("https://auth.openai.com/oauth/authorize", receipt)
            self.assertFalse(receipt.exists())
            launch.return_value.wait.side_effect = subprocess.TimeoutExpired("browser", 1)
            signin.open_browser("https://auth.openai.com/oauth/authorize", receipt)
            self.assertEqual(receipt.read_text(), "opened\n")
            self.assertNotIn("https", receipt.read_text())
            signin.open_browser("https://claude.com/cai/oauth/authorize", receipt)

    def test_desktop_has_both_provider_login_shortcuts_even_without_desktop_apps(self):
        with tempfile.TemporaryDirectory() as path, patch.object(agent_desktops.shutil, "which", side_effect=lambda name: "/usr/bin/"+name if name in {"codex", "claude"} else None):
            home = Path(path)
            agent_desktops.write_launchers(home, "codex")
            for label, framework in [("Codex", "codex"), ("Claude Code", "claude_code")]:
                shortcut = (home / "Desktop" / f"Sign in to {label}.desktop").read_text()
                self.assertIn("Terminal=false", shortcut)
                launcher = home / ".local/bin" / ("tinyhat-signin-" + framework)
                self.assertIn("--desktop --framework " + framework, launcher.read_text())
                self.assertIn("hermes_runtime.channel_agent.signin", launcher.read_text())

class LoginWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_login_is_reused_without_resetting_its_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = SimpleNamespace(state_dir=Path(directory))
            signin.save(ctx, "codex", "waiting")
            with patch.object(signin, "probe", AsyncMock(return_value={"installed": True, "authenticated": False})), patch.object(signin.subprocess, "Popen") as launch:
                result = await signin.start(ctx, "codex")
                self.assertEqual(result["signin"]["status"], "waiting")
                launch.assert_not_called()
                launch.side_effect = lambda *args, **kwargs: signin.save(ctx, "claude_code", "opening")
                result = await signin.start(ctx, "claude_code")
                self.assertEqual(result["signin"], {"framework": "claude_code", "status": "opening"})
                self.assertEqual(result["signins"]["codex"]["status"], "waiting")
                self.assertEqual(launch.call_count, 1)
                # A later update from Codex must not take over the selected login.
                signin.save(ctx, "codex", "authenticated")
                self.assertEqual(signin.status(ctx)["framework"], "claude_code")

    async def test_old_provider_worker_cannot_block_other_provider_after_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = SimpleNamespace(state_dir=Path(directory))
            legacy = control.directory(ctx)
            legacy.mkdir(parents=True)
            (legacy / "signin.json").write_text(json.dumps({
                "framework": "claude_code", "status": "waiting", "updated_at": signin.time.time(),
            }))
            with (legacy / "signin.lock").open("w") as old_lock:
                signin.fcntl.flock(old_lock, signin.fcntl.LOCK_EX | signin.fcntl.LOCK_NB)
                with patch.object(signin, "probe", AsyncMock(return_value={"installed": True, "authenticated": False})), patch.object(signin.subprocess, "Popen", side_effect=lambda *a, **kw: signin.save(ctx, "codex", "opening")) as launch:
                    await signin.start(ctx, "claude_code")
                    launch.assert_not_called()
                    result = await signin.start(ctx, "codex")
                    self.assertEqual(result["signins"]["codex"]["status"], "opening")
                    self.assertEqual(result["signins"]["claude_code"]["status"], "waiting")

    async def test_provider_worker_is_not_blocked_by_other_provider_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = SimpleNamespace(state_dir=Path(directory))
            other = signin.directory(ctx, "claude_code")
            other.mkdir(parents=True)
            with (other / "signin.lock").open("w") as other_lock:
                signin.fcntl.flock(other_lock, signin.fcntl.LOCK_EX | signin.fcntl.LOCK_NB)
                signin.save(ctx, "claude_code", "waiting")
                (other / "signin-browser-opened").write_text("opened")
                provider = SimpleNamespace(returncode=0, stdout=None)
                display = SimpleNamespace(wait=AsyncMock(return_value=0))
                with patch.object(signin, "display_probe", return_value=["xset", "q"]), patch.object(signin.asyncio, "create_subprocess_exec", AsyncMock(side_effect=[display, provider])), patch.object(signin, "probe", AsyncMock(return_value={"authenticated": True})), patch.object(signin, "terminate", AsyncMock()):
                    await signin.run(ctx, "codex", None)
                self.assertEqual(signin.status(ctx, "codex")["status"], "authenticated")
                self.assertEqual(signin.status(ctx, "claude_code")["status"], "waiting")
                self.assertTrue((other / "signin-browser-opened").exists())

    async def test_worker_remains_opening_until_browser_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = SimpleNamespace(state_dir=Path(directory))
            class Process:
                returncode = None
                stdout = None
                async def wait(self):
                    if signin.status(ctx, "codex")["status"] == "opening":
                        self.opening_seen = True
                        (signin.directory(ctx, "codex")/"signin-browser-opened").write_text("opened")
                        raise asyncio.TimeoutError()
                    self.returncode = 0
                    return 0
            provider = Process()
            display = SimpleNamespace(wait=AsyncMock(return_value=0))
            with patch.object(signin, "display_probe", return_value=["xset", "q"]), patch.object(signin.asyncio, "create_subprocess_exec", AsyncMock(side_effect=[display, provider])), patch.object(signin, "probe", AsyncMock(return_value={"installed": True, "authenticated": True})), patch.object(signin, "terminate", AsyncMock()):
                await signin.run(ctx, "codex", None)
            self.assertTrue(provider.opening_seen)
            self.assertEqual(signin.status(ctx, "codex")["status"], "authenticated")
            self.assertNotIn("url", json.loads((signin.directory(ctx, "codex")/"state.json").read_text()))

    async def test_cli_printed_login_fallback_opens_once_without_persisting_url(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "opened"
            stream = asyncio.StreamReader()
            url = "https://claude.ai/oauth/authorize?state=private-value"
            stream.feed_data(("Visit: \x1b]8;;" + url + "\x07Sign in\x1b]8;;\x07\n" + url + "\n").encode())
            stream.feed_eof()
            def open_browser(value, destination):
                self.assertEqual(value, url)
                destination.write_text("opened\n")
            with patch.object(signin, "open_browser", side_effect=open_browser) as opened, patch.object(signin.asyncio, "sleep", AsyncMock()):
                await signin.open_printed_login(stream, receipt)
            opened.assert_called_once()
            self.assertNotIn("private-value", receipt.read_text())

    async def test_login_link_split_across_reads_does_not_need_a_newline_or_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "opened"
            stream = asyncio.StreamReader()
            stream.feed_data(b"Visit https://claude.ai/oauth/authorize?state=par")
            first_read = asyncio.Event()
            original_read = stream.read
            async def read(size):
                value = await original_read(size)
                first_read.set()
                return value
            def open_browser(url, destination):
                self.assertEqual(url, "https://claude.ai/oauth/authorize?state=partial")
                destination.write_text("opened")
            with patch.object(stream, "read", side_effect=read), patch.object(signin, "open_browser", side_effect=open_browser) as opened, patch.object(signin.asyncio, "sleep", AsyncMock()):
                worker = asyncio.create_task(signin.open_printed_login(stream, receipt))
                await asyncio.wait_for(first_read.wait(), 1)
                opened.assert_not_called()
                stream.feed_data(b"tial\x07Paste code here > ")
                stream.feed_eof()
                await asyncio.wait_for(worker, 2)
                opened.assert_called_once()
