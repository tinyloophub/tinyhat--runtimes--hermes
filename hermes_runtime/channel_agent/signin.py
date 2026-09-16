"""Run the provider's browser login on the Computer without opening a terminal.

OAuth URLs and CLI output never leave the process or enter runtime telemetry.
The provider CLI owns its credentials and its loopback OAuth callback.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
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


def status(ctx):
    try:
        value = json.loads((control.directory(ctx) / "signin.json").read_text())
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
        control.directory(ctx) / "signin.json",
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


def open_browser(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname
        not in {
            "auth.openai.com",
            "chatgpt.com",
            "claude.ai",
            "console.anthropic.com",
            "platform.claude.com",
        }
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Expected an official provider login URL.")
    browser = next(
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
    subprocess.Popen(
        [browser, "--no-sandbox", "--new-window", url],
        env=desktop_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


async def start(ctx, framework):
    if framework not in COMMANDS:
        raise ValueError("Choose Codex or Claude Code to sign in.")
    target = await probe(framework)
    if not target["installed"]:
        raise RuntimeError("Install the framework first.")
    if target["authenticated"]:
        await control.inventory(ctx)
        return control.snapshot(ctx)
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
    return control.snapshot(ctx)


async def run(ctx, framework, binding):
    if framework not in COMMANDS or control.assignment(ctx) != binding:
        raise ValueError("Computer assignment changed.")
    directory = control.directory(ctx)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = os.open(directory / "signin.lock", os.O_CREAT | os.O_RDWR, 0o600)
    process = None
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
                "xdpyinfo",
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
        launcher = directory / "signin-browser"
        _write_atomic(
            launcher,
            "#!/bin/sh\nexec "
            + shlex.quote(sys.executable)
            + " -m "
            + MODULE
            + ' --open-browser "$@"\n',
            0o700,
        )
        env = desktop_env()
        env["BROWSER"] = str(launcher)
        # Some CLI browser launchers use xdg-open instead of BROWSER.
        shim = directory / "signin-bin"
        shim.mkdir(mode=0o700, exist_ok=True)
        _write_atomic(shim / "xdg-open", launcher.read_text(), 0o700)
        env["PATH"] = str(shim) + os.pathsep + env.get("PATH", "")
        process = await asyncio.create_subprocess_exec(
            *COMMANDS[framework],
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline and process.returncode is None:
            if control.assignment(ctx) != binding:
                return
            save(ctx, framework, "waiting")
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
        if process is not None:
            await terminate(process)
        os.close(lock)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--open-browser")
    parser.add_argument("--framework", choices=COMMANDS)
    parser.add_argument("--assignment")
    parser.add_argument("--state-dir")
    args = parser.parse_args()
    if args.open_browser:
        open_browser(args.open_browser)
    elif args.framework and args.assignment and args.state_dir:
        asyncio.run(
            run(
                SimpleNamespace(state_dir=Path(args.state_dir)),
                args.framework,
                args.assignment,
            )
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
