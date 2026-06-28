"""Telegram quick-command helper for Hermes OpenAI Codex auth.

This module is invoked by Hermes quick commands that Tinyhat installs into
``~/.hermes/config.yaml`` when a Computer is connected to Telegram. It starts
the official Codex CLI device-code auth flow, sends the authorization link and
device code to the configured Telegram home channel, then asks Hermes through
its formal model picker to import/switch to OpenAI Codex.

The OpenAI device code and final auth token stay on the Computer. The platform
only installs the quick command; it is not in the OpenAI auth path.
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import json
import os
from pathlib import Path
import pty
import re
import select
import subprocess
import sys
import time
from typing import Any
from urllib import error, parse, request

from hermes_runtime.codex_limits import find_codex_binary
from hermes_runtime.hermes_cli import find_hermes_binary

STATE_SCHEMA = "tinyhat_hermes_codex_auth_v1"
PRIMARY_PROVIDER = "openai-codex"
FALLBACK_PROVIDER = "codex-oauth"
# Older Hermes builds may name the auth command ``codex-oauth``. Both auth
# command names write credentials for the OpenAI Codex model provider that
# Hermes uses at chat time.
MODEL_PROVIDER = "openai-codex"
MAX_LOG_CHARS = 16_000
AUTH_TIMEOUT_SECONDS = 900
MODEL_PICKER_TIMEOUT_SECONDS = 90
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
SECRET_VALUE_RE = re.compile(
    r"(?i)\b(access_token|refresh_token|id_token|api[_-]?key|authorization)\b"
    r"(\s*[:=]\s*)(['\"]?)[^'\"\s]+"
)
OPENAI_SECRET_RE = re.compile(r"\b(?:sk|sess|eyJ)[A-Za-z0-9._-]{20,}\b")


def _state_dir() -> Path:
    raw = (os.getenv("TINYHAT_CODEX_AUTH_STATE_DIR") or "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".hermes" / "tinyhat-codex-auth"


def _log_path() -> Path:
    return _state_dir() / "auth.log"


def _pid_path() -> Path:
    return _state_dir() / "worker.pid"


def _start_lock_path() -> Path:
    return _state_dir() / "worker.starting"


def _status_path() -> Path:
    return _state_dir() / "status.json"


def _ensure_state_dir() -> None:
    _state_dir().mkdir(parents=True, exist_ok=True)
    try:
        _state_dir().chmod(0o700)
    except OSError:
        pass


def _write_status(payload: dict[str, Any]) -> None:
    _ensure_state_dir()
    payload = {
        "schema": STATE_SCHEMA,
        "updated_at": int(time.time()),
        **payload,
    }
    _status_path().write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        _status_path().chmod(0o600)
    except OSError:
        pass


def _read_status() -> dict[str, Any] | None:
    try:
        payload = json.loads(_status_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _redact_sensitive_text(text: str) -> str:
    text = SECRET_VALUE_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}[redacted]", text)
    return OPENAI_SECRET_RE.sub("[redacted-secret]", text)


def _append_log(text: str) -> None:
    _ensure_state_dir()
    with _log_path().open("a", encoding="utf-8") as handle:
        handle.write(text)
    try:
        _log_path().chmod(0o600)
    except OSError:
        pass
    try:
        current = _log_path().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    if len(current) > MAX_LOG_CHARS:
        _log_path().write_text(current[-MAX_LOG_CHARS:], encoding="utf-8")


def _read_log(limit: int = 120) -> str:
    try:
        text = _log_path().read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return "No Codex auth log yet."
    lines = text.splitlines()
    if limit > 0:
        lines = lines[-limit:]
    return _redact_sensitive_text("\n".join(lines)).strip() or "Codex auth log is empty."


def _pid_is_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _running_worker_pid() -> int | None:
    try:
        pid = int(_pid_path().read_text(encoding="utf-8").strip())
    except (FileNotFoundError, OSError, ValueError):
        return None
    return pid if _pid_is_running(pid) else None


def _claim_start_lock(max_age_seconds: int = 60) -> bool:
    _ensure_state_dir()
    path = _start_lock_path()
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            age = 0
        if age <= max_age_seconds:
            return False
        try:
            path.unlink()
        except OSError:
            return False
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"{os.getpid()}\n")
    return True


def _release_start_lock() -> None:
    try:
        _start_lock_path().unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _telegram_credentials() -> tuple[str, str]:
    values = _telegram_env_values()
    token = (values.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (values.get("TELEGRAM_HOME_CHANNEL") or "").strip()
    if not chat_id:
        allowed_users = (values.get("TELEGRAM_ALLOWED_USERS") or "").strip()
        if allowed_users and "," not in allowed_users:
            chat_id = allowed_users
    if not token or not chat_id:
        raise RuntimeError("Telegram is not configured for this Hermes instance yet.")
    return token, chat_id


def _telegram_env_files() -> list[Path]:
    candidates: list[Path] = []
    explicit = (os.getenv("HERMES_ENV_FILE") or "").strip()
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(Path.home() / ".hermes" / ".env")
    project_dir = Path(
        (os.getenv("HERMES_PROJECT_DIR") or "/usr/local/lib/hermes-agent").strip()
    )
    candidates.append(project_dir / ".env")

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            unique.append(candidate)
            seen.add(key)
    return unique


def _parse_env_value(raw: str) -> str:
    value = raw.strip()
    if (
        len(value) >= 2
        and value[0] == value[-1]
        and value.startswith(("'", '"'))
    ):
        value = value[1:-1]
    return value.replace('\\"', '"').replace("\\\\", "\\")


def _telegram_env_values() -> dict[str, str]:
    values = {
        "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN") or "",
        "TELEGRAM_HOME_CHANNEL": os.getenv("TELEGRAM_HOME_CHANNEL") or "",
        "TELEGRAM_ALLOWED_USERS": os.getenv("TELEGRAM_ALLOWED_USERS") or "",
    }
    for path in _telegram_env_files():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            clean = line.strip()
            if not clean or clean.startswith("#") or "=" not in clean:
                continue
            key, raw_value = clean.split("=", 1)
            key = key.strip()
            if key in values and not values[key]:
                values[key] = _parse_env_value(raw_value)
    return values


def _telegram_send(
    text: str,
    *,
    button_text: str | None = None,
    button_url: str | None = None,
) -> dict[str, Any]:
    token, chat_id = _telegram_credentials()
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text[:3900],
        "disable_web_page_preview": True,
    }
    if button_text and button_url:
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": button_text, "url": button_url}]]
        }
    body = parse.urlencode(
        {
            key: json.dumps(value) if isinstance(value, dict) else str(value)
            for key, value in payload.items()
        }
    ).encode("utf-8")
    req = request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "tinyhat-hermes-runtime/telegram-codex-auth",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "http_status": exc.code, "description": detail[:500]}
    except error.URLError as exc:
        return {"ok": False, "description": str(exc.reason)[:500]}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "description": "Telegram returned invalid JSON."}
    return payload if isinstance(payload, dict) else {"ok": False}


def _is_likely_device_url(candidate: str) -> bool:
    try:
        parsed = parse.urlparse(candidate)
    except ValueError:
        return False
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    query = (parsed.query or "").lower()
    if not host:
        return False
    host_ok = (
        host == "openai.com"
        or host.endswith(".openai.com")
        or host == "microsoft.com"
        or host.endswith(".microsoft.com")
        or host == "microsoftonline.com"
        or host.endswith(".microsoftonline.com")
    )
    if not host_ok:
        return False
    path_and_query = f"{path}?{query}"
    return "device" in path_and_query or "deviceauth" in path_and_query


def _valid_device_code(candidate: str) -> bool:
    upper = candidate.upper()
    if upper in {"HTTPS", "HTTP", "OPENAI", "CODEX", "OAUTH", "PROVIDER"}:
        return False
    return not any(fragment in upper for fragment in ("CODEX", "OAUTH", "PROVIDER"))


def _extract_auth_material(text: str) -> dict[str, str | None]:
    text = ANSI_RE.sub("", text)
    urls = re.findall(r"https?://[^\s)>\]\"']+", text)
    url = None
    for candidate in urls:
        clean = candidate.rstrip(".,;:")
        if _is_likely_device_url(clean):
            url = clean
            break

    code = None
    code_text = text
    for candidate in urls:
        code_text = code_text.replace(candidate, " ")
    code_patterns = (
        r"(?i)(?:code|enter|paste)[^\nA-Z0-9]{0,50}([A-Z0-9]{4,}(?:-[A-Z0-9]{4,})+|[A-Z0-9]{8,12})\b",
        r"(?m)^\s*([A-Z0-9]{4,}(?:-[A-Z0-9]{4,})+|[A-Z0-9]{8,12})\s*$",
    )
    for pattern in code_patterns:
        match = re.search(pattern, code_text.upper())
        if match:
            candidate = match.group(1)
            if _valid_device_code(candidate):
                code = candidate
                break
    return {"url": url, "code": code}


def _send_auth_material(material: dict[str, str | None], provider: str) -> dict[str, Any]:
    url = material.get("url")
    code = material.get("code")
    deliveries: list[dict[str, Any]] = []
    if url:
        deliveries.append(
            _telegram_send(
                "OpenAI Codex auth is ready. Open the authorization page, then paste the code from the next message.",
                button_text="Open OpenAI auth",
                button_url=url,
            )
        )
    if code:
        deliveries.append(_telegram_send(str(code)))
    if not url and not code:
        deliveries.append(
            _telegram_send(
                f"I started Hermes Codex auth with `{provider}`, but I have not seen the device code yet. Send /codex_auth_log in a few seconds."
            )
        )
    ok = bool(deliveries) and all(bool(item.get("ok")) for item in deliveries)
    if not ok:
        _append_log(f"Telegram delivery failed for {provider}: {deliveries}\n")
    return {"ok": ok, "deliveries": deliveries}


def _terminal_text(text: str) -> str:
    return ANSI_RE.sub("", text).replace("\r", "\n")


def _provider_menu_delta(text: str) -> int | None:
    section_start = text.rfind("Select provider:")
    if section_start < 0:
        return None
    section = text[section_start:]
    if "Select OpenAI provider:" in section:
        return None

    selected_index: int | None = None
    openai_index: int | None = None
    index = -1
    for raw_line in section.splitlines():
        line = raw_line.strip()
        if "(○)" not in line and "(●)" not in line:
            continue
        index += 1
        if "→" in line or "(●)" in line:
            selected_index = index
        if "OpenAI" in line and "Codex CLI" in line:
            openai_index = index
    if selected_index is None or openai_index is None:
        return None
    return openai_index - selected_index


def _provider_menu_choice(text: str) -> str | None:
    section_start = text.rfind("Select provider:")
    if section_start < 0:
        return None
    section = text[section_start:]
    if "Select OpenAI provider:" in section:
        return None
    for raw_line in section.splitlines():
        line = raw_line.strip()
        if "OpenAI" not in line or "Codex CLI" not in line:
            continue
        match = re.search(r"(?:^|\D)(\d+)\.\s+OpenAI\b", line)
        if match:
            return match.group(1)
    return None


def _send_model_picker_key(master_fd: int, key: str) -> None:
    os.write(master_fd, key.encode("utf-8"))


def _send_enter(master_fd: int) -> None:
    _send_model_picker_key(master_fd, "\n")


def _send_arrows(master_fd: int, delta: int) -> None:
    if delta > 0:
        _send_model_picker_key(master_fd, "\x1b[B" * delta)
    elif delta < 0:
        _send_model_picker_key(master_fd, "\x1b[A" * abs(delta))


def _extract_selected_model(text: str) -> str | None:
    match = re.search(r"Default model set to:\s*([^\s(]+)", _terminal_text(text))
    return match.group(1) if match else None


def _run_config_switch(hermes_bin: Path) -> dict[str, Any]:
    started = time.monotonic()
    command = [str(hermes_bin), "model", "--no-browser"]
    master_fd, slave_fd = pty.openpty()
    process = subprocess.Popen(
        command,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        text=True,
        close_fds=True,
        start_new_session=True,
    )
    os.close(slave_fd)

    output = ""
    sent_provider = False
    sent_openai_provider = False
    sent_credentials = False
    sent_cli_import = False
    sent_model = False
    timed_out = False
    try:
        while True:
            if time.monotonic() - started > MODEL_PICKER_TIMEOUT_SECONDS:
                timed_out = True
                process.terminate()
                break

            ready, _, _ = select.select([master_fd], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master_fd, 4096).decode(
                        "utf-8",
                        errors="replace",
                    )
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if chunk:
                    output = (output + chunk)[-24_000:]
                else:
                    break

            clean = _terminal_text(output)
            if not sent_provider and "Select provider:" in clean and "OpenAI" in clean:
                choice = _provider_menu_choice(clean)
                if choice is not None:
                    _send_model_picker_key(master_fd, f"{choice}\n")
                    sent_provider = True
                else:
                    delta = _provider_menu_delta(clean)
                    if delta is None:
                        continue
                    _send_arrows(master_fd, delta)
                    _send_enter(master_fd)
                    sent_provider = True
            elif not sent_openai_provider and "Select OpenAI provider:" in clean:
                # OpenAI Codex is Hermes' first OpenAI provider option.
                _send_enter(master_fd)
                sent_openai_provider = True
            elif (
                not sent_cli_import
                and "Found existing Codex CLI credentials" in clean
                and "Import these credentials?" in clean
            ):
                _send_model_picker_key(master_fd, "y\n")
                sent_cli_import = True
                sent_credentials = True
            elif (
                not sent_credentials
                and "Existing Codex credentials found in Hermes auth store." in clean
                and "Use existing credentials?" in clean
            ):
                _send_enter(master_fd)
                sent_credentials = True
            elif not sent_credentials and "OpenAI Codex credentials:" in clean:
                # The device-code command has just completed; keep Hermes'
                # existing Codex credential and continue its formal picker.
                _send_enter(master_fd)
                sent_credentials = True
            elif not sent_model and "Select default model:" in clean:
                # Hermes places its recommended Codex model first.
                _send_enter(master_fd)
                sent_model = True

            if process.poll() is not None:
                break
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass

    if process.poll() is None:
        try:
            returncode = process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            returncode = process.wait(timeout=10)
            timed_out = True
    else:
        returncode = process.returncode
    selected_model = _extract_selected_model(output)
    return {
        "command": command,
        "source": "hermes model",
        "model_provider": MODEL_PROVIDER,
        "model_default": selected_model,
        "ok": returncode == 0 and not timed_out and bool(selected_model),
        "returncode": returncode,
        "timed_out": timed_out,
        "selections": {
            "provider": sent_provider,
            "openai_provider": sent_openai_provider,
            "credentials": sent_credentials,
            "cli_import": sent_cli_import,
            "model": sent_model,
        },
        "output": _redact_sensitive_text(_terminal_text(output)[-4000:]),
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


def _auth_status(hermes_bin: Path) -> dict[str, Any]:
    providers = (PRIMARY_PROVIDER, FALLBACK_PROVIDER)
    for provider in providers:
        started = time.monotonic()
        try:
            process = subprocess.run(
                [str(hermes_bin), "auth", "status", provider],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {"ok": False, "provider": provider, "message": str(exc)}
        text = f"{process.stdout}\n{process.stderr}".strip()
        if process.returncode == 0:
            return {
                "ok": True,
                "provider": provider,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "output": text[-2000:],
            }
    return {"ok": False, "provider": providers[-1], "output": text[-2000:]}


def _codex_cli_status(codex_bin: Path) -> dict[str, Any]:
    started = time.monotonic()
    try:
        process = subprocess.run(
            [str(codex_bin), "login", "status"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "message": str(exc)}
    text = f"{process.stdout}\n{process.stderr}".strip()
    return {
        "ok": process.returncode == 0,
        "returncode": process.returncode,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "output": _redact_sensitive_text(text[-2000:]),
    }


def _auth_command(hermes_bin: Path, provider: str) -> list[str]:
    return [
        str(hermes_bin),
        "auth",
        "add",
        provider,
        "--no-browser",
        "--timeout",
        str(AUTH_TIMEOUT_SECONDS),
    ]


def _codex_cli_login_command(codex_bin: Path) -> list[str]:
    return [str(codex_bin), "login", "--device-auth"]


def _restart_gateway_after_auth(hermes_bin: Path) -> dict[str, Any]:
    """Restart Hermes so the freshly written auth/config is used for chat."""

    try:
        from hermes_runtime.commands.configure_telegram import _run_gateway

        return asyncio.run(_run_gateway(hermes_bin))
    except Exception as exc:  # noqa: BLE001 - auth worker reports best-effort status.
        return {
            "healthy": False,
            "started": False,
            "message": str(exc),
            "failure_code": exc.__class__.__name__,
        }


def _completion_message(
    *,
    switch: dict[str, Any],
    gateway: dict[str, Any],
) -> str:
    if switch.get("ok") and gateway.get("healthy"):
        return (
            "OpenAI Codex auth is connected ✅\n\n"
            "I switched Hermes to OpenAI Codex and restarted my Telegram gateway, "
            "so your next message should use your OpenAI subscription."
        )
    if switch.get("ok"):
        return (
            "OpenAI Codex auth is connected ✅\n\n"
            "I switched Hermes to OpenAI Codex, but I could not confirm the Telegram gateway restart. "
            "Send /codex_auth_status if replies look stale."
        )
    return (
        "OpenAI Codex auth finished, but I could not switch Hermes to the Codex provider automatically. "
        "Send /codex_auth_log so we can inspect the last auth output."
    )


def _run_auth_once(hermes_bin: Path, provider: str) -> tuple[int, bool]:
    """Run one provider auth flow.

    Returns ``(returncode, saw_device_material)``.
    """

    _append_log(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] starting {provider}\n")
    master_fd, slave_fd = pty.openpty()
    process = subprocess.Popen(
        _auth_command(hermes_bin, provider),
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        text=True,
        close_fds=True,
        start_new_session=True,
    )
    os.close(slave_fd)
    saw_material = False
    buffer = ""
    _write_status(
        {
            "state": "waiting_for_user",
            "provider": provider,
            "auth_pid": process.pid,
            "message": "Waiting for the user to complete the device-code flow.",
        }
    )
    try:
        while True:
            ready, _, _ = select.select([master_fd], [], [], 0.5)
            if ready:
                try:
                    chunk = os.read(master_fd, 4096).decode(
                        "utf-8",
                        errors="replace",
                    )
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if chunk:
                    _append_log(chunk)
                    buffer = (buffer + chunk)[-8000:]
                    if not saw_material:
                        material = _extract_auth_material(buffer)
                        if material.get("url") or material.get("code"):
                            saw_material = True
                            _write_status(
                                {
                                    "state": "device_code_sent",
                                    "provider": provider,
                                    "auth_pid": process.pid,
                                    "has_url": bool(material.get("url")),
                                    "has_code": bool(material.get("code")),
                                }
                            )
                            delivery = _send_auth_material(material, provider)
                            if not delivery.get("ok"):
                                _write_status(
                                    {
                                        "state": "delivery_failed",
                                        "provider": provider,
                                        "auth_pid": process.pid,
                                        "has_url": bool(material.get("url")),
                                        "has_code": bool(material.get("code")),
                                        "message": "The auth code was found, but Telegram delivery failed.",
                                        "telegram_delivery": delivery,
                                    }
                                )
                else:
                    break
            if process.poll() is not None:
                if not ready:
                    break
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
    returncode = process.wait()
    _append_log(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {provider} exited {returncode}\n")
    return returncode, saw_material


def _run_codex_cli_login_once(codex_bin: Path) -> tuple[int, bool]:
    """Run Codex CLI device auth so ``codex app-server`` can read limits."""

    _append_log(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] starting codex cli login\n")
    master_fd, slave_fd = pty.openpty()
    process = subprocess.Popen(
        _codex_cli_login_command(codex_bin),
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        text=True,
        close_fds=True,
        start_new_session=True,
    )
    os.close(slave_fd)
    saw_material = False
    buffer = ""
    _write_status(
        {
            "state": "waiting_for_user",
            "provider": "codex-cli",
            "auth_pid": process.pid,
            "message": "Waiting for the user to complete the Codex CLI device-code flow.",
        }
    )
    try:
        while True:
            ready, _, _ = select.select([master_fd], [], [], 0.5)
            if ready:
                try:
                    chunk = os.read(master_fd, 4096).decode(
                        "utf-8",
                        errors="replace",
                    )
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if chunk:
                    _append_log(chunk)
                    buffer = (buffer + chunk)[-8000:]
                    if not saw_material:
                        material = _extract_auth_material(buffer)
                        if material.get("url") or material.get("code"):
                            saw_material = True
                            _write_status(
                                {
                                    "state": "device_code_sent",
                                    "provider": "codex-cli",
                                    "auth_pid": process.pid,
                                    "has_url": bool(material.get("url")),
                                    "has_code": bool(material.get("code")),
                                }
                            )
                            delivery = _send_auth_material(material, "Codex CLI")
                            if not delivery.get("ok"):
                                _write_status(
                                    {
                                        "state": "delivery_failed",
                                        "provider": "codex-cli",
                                        "auth_pid": process.pid,
                                        "has_url": bool(material.get("url")),
                                        "has_code": bool(material.get("code")),
                                        "message": "The Codex CLI auth code was found, but Telegram delivery failed.",
                                        "telegram_delivery": delivery,
                                    }
                                )
                else:
                    break
            if process.poll() is not None:
                if not ready:
                    break
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
    returncode = process.wait()
    _append_log(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] codex cli exited {returncode}\n")
    return returncode, saw_material


def _ensure_codex_cli_auth(codex_bin: Path) -> tuple[int, bool, dict[str, Any]]:
    """Ensure the official Codex CLI is logged in before Hermes switches."""

    status = _codex_cli_status(codex_bin)
    if status.get("ok"):
        return 0, False, status
    returncode, saw_material = _run_codex_cli_login_once(codex_bin)
    return returncode, saw_material, _codex_cli_status(codex_bin)


def worker() -> int:
    _ensure_state_dir()
    _pid_path().write_text(f"{os.getpid()}\n", encoding="utf-8")
    _release_start_lock()
    try:
        _pid_path().chmod(0o600)
    except OSError:
        pass

    hermes_bin = find_hermes_binary()
    if hermes_bin is None:
        _write_status({"state": "failed", "message": "Hermes CLI was not found."})
        _telegram_send("I could not start OpenAI Codex auth because Hermes CLI is not installed.")
        return 1

    codex_bin = find_codex_binary()
    if codex_bin is None:
        _write_status({"state": "failed", "message": "Codex CLI was not found."})
        _telegram_send(
            "I could not start OpenAI Codex auth because Codex CLI is not installed on this computer."
        )
        return 1

    try:
        codex_returncode, codex_saw_material, codex_status = _ensure_codex_cli_auth(codex_bin)
    except Exception as exc:  # noqa: BLE001 - worker must report to Telegram.
        _append_log(f"codex cli login failed: {exc}\n")
        codex_returncode = 1
        codex_saw_material = False
        codex_status = {"ok": False, "message": str(exc)}

    if codex_returncode != 0 or not codex_status.get("ok"):
        _write_status(
            {
                "state": "failed",
                "provider": "codex-cli",
                "returncode": codex_returncode,
                "codex_cli_status": codex_status,
                "message": "Codex CLI auth did not complete.",
            }
        )
        suffix = " Send /codex_auth_log to see the latest auth output." if codex_saw_material else ""
        _telegram_send(
            "OpenAI Codex CLI auth did not complete."
            f"{suffix} Run /codex_auth to try again."
        )
        return codex_returncode or 1

    switch = _run_config_switch(hermes_bin)
    if switch.get("ok"):
        gateway = _restart_gateway_after_auth(hermes_bin)
        status = _auth_status(hermes_bin)
        codex_status = _codex_cli_status(codex_bin)
        _write_status(
            {
                "state": "connected",
                "provider": PRIMARY_PROVIDER,
                "model_provider": MODEL_PROVIDER,
                "codex_cli_status": codex_status,
                "config_switch": switch,
                "gateway_restart": gateway,
                "auth_status": status,
                "message": "OpenAI Codex auth connected.",
            }
        )
        _telegram_send(_completion_message(switch=switch, gateway=gateway))
        return 0

    last_returncode = 1
    for provider in (PRIMARY_PROVIDER, FALLBACK_PROVIDER):
        try:
            last_returncode, saw_material = _run_auth_once(hermes_bin, provider)
        except Exception as exc:  # noqa: BLE001 - worker must report to Telegram.
            _append_log(f"{provider} failed: {exc}\n")
            last_returncode = 1
            saw_material = False
        if last_returncode == 0:
            switch = _run_config_switch(hermes_bin)
            gateway = _restart_gateway_after_auth(hermes_bin)
            status = _auth_status(hermes_bin)
            codex_status = _codex_cli_status(codex_bin)
            _write_status(
                {
                    "state": "connected",
                    "provider": provider,
                    "model_provider": MODEL_PROVIDER,
                    "codex_cli_status": codex_status,
                    "config_switch": switch,
                    "gateway_restart": gateway,
                    "auth_status": status,
                    "message": "OpenAI Codex auth connected.",
                }
            )
            _telegram_send(
                _completion_message(switch=switch, gateway=gateway)
            )
            return 0
        if saw_material:
            break

    _write_status(
        {
            "state": "failed",
            "provider": provider,
            "returncode": last_returncode,
            "message": "OpenAI Codex auth did not complete.",
        }
    )
    _telegram_send(
        "OpenAI Codex auth did not complete. Send /codex_auth_log to see the latest auth output, then run /codex_auth to try again."
    )
    return last_returncode or 1


def start() -> str:
    _ensure_state_dir()
    running_pid = _running_worker_pid()
    if running_pid:
        return (
            "OpenAI Codex auth is already running. I will send the auth link and completion message here."
        )
    if not _claim_start_lock():
        return (
            "OpenAI Codex auth is already starting. I will send the auth link and completion message here."
        )

    script = (
        "PYTHONPATH=\"${TINYHAT_RUNTIME_PREFIX:-/opt/tinyhat-hermes-runtime}:${PYTHONPATH:-}\" "
        "python3 -m hermes_runtime.telegram_codex_auth worker"
    )
    log_file = _log_path().open("a", encoding="utf-8")
    try:
        subprocess.Popen(
            ["bash", "-lc", script],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )
    except Exception:
        _release_start_lock()
        raise
    finally:
        log_file.close()
    return (
        "I am starting OpenAI Codex auth now. I will send the authorization link and code here in a moment."
    )


def status() -> str:
    payload = _read_status()
    running_pid = _running_worker_pid()
    hermes_bin = find_hermes_binary()
    auth_status = _auth_status(hermes_bin) if hermes_bin else None
    codex_bin = find_codex_binary()
    codex_status = _codex_cli_status(codex_bin) if codex_bin else None
    lines = ["OpenAI Codex auth status"]
    if running_pid:
        lines.append(f"Worker: running (pid {running_pid})")
    else:
        lines.append("Worker: not running")
    if payload:
        lines.append(f"State: {payload.get('state') or 'unknown'}")
        if payload.get("message"):
            lines.append(f"Message: {payload['message']}")
    if auth_status:
        lines.append(f"Hermes auth: {'ok' if auth_status.get('ok') else 'not connected'}")
        if auth_status.get("provider"):
            lines.append(f"Provider checked: {auth_status['provider']}")
    if codex_status:
        lines.append(f"Codex CLI auth: {'ok' if codex_status.get('ok') else 'not connected'}")
    elif not codex_bin:
        lines.append("Codex CLI auth: codex command not found")
    return "\n".join(lines)


def log() -> str:
    return _read_log()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "status", "log", "worker"))
    args = parser.parse_args(argv)
    if args.action == "worker":
        return worker()
    if args.action == "start":
        print(start())
        return 0
    if args.action == "status":
        print(status())
        return 0
    if args.action == "log":
        print(log())
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
