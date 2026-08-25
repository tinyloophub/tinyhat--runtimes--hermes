"""Enroll a managed Computer in Tinyhat's private Tailscale network.

The platform issues a short-lived, single-use auth key directly to the
Computer. The runtime uses it only as an input to ``tailscale up`` and never
stores it in command results or durable status.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Sequence

STATUS_PATH = Path("/var/lib/tinyhat-private-access/bootstrap-status.json")
TAILSCALE_INSTALL_TIMEOUT_SECONDS = 180
TAILSCALE_STATUS_TIMEOUT_SECONDS = 10
TAILSCALE_UP_TIMEOUT_SECONDS = 120

Runner = Callable[..., subprocess.CompletedProcess[str]]
_AUTH_KEY_RE = re.compile(r"tskey-[A-Za-z0-9_-]+")


def _status_path() -> Path:
    return Path(os.environ.get("TINYHAT_PRIVATE_ACCESS_STATUS_PATH") or STATUS_PATH)


def _safe_diagnostic(value: Any) -> str:
    return _AUTH_KEY_RE.sub("[redacted]", str(value or "").strip())[:500]


def _write_status(payload: dict[str, Any]) -> dict[str, Any]:
    target = _status_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    try:
        target.chmod(0o644)
    except OSError:
        pass
    return payload


def _run(
    args: Sequence[str],
    *,
    runner: Runner = subprocess.run,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return runner(
        list(args),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def enroll_from_payload(
    payload: dict[str, Any],
    *,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Consume one platform-issued enrollment payload without persisting it."""

    provider = str(payload.get("provider") or "").strip().lower()
    if provider != "tailscale":
        return _write_status(
            {
                "provider": provider or "disabled",
                "state": "disabled",
                "diagnostic": "private access disabled",
            }
        )

    auth_key = str(payload.get("tailscale_auth_key") or "").strip()
    node_name = str(payload.get("tailscale_node_name") or "").strip()
    raw_tags = payload.get("tailscale_tags")
    tags = (
        ",".join(str(tag).strip() for tag in raw_tags if str(tag).strip())
        if isinstance(raw_tags, list)
        else str(raw_tags or "").strip()
    )
    ssh_enabled = bool(payload.get("tailscale_ssh", True))
    if not auth_key or not node_name:
        return _write_status(
            {
                "provider": "tailscale",
                "state": "config_missing",
                "diagnostic": "missing auth key or node name",
            }
        )

    if shutil.which("tailscale") is None:
        install = runner(
            ["bash", "-c", "curl -fsSL https://tailscale.com/install.sh | sh"],
            capture_output=True,
            text=True,
            timeout=TAILSCALE_INSTALL_TIMEOUT_SECONDS,
            check=False,
        )
        if install.returncode != 0:
            return _write_status(
                {
                    "provider": "tailscale",
                    "state": "error",
                    "diagnostic": "tailscale install failed: "
                    + _safe_diagnostic(install.stderr or install.stdout),
                }
            )

    systemctl = shutil.which("systemctl")
    if systemctl is not None:
        started = _run(
            [systemctl, "enable", "--now", "tailscaled"],
            runner=runner,
            timeout=60,
        )
        if started.returncode != 0:
            return _write_status(
                {
                    "provider": "tailscale",
                    "state": "error",
                    "diagnostic": "tailscaled start failed: "
                    + _safe_diagnostic(started.stderr or started.stdout),
                }
            )

    secret_dir = _status_path().parent / "secrets"
    secret_dir.mkdir(parents=True, exist_ok=True)
    try:
        secret_dir.chmod(0o700)
    except OSError:
        pass
    with tempfile.NamedTemporaryFile(
        "w",
        prefix="tailscale-auth.",
        dir=secret_dir,
        delete=False,
    ) as handle:
        auth_file = Path(handle.name)
        handle.write(auth_key)
    try:
        auth_file.chmod(0o600)
        up_args = [
            "tailscale",
            "up",
            f"--auth-key=file:{auth_file}",
            f"--hostname={node_name}",
        ]
        if ssh_enabled:
            up_args.append("--ssh")
        if tags:
            up_args.append(f"--advertise-tags={tags}")
        _run(
            ["tailscale", "logout"],
            runner=runner,
            timeout=TAILSCALE_STATUS_TIMEOUT_SECONDS,
        )
        result = _run(up_args, runner=runner, timeout=TAILSCALE_UP_TIMEOUT_SECONDS)
    finally:
        try:
            auth_file.unlink()
        except OSError:
            pass

    if result.returncode != 0:
        return _write_status(
            {
                "provider": "tailscale",
                "state": "error",
                "node_name": node_name,
                "ssh_enabled": ssh_enabled,
                "diagnostic": "tailscale up failed: "
                + _safe_diagnostic(result.stderr or result.stdout),
            }
        )
    return _write_status(
        {
            "provider": "tailscale",
            "state": "ready",
            "node_name": node_name,
            "ssh_enabled": ssh_enabled,
            "diagnostic": "tailscale enrollment completed",
        }
    )


def private_access_report(*, runner: Runner = subprocess.run) -> dict[str, Any]:
    """Return non-secret Tailscale state for the platform command result."""

    bootstrap: dict[str, Any] = {}
    try:
        decoded = json.loads(_status_path().read_text(encoding="utf-8"))
        if isinstance(decoded, dict):
            bootstrap = decoded
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    base = {
        "provider": "tailscale",
        "node_name": bootstrap.get("node_name"),
    }
    if shutil.which("tailscale") is None:
        return {
            **base,
            "state": "not_installed",
            "diagnostic_code": "tailscale_cli_missing",
            "diagnostic": "tailscale CLI is not installed",
        }
    result = _run(
        ["tailscale", "status", "--json"],
        runner=runner,
        timeout=TAILSCALE_STATUS_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        return {
            **base,
            "state": "unreachable",
            "diagnostic_code": "tailscale_status_failed",
            "diagnostic": _safe_diagnostic(result.stderr or result.stdout),
        }
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        return {
            **base,
            "state": "error",
            "diagnostic_code": "tailscale_status_json_invalid",
            "diagnostic": str(exc)[:500],
        }
    self_node = payload.get("Self") if isinstance(payload.get("Self"), dict) else {}
    ips = self_node.get("TailscaleIPs")
    tailnet_ip = (
        next((str(ip).strip() for ip in ips if str(ip).strip()), None)
        if isinstance(ips, list)
        else None
    )
    node_name = (
        str(self_node.get("HostName") or "").strip()
        or str(base.get("node_name") or "").strip()
        or None
    )
    backend_state = str(payload.get("BackendState") or "").strip()
    ready = bool(tailnet_ip) and (
        backend_state.lower() == "running" or self_node.get("Online") is True
    )
    return {
        **base,
        "node_name": node_name,
        "tailnet_ip": tailnet_ip,
        "state": "ready" if ready else "unreachable",
        "diagnostic_code": "ready" if ready else "tailscale_not_running",
        "diagnostic": backend_state or "tailscale status read",
    }
