"""Run the provider's browser login on the Computer without opening a terminal.

OAuth URLs and CLI output never leave the process or enter runtime telemetry.
The provider CLI owns its credentials and its loopback OAuth callback.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from hermes_runtime.agent_systems import _write_atomic
from hermes_runtime.channel_agent import control
from hermes_runtime.channel_agent.native import child_env, probe, terminate

COMMANDS = {
    "codex": ["codex", "login"],
    "claude_code": ["claude", "auth", "login", "--claudeai"],
}
MODULE = "hermes_runtime.channel_agent.signin"
STATES = {"opening", "waiting", "authenticated", "failed"}


def directory(ctx, framework):
    if framework not in COMMANDS:
        raise ValueError("Choose Codex or Claude Code to sign in.")
    return control.directory(ctx) / ("signin-" + framework)


def select(ctx, framework):
    directory(ctx, framework)  # Validate before persisting a path component.
    root = control.directory(ctx)
    root.mkdir(parents=True, exist_ok=True)
    # Desktop shortcuts and runtime commands may choose different providers
    # concurrently. Serialize the shared pointer, not their login workers.
    with (root / "signin-selected.lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        _write_atomic(root / "signin-selected", framework, 0o600)


def status(ctx, framework=None):
    try:
        if framework is None:
            try:
                framework = (control.directory(ctx) / "signin-selected").read_text()
            except OSError:
                pass
        path = directory(ctx, framework) / "state.json" if framework else None
        # Keep an in-flight pre-upgrade login visible until its old worker exits.
        value = json.loads((path if path and path.exists() else control.directory(ctx) / "signin.json").read_text())
        if framework and value.get("framework") != framework:
            return None
        if value.get("framework") not in COMMANDS or value.get("status") not in STATES:
            return None
        if (
            value["status"] in {"opening", "waiting"}
            and time.time() - float(value.get("updated_at", 0)) > 30
        ):
            value["status"] = "failed"
        return {"framework": value["framework"], "status": value["status"]}
    except (OSError, ValueError, TypeError):
        return None


def save(ctx, framework, state):
    _write_atomic(
        directory(ctx, framework) / "state.json",
        json.dumps(
            {
                "framework": framework,
                "status": state,
                "updated_at": time.time(),
            }
        ),
        0o600,
    )


def desktop_env():
    env = child_env()
    env["DISPLAY"] = os.environ.get("DISPLAY") or ":1"
    env["XAUTHORITY"] = os.environ.get("XAUTHORITY") or str(Path.home() / ".Xauthority")
    return env


def open_browser(url, receipt=None):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname
        not in {
            "auth.openai.com",
            "chatgpt.com",
            "claude.ai",
            "claude.com",
            "console.anthropic.com",
            "platform.claude.com",
        }
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Expected an official provider login URL.")
    if receipt:
        # The CLI hook and printed-link fallback can race. Only one should
        # create the browser window for this login attempt.
        with Path(str(receipt) + ".lock").open("a") as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if Path(receipt).exists():
                return
            launch_browser(url)
            _write_atomic(Path(receipt), "opened\n", 0o600)
    else:
        launch_browser(url)


def launch_browser(url):
    launcher = shutil.which("tinyhat-browser")
    browser = launcher or next(
        (
            shutil.which(name)
            for name in (
                "google-chrome-stable",
                "google-chrome",
                "chromium",
                "chromium-browser",
            )
            if shutil.which(name)
        ),
        None,
    )
    if not browser:
        raise RuntimeError("The Computer browser is not installed.")
    process = subprocess.Popen(
        [browser, *([] if launcher else ["--no-sandbox"]), "--new-window", url],
        env=desktop_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    # Chrome may hand off to an existing window or remain as the browser process.
    # A failed launch must never become a "waiting for credentials" status.
    try:
        code = process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        code = None
    if code not in (None, 0):
        raise RuntimeError("The Computer browser could not open.")


def display_probe():
    # Minimal XFCE images include xset, but not the optional xdpyinfo package.
    if shutil.which("xdpyinfo"):
        return ["xdpyinfo"]
    if shutil.which("xset"):
        return ["xset", "q"]
    raise RuntimeError("The Computer desktop tools are not installed.")


async def open_printed_login(stream, receipt):
    """Handle the official CLI's browser fallback without retaining its output."""
    buffer = ""
    while chunk := await stream.read(4096):
        if receipt.exists():
            buffer = ""
            continue
        buffer = (buffer + chunk.decode("utf-8", "replace"))[-65536:]
        # Both CLIs print their official login link when automatic opening is
        # unavailable. Exclude ANSI/OSC escapes as well as ordinary whitespace.
        # A prompt need not end with a newline. Require a delimiter so a URL
        # split across reads is never opened while incomplete.
        for url in re.findall(r'https://[^\s\x00-\x20\x7f<>"\']+(?=[\s\x00-\x20\x7f<>"\'])', buffer):
            # Give the CLI's BROWSER/xdg-open hook a chance to finish first.
            await asyncio.sleep(1)
            if receipt.exists():
                break
            try:
                await asyncio.to_thread(open_browser, url, receipt)
            except ValueError:
                continue  # Ignore documentation or other non-provider links.


async def start(ctx, framework):
    if framework not in COMMANDS:
        raise ValueError("Choose Codex or Claude Code to sign in.")
    target = await probe(framework)
    if not target["installed"]:
        raise RuntimeError("Install the framework first.")
    select(ctx, framework)
    if target["authenticated"]:
        save(ctx, framework, "authenticated")
        await control.inventory(ctx)
        return control.snapshot(ctx)
    current = status(ctx, framework)
    if current and current["status"] in {"opening", "waiting"}:
        return control.snapshot(ctx)
    started = time.time()
    # The detached worker owns a flock so retries cannot start competing OAuth
    # callbacks. It outlives the short runtime command and is assignment-fenced.
    subprocess.Popen(
        [
            sys.executable,
            "-m",
            MODULE,
            "--framework",
            framework,
            "--assignment",
            control.assignment(ctx),
            "--state-dir",
            str(ctx.state_dir),
        ],
        env=dict(child_env(), PYTHONPATH=str(Path(__file__).resolve().parents[2])),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    # Do not settle the command until the worker has started. Its public state
    # has no provider output, URL or credentials; the browser receipt comes later.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            saved = json.loads((directory(ctx, framework) / "state.json").read_text())
            if saved.get("framework") == framework and saved.get("updated_at", 0) >= started:
                if saved.get("status") == "failed":
                    raise RuntimeError("Could not start provider sign-in.")
                return control.snapshot(ctx)
        except (OSError, ValueError, TypeError):
            pass
        await asyncio.sleep(0.1)
    raise RuntimeError("Could not start provider sign-in.")


async def run(ctx, framework, binding):
    if framework not in COMMANDS or control.assignment(ctx) != binding:
        raise ValueError("Computer assignment changed.")
    path = directory(ctx, framework)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = os.open(path / "signin.lock", os.O_CREAT | os.O_RDWR, 0o600)
    process = None
    output = None
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        # The owner opens the desktop in the same click. Wait for its X server
        # before starting OAuth, so the browser cannot disappear off screen.
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            if control.assignment(ctx) != binding:
                return
            save(ctx, framework, "opening")
            check = await asyncio.create_subprocess_exec(
                *display_probe(),
                env=desktop_env(),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                ready = await asyncio.wait_for(check.wait(), 5) == 0
            except asyncio.TimeoutError:
                check.kill()
                await check.wait()
                ready = False
            if ready:
                break
            await asyncio.sleep(2)
        else:
            raise RuntimeError("Open the Computer desktop to continue.")
        receipt = path / "signin-browser-opened"
        receipt.unlink(missing_ok=True)
        launcher = path / "signin-browser"
        _write_atomic(
            launcher,
            "#!/bin/sh\nexec "
            + shlex.quote(sys.executable)
            + " -m "
            + MODULE
            + " --browser-receipt "
            + shlex.quote(str(receipt))
            + ' --open-browser "$@"\n',
            0o700,
        )
        env = desktop_env()
        env["BROWSER"] = str(launcher)
        # Some CLI browser launchers use xdg-open instead of BROWSER.
        shim = path / "signin-bin"
        shim.mkdir(mode=0o700, exist_ok=True)
        _write_atomic(shim / "xdg-open", launcher.read_text(), 0o700)
        env["PATH"] = str(shim) + os.pathsep + env.get("PATH", "")
        process = await asyncio.create_subprocess_exec(
            *COMMANDS[framework],
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        if process.stdout is not None:
            output = asyncio.create_task(open_printed_login(process.stdout, receipt))
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline and process.returncode is None:
            if output is not None and output.done():
                output.result()
            if control.assignment(ctx) != binding:
                return
            save(ctx, framework, "waiting" if receipt.exists() else "opening")
            try:
                await asyncio.wait_for(process.wait(), 2)
            except asyncio.TimeoutError:
                pass
        if control.assignment(ctx) == binding:
            result = await probe(framework)
            save(
                ctx, framework, "authenticated" if result["authenticated"] else "failed"
            )
    except Exception:
        if control.assignment(ctx) == binding:
            save(ctx, framework, "failed")
    finally:
        if output is not None:
            output.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await output
        if process is not None:
            await terminate(process)
        os.close(lock)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--open-browser")
    parser.add_argument("--browser-receipt")
    parser.add_argument("--desktop", action="store_true")
    parser.add_argument("--framework", choices=COMMANDS)
    parser.add_argument("--assignment")
    parser.add_argument("--state-dir", default=os.getenv("TINYHAT_RUNTIME_STATE_DIR", "/var/lib/tinyhat-hermes-runtime"))
    args = parser.parse_args()
    if args.open_browser:
        open_browser(args.open_browser, args.browser_receipt)
    elif args.framework and (args.assignment or args.desktop):
        ctx = SimpleNamespace(state_dir=Path(args.state_dir))
        # Desktop shortcuts share the API's idempotent start path. A second
        # click reuses the same provider worker, including during an upgrade.
        asyncio.run(
            start(ctx, args.framework)
            if args.desktop
            else run(ctx, args.framework, args.assignment)
        )
    else:
        parser.error("A framework and current assignment are required.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never print OAuth URLs, provider output, or account details.
        print("Could not open the provider sign-in.", file=sys.stderr)
        sys.exit(1)
