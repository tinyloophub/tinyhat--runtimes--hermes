"""Install Hermes Agent using the official installer when it is missing.

What it does:
    Checks whether the ``hermes`` CLI is already installed. If it is present,
    the command returns the current Hermes status and does not reinstall. If it
    is missing, the command installs the small Debian prerequisites when it can
    do so safely as root, then runs the official Hermes Agent installer:

        curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash

    Tinyhat pins the upstream Hermes checkout and lets the official installer
    install its browser dependencies. If that does not satisfy Hermes' own
    browser registration gate, Tinyhat installs both ``agent-browser``'s local
    browser and the pinned Playwright Chromium compatibility cache before
    repeating Hermes' official diagnostics and public browser CLI smoke. Set
    ``TINYHAT_HERMES_INSTALL_ARGS`` on the machine to override the upstream
    installer arguments.

    After Hermes is present, the command verifies the Hermes venv can import
    the Telegram gateway adapter, voice-transcription dependencies, pinned
    ``ddgs`` web search, and pinned Edge TTS. If not, it installs the missing
    packages into the same Hermes project venv. It also installs and verifies
    the pinned Google Workspace CLI, then proves Hermes will register its
    browser tools and that the browser CLI can open, snapshot, and close a
    deterministic local page without depending on external DNS or network
    availability.
    This keeps Tinyhat Computers warm: the later agent-assignment step only
    writes the bot settings and starts the gateway.
    It also warms faster-whisper's selected local STT model cache so a Computer
    still has an on-box multilingual model ready if an operator switches Hermes
    to the local STT provider.

    The command also preinstalls Tinyhat's OpenAI Codex auth quick commands and
    matching Hermes plugin slash-command registrations in ``~/.hermes``. They
    are inert until Telegram is connected, but this keeps the later assignment
    path short and lets Hermes add the Codex commands to Telegram's menu.

When to use it:
    Hat admin queues this automatically during Computer creation after the
    Tinyhat runtime has started heartbeating. You can also run it manually if a
    machine was created before Hermes was installed.

Example input:
    {"kind": "install_hermes", "spec": {}}

Example output:
    {
      "installed_before": false,
      "installed_now": true,
      "installed_after": true,
      "changed": true,
      "status": {"ok": true, "version": "Hermes Agent 0.1.0"}
    }

    ``installed_now`` means the installer ran during this command. If Hermes
    was already present, ``installed_now`` is false, ``installed_after`` is
    true, and ``changed`` is false.

Side effects:
    May install Debian packages ``ca-certificates``, ``curl``, ``git``, and
    ``python3-pip``, ``xz-utils``, ``build-essential``, ``ffmpeg``,
    ``ripgrep``, ``xclip``, and ``wl-clipboard`` when running as root on
    Debian/Ubuntu.
    Runs the public Hermes installer if Hermes is missing. May install Hermes'
    ``messaging``/``voice`` extras into the Hermes venv and download the selected
    local STT model weights. Prefetch failures are reported but do not
    fail provisioning because OpenRouter is the active day-one STT provider.
    After a failed browser probe, may install Chrome for Testing through
    ``agent-browser`` plus pinned Playwright Chromium on Linux x86, or distro
    Chromium on Linux ARM. Downloads and installs the pinned public Google
    Workspace CLI release asset for the machine architecture after verifying
    its SHA-256.
    Does not configure Tinyhat platform state.
"""

from __future__ import annotations

import asyncio
import os
import platform
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from hermes_runtime.commands.configure_telegram import (
    _configure_day_one_capabilities,
    _install_codex_auth_plugin_commands,
    _install_codex_auth_quick_commands,
    local_stt_model,
)
from hermes_runtime.day_one_capabilities import (
    BASELINE_ID,
    BROWSER_CLOUD_PROVIDER,
    BROWSER_ENGINE,
    DDGS_VERSION,
    EDGE_TTS_VERSION,
    GOOGLE_WORKSPACE_CLI_LINUX_ASSETS,
    GOOGLE_WORKSPACE_CLI_RELEASE_TAG,
    GOOGLE_WORKSPACE_CLI_VERSION,
    HERMES_UPSTREAM_COMMIT,
    IMAGE_GENERATION_MODEL,
    IMAGE_GENERATION_PROVIDER,
    PLAYWRIGHT_VERSION,
    TELEGRAM_RICH_DRAFTS,
    TELEGRAM_RICH_MESSAGES,
    TTS_PROVIDER,
    WEB_SEARCH_BACKEND,
)
from hermes_runtime.hermes_cli import (
    find_hermes_binary,
    hermes_install_script,
    maybe_install_debian_prerequisites,
    probe_hermes_status,
    run_process,
    run_shell,
)


def _hermes_project_candidates() -> list[Path]:
    candidates: list[Path] = []
    explicit = (os.getenv("HERMES_PROJECT_DIR") or "").strip()
    if explicit:
        candidates.append(Path(explicit))
    candidates.extend(
        [
            Path("/usr/local/lib/hermes-agent"),
            Path.home() / ".hermes" / "hermes-agent",
        ]
    )
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.expanduser())
        if key not in seen:
            unique.append(candidate.expanduser())
            seen.add(key)
    return unique


def _find_hermes_project_dir() -> Path | None:
    for candidate in _hermes_project_candidates():
        if (candidate / "pyproject.toml").is_file() and (
            candidate / "venv" / "bin" / "python"
        ).is_file():
            return candidate
    return None


async def _probe_messaging_dependencies(project_dir: Path) -> dict[str, Any]:
    python_bin = project_dir / "venv" / "bin" / "python"
    probe = await run_process(
        [
            str(python_bin),
            "-c",
            (
                "import importlib.metadata\n"
                "import importlib.util\n"
                "modules=('telegram','telegram.ext','faster_whisper','ddgs','edge_tts')\n"
                "missing=[name for name in modules if importlib.util.find_spec(name) is None]\n"
                f"expected={{'ddgs':'{DDGS_VERSION}','edge-tts':'{EDGE_TTS_VERSION}'}}\n"
                "wrong=[]\n"
                "for package, version in expected.items():\n"
                "    try:\n"
                "        actual=importlib.metadata.version(package)\n"
                "    except importlib.metadata.PackageNotFoundError:\n"
                "        actual='missing'\n"
                "    if actual != version:\n"
                "        wrong.append(f'{package}={actual} (expected {version})')\n"
                "problems=missing+wrong\n"
                "print('ok' if not problems else 'unready:' + ','.join(problems))\n"
                "raise SystemExit(0 if not problems else 1)\n"
            ),
        ],
        timeout_seconds=30,
    )
    return {
        "ok": bool(probe.get("ok")),
        "project_dir": str(project_dir),
        "python": str(python_bin),
        "probe": probe,
    }


def _pip_command_for_python(python_bin: Path) -> str:
    if (python_bin.parent / "pip").is_file():
        return f"{shlex.quote(str(python_bin))} -m pip"

    pip_bin = shutil.which("pip") or shutil.which("pip3")
    # ``pip --python`` can install into a venv that does not have pip
    # bootstrapped yet, but older distro pips do not support the flag. Prefer
    # the Hermes venv's own pip when present, then fall back only when the
    # system pip advertises the option.
    if pip_bin and _pip_supports_python_option(pip_bin):
        return (
            f"{shlex.quote(pip_bin)} --python {shlex.quote(str(python_bin))}"
        )
    return f"{shlex.quote(str(python_bin))} -m pip"


def _pip_supports_python_option(pip_bin: str) -> bool:
    try:
        result = subprocess.run(
            [pip_bin, "--help"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "--python" in f"{result.stdout}\n{result.stderr}"


async def _ensure_messaging_dependencies() -> dict[str, Any]:
    project_dir = _find_hermes_project_dir()
    if project_dir is None:
        return {
            "ok": False,
            "changed": False,
            "message": "Hermes project venv was not found.",
        }

    before = await _probe_messaging_dependencies(project_dir)
    if before.get("ok"):
        return {
            "ok": True,
            "changed": False,
            "project_dir": str(project_dir),
            "before": before,
            "after": before,
            "install": None,
        }

    prerequisites: dict[str, Any] | None = None
    if shutil.which("pip") is None and shutil.which("pip3") is None:
        prerequisites = await maybe_install_debian_prerequisites()

    python_bin = project_dir / "venv" / "bin" / "python"
    package_spec = f"{project_dir}[messaging,voice]"
    install = await run_shell(
        (
            f"cd {shlex.quote(str(project_dir))}\n"
            f"{_pip_command_for_python(python_bin)} install -e "
            f"{shlex.quote(package_spec)}\n"
            f"{_pip_command_for_python(python_bin)} install "
            f"{shlex.quote(f'ddgs=={DDGS_VERSION}')} "
            f"{shlex.quote(f'edge-tts=={EDGE_TTS_VERSION}')}"
        ),
        timeout_seconds=900,
        env={"PIP_DISABLE_PIP_VERSION_CHECK": "1"},
    )
    after = await _probe_messaging_dependencies(project_dir)
    return {
        "ok": bool(after.get("ok")),
        "changed": bool(install.get("ok")) and bool(after.get("ok")),
        "project_dir": str(project_dir),
        "before": before,
        "after": after,
        "install": install,
        "prerequisites": prerequisites,
    }


def _google_workspace_cli_asset() -> dict[str, str] | None:
    if platform.system().strip().lower() != "linux":
        return None
    machine = platform.machine().strip().lower()
    architecture = {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "arm64": "aarch64",
        "aarch64": "aarch64",
    }.get(machine)
    if architecture is None:
        return None
    configured = GOOGLE_WORKSPACE_CLI_LINUX_ASSETS.get(architecture)
    if configured is None:
        return None
    target = str(configured["target"])
    archive = f"google-workspace-cli-{target}.tar.gz"
    return {
        "architecture": architecture,
        "target": target,
        "archive": archive,
        "sha256": str(configured["sha256"]),
        "url": (
            "https://github.com/googleworkspace/cli/releases/download/"
            f"{GOOGLE_WORKSPACE_CLI_RELEASE_TAG}/{archive}"
        ),
    }


def _google_workspace_cli_install_path() -> Path:
    explicit = (os.getenv("TINYHAT_GWS_INSTALL_PATH") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return Path("/usr/local/bin/gws")


def _google_workspace_cli_binary() -> Path | None:
    explicit = (os.getenv("GWS_BIN") or "").strip()
    candidates = [
        Path(explicit).expanduser() if explicit else None,
        _google_workspace_cli_install_path(),
        Path(shutil.which("gws") or ""),
    ]
    for candidate in candidates:
        if candidate is None or not str(candidate):
            continue
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


async def _probe_google_workspace_cli() -> dict[str, Any]:
    binary = _google_workspace_cli_binary()
    if binary is None:
        return {
            "ok": False,
            "installed": False,
            "version": None,
            "binary": None,
            "message": "Google Workspace CLI was not found.",
        }
    probe = await run_process(
        [str(binary), "--version"],
        timeout_seconds=30,
    )
    stdout = str(probe.get("stdout") or "").strip()
    first_line = stdout.splitlines()[0].strip() if stdout else ""
    expected = f"gws {GOOGLE_WORKSPACE_CLI_VERSION}"
    ok = bool(probe.get("ok")) and first_line == expected
    return {
        "ok": ok,
        "installed": True,
        "version": first_line.removeprefix("gws ").strip() or None,
        "expected_version": GOOGLE_WORKSPACE_CLI_VERSION,
        "binary": str(binary),
        "probe": probe,
        "message": (
            "Google Workspace CLI is ready."
            if ok
            else f"Expected '{expected}', received '{first_line or 'no output'}'."
        ),
    }


async def _ensure_google_workspace_cli() -> dict[str, Any]:
    before = await _probe_google_workspace_cli()
    if before.get("ok"):
        return {
            "ok": True,
            "changed": False,
            "before": before,
            "after": before,
            "asset": _google_workspace_cli_asset(),
            "install": None,
        }

    asset = _google_workspace_cli_asset()
    if asset is None:
        return {
            "ok": False,
            "changed": False,
            "before": before,
            "after": before,
            "asset": None,
            "install": None,
            "message": (
                "Google Workspace CLI has no pinned release asset for "
                f"{platform.system()} {platform.machine()}."
            ),
        }

    install_path = _google_workspace_cli_install_path()
    install = await run_shell(
        (
            "set -euo pipefail\n"
            'tmp_dir="$(mktemp -d)"\n'
            'trap \'rm -rf "$tmp_dir"\' EXIT\n'
            f"archive=\"$tmp_dir/{asset['archive']}\"\n"
            f"curl -fsSL {shlex.quote(asset['url'])} -o \"$archive\"\n"
            f"printf '%s  %s\\n' {shlex.quote(asset['sha256'])} \"$archive\" "
            "| sha256sum --check --status -\n"
            'tar -xzf "$archive" -C "$tmp_dir"\n'
            f"install -d {shlex.quote(str(install_path.parent))}\n"
            f"install -m 0755 \"$tmp_dir/gws\" {shlex.quote(str(install_path))}"
        ),
        timeout_seconds=300,
    )
    after = await _probe_google_workspace_cli()
    return {
        "ok": bool(install.get("ok")) and bool(after.get("ok")),
        "changed": bool(install.get("ok")) and bool(after.get("ok")),
        "before": before,
        "after": after,
        "asset": asset,
        "install_path": str(install_path),
        "install": install,
    }


def _agent_browser_binary() -> Path | None:
    explicit = (os.getenv("AGENT_BROWSER_BIN") or "").strip()
    project_dir = _find_hermes_project_dir()
    candidates = [
        Path(explicit).expanduser() if explicit else None,
        Path.home() / ".hermes" / "node" / "bin" / "agent-browser",
        (
            project_dir / "node_modules" / ".bin" / "agent-browser"
            if project_dir is not None
            else None
        ),
        Path(shutil.which("agent-browser") or ""),
    ]
    for candidate in candidates:
        if candidate is None or not str(candidate):
            continue
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _npx_binary() -> Path | None:
    candidates = [
        Path.home() / ".hermes" / "node" / "bin" / "npx",
        Path(shutil.which("npx") or ""),
    ]
    for candidate in candidates:
        if not str(candidate):
            continue
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


BROWSER_SMOKE_EXPECTED_TEXT = "Tinyhat Browser Smoke"
BROWSER_SMOKE_TARGET = (
    "data:text/html,%3Ctitle%3ETinyhat%20Browser%20Smoke%3C%2Ftitle%3E"
    "%3Ch1%3ETinyhat%20Browser%20Smoke%3C%2Fh1%3E"
)


async def _probe_browser_automation() -> dict[str, Any]:
    """Prove Hermes browser tools work without requiring network access."""
    hermes_bin = find_hermes_binary()
    if hermes_bin is None:
        return {
            "ok": False,
            "registered": False,
            "smoke_status": "failed",
            "message": "Hermes CLI was not found.",
        }
    browser_bin = _agent_browser_binary()
    if browser_bin is None:
        return {
            "ok": False,
            "registered": False,
            "smoke_status": "failed",
            "message": "agent-browser was not found after Hermes installation.",
        }

    doctor = await run_process(
        [str(hermes_bin), "doctor"],
        timeout_seconds=300,
    )
    doctor_text = "\n".join(
        [
            str(doctor.get("stdout") or ""),
            str(doctor.get("stderr") or ""),
        ]
    )
    registered = any(
        "✓ Playwright Chromium" in line
        for line in doctor_text.splitlines()
    )
    agent_browser_version_probe = await run_process(
        [str(browser_bin), "--version"],
        timeout_seconds=30,
    )
    agent_browser_version = str(
        agent_browser_version_probe.get("stdout")
        or agent_browser_version_probe.get("stderr")
        or ""
    ).strip().splitlines()
    agent_browser_version_text = (
        agent_browser_version[0].strip() if agent_browser_version else None
    )

    session = "tinyhat-provisioning-smoke"
    attempts: list[dict[str, Any]] = []
    open_result: dict[str, Any] = {}
    snapshot_result: dict[str, Any] | None = None
    engine_probe: dict[str, Any] | None = None
    close_result: dict[str, Any] = {}
    expected_page = False
    engine_version: str | None = None
    for attempt_number in range(1, 4):
        open_result = await run_process(
            [
                str(browser_bin),
                "--session",
                session,
                "open",
                BROWSER_SMOKE_TARGET,
            ],
            timeout_seconds=120,
        )
        snapshot_result = None
        if open_result.get("ok"):
            snapshot_result = await run_process(
                [str(browser_bin), "--session", session, "snapshot"],
                timeout_seconds=60,
            )
            engine_probe = await run_process(
                [
                    str(browser_bin),
                    "--session",
                    session,
                    "eval",
                    "navigator.userAgent",
                ],
                timeout_seconds=30,
            )
        else:
            engine_probe = None
        close_result = await run_process(
            [str(browser_bin), "--session", session, "close"],
            timeout_seconds=30,
        )
        page_text = "\n".join(
            [
                str(open_result.get("stdout") or ""),
                str(snapshot_result.get("stdout") or "")
                if isinstance(snapshot_result, dict)
                else "",
            ]
        )
        expected_page = BROWSER_SMOKE_EXPECTED_TEXT in page_text
        engine_text = (
            str(engine_probe.get("stdout") or "")
            if isinstance(engine_probe, dict)
            else ""
        )
        engine_match = re.search(
            r"(?:HeadlessChrome|Chrome)/([0-9.]+)",
            engine_text,
        )
        engine_version = engine_match.group(1) if engine_match else None
        attempt_ok = (
            bool(open_result.get("ok"))
            and bool(snapshot_result and snapshot_result.get("ok"))
            and bool(engine_probe and engine_probe.get("ok"))
            and bool(engine_version)
            and bool(close_result.get("ok"))
            and expected_page
        )
        attempts.append(
            {
                "attempt": attempt_number,
                "ok": attempt_ok,
                "open": open_result,
                "snapshot": snapshot_result,
                "engine_probe": engine_probe,
                "close": close_result,
                "expected_page_found": expected_page,
                "engine_version": engine_version,
            }
        )
        if attempt_ok:
            break
        if attempt_number < 3:
            await asyncio.sleep(float(attempt_number))

    ok = (
        registered
        and bool(agent_browser_version_probe.get("ok"))
        and bool(agent_browser_version_text)
        and bool(open_result.get("ok"))
        and bool(snapshot_result and snapshot_result.get("ok"))
        and bool(engine_probe and engine_probe.get("ok"))
        and bool(engine_version)
        and bool(close_result.get("ok"))
        and expected_page
    )
    return {
        "ok": ok,
        "registered": registered,
        "smoke_status": "passed" if ok else "failed",
        "hermes_bin": str(hermes_bin),
        "browser_bin": str(browser_bin),
        "agent_browser_version": agent_browser_version_text,
        "agent_browser_version_probe": agent_browser_version_probe,
        "engine_version": engine_version,
        "target": BROWSER_SMOKE_TARGET,
        "network_required": False,
        "expected_page_found": expected_page,
        "doctor": doctor,
        "open": open_result,
        "snapshot": snapshot_result,
        "engine_probe": engine_probe,
        "close": close_result,
        "attempts": attempts,
    }


def _needs_arm_system_browser_fallback() -> bool:
    return platform.machine().strip().lower() in {"aarch64", "arm64"}


def _needs_managed_browser_fallback() -> bool:
    return (
        platform.system().strip().lower() == "linux"
        and platform.machine().strip().lower() in {"x86_64", "amd64"}
    )


async def _install_managed_browser_fallback() -> dict[str, Any]:
    """Install both browser layouts required by agent-browser and Hermes.

    ``agent-browser install`` can successfully run a browser that it downloaded
    outside Playwright's cache. The pinned Hermes registration check does not
    inspect that private cache; it looks for a system browser or a
    Playwright-managed ``chromium-*`` directory. Install both public layouts so
    the runtime smoke and the model's tool-registration gate agree.
    """
    result: dict[str, Any] = {
        "attempted": False,
        "architecture": platform.machine(),
        "reason": "managed_browser_not_applicable",
        "result": None,
    }
    if not _needs_managed_browser_fallback():
        return result

    browser_bin = _agent_browser_binary()
    if browser_bin is None:
        result["reason"] = "agent_browser_missing"
        return result

    agent_browser_install = await run_process(
        [str(browser_bin), "install", "--with-deps"],
        timeout_seconds=900,
    )
    npx_bin = _npx_binary()
    if npx_bin is None:
        result["attempted"] = True
        result["reason"] = "npx_missing"
        result["browser_bin"] = str(browser_bin)
        result["playwright_version"] = PLAYWRIGHT_VERSION
        result["agent_browser_install"] = agent_browser_install
        result["result"] = {"ok": False}
        return result
    playwright_install = await run_process(
        [
            str(npx_bin),
            "--yes",
            f"playwright@{PLAYWRIGHT_VERSION}",
            "install",
            "--with-deps",
            "chromium",
        ],
        timeout_seconds=900,
    )
    result["attempted"] = True
    result["reason"] = "hermes_browser_registration_unready"
    result["browser_bin"] = str(browser_bin)
    result["playwright_version"] = PLAYWRIGHT_VERSION
    result["agent_browser_install"] = agent_browser_install
    result["playwright_install"] = playwright_install
    result["result"] = {
        "ok": bool(agent_browser_install.get("ok"))
        and bool(playwright_install.get("ok"))
    }
    return result


async def _install_arm_system_browser_fallback() -> dict[str, Any]:
    """Install distro Chromium only when Playwright has no ARM browser.

    The official Hermes installer owns the normal browser install. Installing
    Ubuntu's ``chromium`` package before it runs makes Hermes select the
    snap-confined wrapper, which cannot launch reliably from a root system
    service. Linux ARM containers are the narrow exception: Playwright does
    not provide the same managed Chromium build there, while Debian's native
    Chromium package is known to work.
    """
    result: dict[str, Any] = {
        "attempted": False,
        "architecture": platform.machine(),
        "reason": "playwright_browser_preferred",
        "result": None,
    }
    if not _needs_arm_system_browser_fallback():
        return result
    result["reason"] = "arm_playwright_browser_unavailable"
    if os.name != "posix" or getattr(os, "geteuid", lambda: -1)() != 0:
        return result
    if shutil.which("apt-get") is None:
        return result

    install = await run_shell(
        "export DEBIAN_FRONTEND=noninteractive\n"
        "apt-get update\n"
        "apt-get install -y --no-install-recommends chromium",
        timeout_seconds=600,
    )
    result["attempted"] = True
    result["result"] = install
    return result


async def _install_browser_fallback() -> dict[str, Any]:
    if _needs_managed_browser_fallback():
        return await _install_managed_browser_fallback()
    return await _install_arm_system_browser_fallback()


def _browser_failure_summary(browser_smoke: dict[str, Any]) -> str:
    message = str(browser_smoke.get("message") or "").strip()
    if message and not isinstance(browser_smoke.get("doctor"), dict):
        return message
    if not browser_smoke.get("registered"):
        return "Hermes doctor did not report Playwright Chromium ready"
    for step in (
        "agent_browser_version_probe",
        "open",
        "snapshot",
        "engine_probe",
        "close",
    ):
        probe = browser_smoke.get(step)
        if not isinstance(probe, dict) or probe.get("ok"):
            continue
        detail = str(probe.get("stderr") or probe.get("stdout") or "").strip()
        if detail:
            return f"{step}: {detail.splitlines()[0][:240]}"
        if probe.get("timed_out"):
            return f"{step}: browser probe timed out"
        return f"{step}: returncode={probe.get('returncode')}"
    if not browser_smoke.get("expected_page_found"):
        return "browser smoke did not find the deterministic local page"
    if not browser_smoke.get("engine_version"):
        return "browser smoke could not determine the Chromium version"
    return message or "unknown browser smoke failure"


def _day_one_capability_report(
    *,
    dependencies: dict[str, Any],
    config: dict[str, Any],
    browser_smoke: dict[str, Any],
    google_workspace_cli: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema": "tinyhat_hermes_day_one_capabilities_v1",
        "baseline_id": BASELINE_ID,
        "upstream_hermes_commit": HERMES_UPSTREAM_COMMIT,
        "capabilities": {
            "web_search": {
                "state": "ready",
                "provider": WEB_SEARCH_BACKEND,
                "dependency_ready": bool(dependencies.get("ok")),
                "smoke_status": "not_run",
            },
            "web_extract": {
                "state": "available_when_connected",
                "provider": None,
                "dependency_ready": False,
                "smoke_status": "not_run",
            },
            "browser": {
                "state": "ready" if browser_smoke.get("ok") else "failed",
                "provider": BROWSER_CLOUD_PROVIDER,
                "engine": BROWSER_ENGINE,
                "version": browser_smoke.get("engine_version"),
                "binary": browser_smoke.get("browser_bin"),
                "automation_cli_version": browser_smoke.get(
                    "agent_browser_version"
                ),
                "dependency_ready": bool(browser_smoke.get("ok")),
                "registered": bool(browser_smoke.get("registered")),
                "smoke_status": browser_smoke.get("smoke_status"),
            },
            "google_workspace_cli": {
                "state": (
                    "ready" if google_workspace_cli.get("ok") else "failed"
                ),
                "version": GOOGLE_WORKSPACE_CLI_VERSION,
                "binary": (
                    google_workspace_cli.get("after", {}).get("binary")
                    if isinstance(google_workspace_cli.get("after"), dict)
                    else None
                ),
                "dependency_ready": bool(google_workspace_cli.get("ok")),
                "authentication": "available_when_connected",
                "smoke_status": (
                    "passed" if google_workspace_cli.get("ok") else "failed"
                ),
            },
            "image_generation": {
                "state": "configured_waiting_for_assignment_credential",
                "provider": IMAGE_GENERATION_PROVIDER,
                "model": IMAGE_GENERATION_MODEL,
                "credential_ready": False,
                "smoke_status": "not_run",
            },
            "text_to_speech": {
                "state": "ready",
                "provider": TTS_PROVIDER,
                "dependency_ready": bool(dependencies.get("ok")),
                "smoke_status": "not_run",
            },
            "telegram_rich_rendering": {
                "state": "configured_waiting_for_assignment",
                "rich_messages": TELEGRAM_RICH_MESSAGES,
                "rich_drafts": TELEGRAM_RICH_DRAFTS,
                "smoke_status": "not_run",
            },
        },
        "config_applied": bool(config.get("ok")),
    }


def _skip_local_stt_model_prefetch() -> bool:
    value = (os.getenv("TINYHAT_SKIP_LOCAL_STT_MODEL_PREFETCH") or "").strip().lower()
    return value in {
        "1",
        "true",
        "yes",
        "on",
    }


def _status_probe_attempts() -> int:
    raw = (os.getenv("TINYHAT_HERMES_STATUS_PROBE_ATTEMPTS") or "").strip()
    if not raw:
        return 5
    try:
        attempts = int(raw)
    except ValueError:
        return 5
    return max(1, min(attempts, 10))


def _status_probe_timeout_seconds() -> int:
    raw = (os.getenv("TINYHAT_HERMES_STATUS_PROBE_TIMEOUT_SECONDS") or "").strip()
    if not raw:
        return 90
    try:
        timeout = int(raw)
    except ValueError:
        return 90
    return max(30, min(timeout, 300))


def _status_probe_total_timeout_seconds() -> int:
    raw = (os.getenv("TINYHAT_HERMES_STATUS_PROBE_TOTAL_TIMEOUT_SECONDS") or "").strip()
    if not raw:
        return 300
    try:
        timeout = int(raw)
    except ValueError:
        return 300
    return max(60, min(timeout, 900))


def _status_probe_retry_delay_seconds(attempt: int) -> int:
    raw = (os.getenv("TINYHAT_HERMES_STATUS_PROBE_RETRY_DELAY_SECONDS") or "").strip()
    if not raw:
        base = 5
    else:
        try:
            base = int(raw)
        except ValueError:
            base = 5
    return max(1, min(base * attempt, 30))


def _failed_status_command_summary(status: dict[str, Any]) -> str:
    commands = status.get("commands")
    if not isinstance(commands, dict):
        return str(status.get("message") or "status probe failed")

    failures: list[str] = []
    for name, result in commands.items():
        if not isinstance(result, dict) or result.get("ok"):
            continue
        detail = str(result.get("stderr") or result.get("stdout") or "").strip()
        if detail:
            detail = detail.splitlines()[0][:240]
        elif result.get("timed_out"):
            detail = "timed out"
        else:
            detail = f"returncode={result.get('returncode')}"
        failures.append(f"{name}: {detail}")
    if failures:
        return "; ".join(failures)
    return str(status.get("message") or "status probe failed")


def _status_probe_timed_out(status: dict[str, Any]) -> bool:
    commands = status.get("commands")
    if not isinstance(commands, dict):
        return False
    return any(
        isinstance(result, dict) and bool(result.get("timed_out"))
        for result in commands.values()
    )


async def _probe_hermes_status_with_retries() -> dict[str, Any]:
    attempts = _status_probe_attempts()
    timeout_seconds = _status_probe_timeout_seconds()
    total_timeout_seconds = _status_probe_total_timeout_seconds()
    loop = asyncio.get_running_loop()
    started_at = loop.time()
    probe_attempts: list[dict[str, Any]] = []
    status: dict[str, Any] = {}
    for attempt in range(1, attempts + 1):
        status = await probe_hermes_status(timeout_seconds=timeout_seconds)
        probe_attempts.append(
            {
                "attempt": attempt,
                "ok": bool(status.get("ok")),
                "installed": bool(status.get("installed")),
                "message": status.get("message"),
                "failure_summary": (
                    None if status.get("ok") else _failed_status_command_summary(status)
                ),
            }
        )
        if status.get("installed") and status.get("ok"):
            break
        if _status_probe_timed_out(status):
            status["probe_stopped_reason"] = "command_timeout"
            break
        elapsed_seconds = loop.time() - started_at
        if elapsed_seconds >= total_timeout_seconds:
            status["probe_stopped_reason"] = "total_timeout"
            break
        if attempt < attempts:
            delay_seconds = _status_probe_retry_delay_seconds(attempt)
            if elapsed_seconds + delay_seconds >= total_timeout_seconds:
                status["probe_stopped_reason"] = "total_timeout"
                break
            await asyncio.sleep(delay_seconds)
    status["probe_attempts"] = probe_attempts
    status["probe_attempt_count"] = len(probe_attempts)
    status["probe_timeout_seconds"] = timeout_seconds
    status["probe_total_timeout_seconds"] = total_timeout_seconds
    return status


def _local_stt_model_prefetch_timeout_seconds() -> int:
    raw = (os.getenv("TINYHAT_HERMES_STT_MODEL_PREFETCH_TIMEOUT_SECONDS") or "").strip()
    if not raw:
        return 900
    try:
        timeout = int(raw)
    except ValueError:
        return 900
    return max(60, timeout)


async def _prefetch_local_stt_model() -> dict[str, Any]:
    """Warm faster-whisper's model cache during provisioning.

    Hermes downloads local STT model weights on first use. Doing that while a
    user waits on their first Telegram voice note makes voice look broken, so
    Tinyhat warms the selected local model during ``install_hermes`` instead.
    """
    if _skip_local_stt_model_prefetch():
        return {
            "ok": True,
            "changed": False,
            "skipped": True,
            "skip_env": "TINYHAT_SKIP_LOCAL_STT_MODEL_PREFETCH",
            "model": local_stt_model(),
        }

    project_dir = _find_hermes_project_dir()
    if project_dir is None:
        return {
            "ok": False,
            "changed": False,
            "skipped": False,
            "message": "Hermes project venv was not found.",
            "model": local_stt_model(),
        }

    python_bin = project_dir / "venv" / "bin" / "python"
    model = local_stt_model()
    result = await run_process(
        [
            str(python_bin),
            "-c",
            (
                "import os\n"
                "from faster_whisper import WhisperModel\n"
                "model = os.environ['TINYHAT_LOCAL_STT_MODEL']\n"
                "WhisperModel(model, device='cpu', compute_type='int8')\n"
                "print('cached:' + model)\n"
            ),
        ],
        timeout_seconds=_local_stt_model_prefetch_timeout_seconds(),
        env={
            "TINYHAT_LOCAL_STT_MODEL": model,
            "HF_HUB_DISABLE_TELEMETRY": "1",
        },
    )
    return {
        "ok": bool(result.get("ok")),
        "changed": bool(result.get("ok")),
        "skipped": False,
        "model": model,
        "project_dir": str(project_dir),
        "python": str(python_bin),
        "result": result,
    }


async def run(_ctx: Any, _command: dict[str, Any]) -> dict[str, Any]:
    installed_before = find_hermes_binary() is not None
    prerequisites: dict[str, Any] | None = None
    install_result: dict[str, Any] | None = None

    if not installed_before:
        prerequisites = await maybe_install_debian_prerequisites()
        install_result = await run_shell(
            hermes_install_script(),
            timeout_seconds=1500,
            env={"CI": "1"},
        )
        if not install_result.get("ok"):
            raise RuntimeError(
                "Hermes installer failed with returncode="
                f"{install_result.get('returncode')}"
            )

    status = await _probe_hermes_status_with_retries()
    if not status.get("installed"):
        raise RuntimeError("Hermes installer completed, but hermes CLI was not found.")
    if not status.get("ok"):
        attempts = status.get("probe_attempt_count") or _status_probe_attempts()
        raise RuntimeError(
            "Hermes CLI is installed, but status checks failed after "
            f"{attempts} attempt(s): {_failed_status_command_summary(status)}"
        )

    hermes_bin_value = status.get("hermes_bin")
    hermes_bin = (
        Path(str(hermes_bin_value))
        if hermes_bin_value
        else find_hermes_binary()
    )
    if hermes_bin is None:
        raise RuntimeError("Hermes CLI is installed, but hermes binary was not found.")

    messaging = await _ensure_messaging_dependencies()
    if not messaging.get("ok"):
        raise RuntimeError("Hermes day-one dependencies are not available.")
    google_workspace_cli = await _ensure_google_workspace_cli()
    if not google_workspace_cli.get("ok"):
        raise RuntimeError("Google Workspace CLI is not available.")
    day_one_defaults = await _configure_day_one_capabilities(hermes_bin)
    if not day_one_defaults.get("ok"):
        raise RuntimeError("Hermes day-one configuration could not be applied.")
    browser_smoke = await _probe_browser_automation()
    browser_fallback: dict[str, Any] | None = None
    if not browser_smoke.get("ok"):
        browser_fallback = await _install_browser_fallback()
        if browser_fallback.get("attempted"):
            browser_smoke = await _probe_browser_automation()
    if not browser_smoke.get("ok"):
        raise RuntimeError(
            "Hermes browser automation smoke check failed: "
            f"{_browser_failure_summary(browser_smoke)}"
        )
    local_stt_model_prefetch = await _prefetch_local_stt_model()
    local_stt_model_prefetch_warning = None
    if not local_stt_model_prefetch.get("ok"):
        local_stt_model_prefetch_warning = (
            "Hermes local STT model prefetch failed; provisioning "
            "continues because OpenRouter STT is the active provider."
        )
    codex_auth = {
        "quick_commands": _install_codex_auth_quick_commands(),
        "plugin_commands": _install_codex_auth_plugin_commands(),
    }

    installed_after = bool(status.get("installed"))
    installed_by_command = not installed_before
    day_one_capabilities = _day_one_capability_report(
        dependencies=messaging,
        config=day_one_defaults,
        browser_smoke=browser_smoke,
        google_workspace_cli=google_workspace_cli,
    )

    return {
        "schema": "tinyhat_hermes_install_v1",
        "installed_before": installed_before,
        "installed_now": installed_by_command,
        "installed_after": installed_after,
        "already_installed": installed_before,
        "changed": installed_by_command,
        "install_url": "https://hermes-agent.nousresearch.com/install.sh",
        "install_args_source": "TINYHAT_HERMES_INSTALL_ARGS",
        "upstream_hermes_commit": HERMES_UPSTREAM_COMMIT,
        "prerequisites": prerequisites,
        "install": install_result,
        "messaging": messaging,
        "capability_dependencies": messaging,
        "google_workspace_cli": google_workspace_cli,
        "multimodal_defaults": day_one_defaults.get("multimedia"),
        "day_one_defaults": day_one_defaults,
        "browser_smoke": browser_smoke,
        "browser_fallback": browser_fallback,
        "day_one_capabilities": day_one_capabilities,
        "local_stt_model_prefetch": local_stt_model_prefetch,
        "local_stt_model_prefetch_warning": local_stt_model_prefetch_warning,
        "codex_auth": codex_auth,
        "status": status,
    }
