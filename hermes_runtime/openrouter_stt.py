"""OpenRouter speech-to-text command bridge for Hermes command STT providers."""

from __future__ import annotations

import argparse
import base64
from contextlib import suppress
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
from urllib import error, request

from hermes_runtime.runtime_env import env_file_candidates, read_env_values

DEFAULT_MODEL = "openai/gpt-4o-transcribe"
DEFAULT_FALLBACK_MODELS = (
    "openai/gpt-4o-mini-transcribe",
    "mistralai/voxtral-mini-transcribe",
    "qwen/qwen3-asr-flash-2026-02-10",
    "openai/whisper-1",
)
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_LOCAL_FALLBACK_MODEL = "medium"
DEFAULT_LOCAL_FALLBACK_TIMEOUT_SECONDS = 240.0
LANGUAGE_AUTO_VALUES = {"", "auto", "detect", "none", "null", "und", "undefined"}
AUDIO_FORMAT_BY_SUFFIX = {
    ".aac": "aac",
    ".flac": "flac",
    ".m4a": "m4a",
    ".mp3": "mp3",
    ".oga": "ogg",
    ".ogg": "ogg",
    ".opus": "ogg",
    ".wav": "wav",
    ".webm": "webm",
}


def _audio_format(path: Path, explicit: str | None) -> str:
    clean = (explicit or "").strip().lower().lstrip(".")
    if clean and clean not in {"txt", "json", "srt", "vtt"}:
        return clean
    return AUDIO_FORMAT_BY_SUFFIX.get(path.suffix.lower(), "wav")


def _read_error_body(exc: error.HTTPError) -> str:
    with suppress(Exception):
        return exc.read().decode("utf-8", errors="replace")[:1000]
    return ""


def _extract_error_message(payload: Any) -> str:
    if isinstance(payload, dict):
        error_payload = payload.get("error")
        if isinstance(error_payload, dict):
            message = str(error_payload.get("message") or "").strip()
            code = str(error_payload.get("code") or error_payload.get("type") or "").strip()
            if message and code:
                return f"{code}: {message}"
            if message:
                return message
        message = str(payload.get("message") or "").strip()
        if message:
            return message
    return ""


def _get_env_value(name: str, default: str = "") -> str:
    process_value = (os.environ.get(name) or "").strip()
    if process_value:
        return process_value
    try:
        value = read_env_values(env_file_candidates(), names=[name]).get(name, "")
    except Exception:
        return default
    return (value or default).strip()


def _parse_model_list(raw: str) -> list[str]:
    if not raw.strip():
        return []
    models: list[str] = []
    for chunk in raw.replace("\n", ",").replace(";", ",").split(","):
        model = chunk.strip()
        if model:
            models.append(model)
    return models


def _fallback_models(primary_model: str, explicit: str = "") -> list[str]:
    candidates = (
        _parse_model_list(explicit)
        or _parse_model_list(
            _get_env_value("TINYHAT_HERMES_OPENROUTER_STT_FALLBACK_MODELS")
        )
        or list(DEFAULT_FALLBACK_MODELS)
    )
    fallback_models: list[str] = []
    seen = {primary_model.strip()}
    for model in candidates:
        if model in seen:
            continue
        seen.add(model)
        fallback_models.append(model)
    return fallback_models


def _as_bool(raw: str, *, default: bool) -> bool:
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _local_fallback_enabled() -> bool:
    return _as_bool(
        _get_env_value("TINYHAT_HERMES_OPENROUTER_STT_LOCAL_FALLBACK", "true"),
        default=True,
    )


def _local_fallback_model(explicit: str = "") -> str:
    return (
        explicit.strip()
        or _get_env_value("TINYHAT_HERMES_LOCAL_STT_MODEL")
        or DEFAULT_LOCAL_FALLBACK_MODEL
    )


def _local_fallback_timeout_seconds() -> float:
    raw = _get_env_value("TINYHAT_HERMES_LOCAL_STT_FALLBACK_TIMEOUT_SECONDS")
    if not raw:
        return DEFAULT_LOCAL_FALLBACK_TIMEOUT_SECONDS
    try:
        timeout = float(raw)
    except ValueError:
        return DEFAULT_LOCAL_FALLBACK_TIMEOUT_SECONDS
    return max(30.0, timeout)


def _hermes_project_dir() -> Path:
    return Path(
        _get_env_value("HERMES_PROJECT_DIR", "/usr/local/lib/hermes-agent")
    ).expanduser()


def _hermes_python() -> Path:
    configured = _get_env_value("TINYHAT_HERMES_AGENT_PYTHON")
    if configured:
        return Path(configured).expanduser()
    return _hermes_project_dir() / "venv" / "bin" / "python"


def _request_local_transcript(
    *,
    audio_path: Path,
    model: str,
    language: str,
    timeout_seconds: float,
) -> str:
    python_bin = _hermes_python()
    if not python_bin.is_file():
        raise RuntimeError(f"local Whisper fallback Python not found at {python_bin}")

    script = """
import sys
from faster_whisper import WhisperModel

audio_path, model_name, language = sys.argv[1:4]
kwargs = {}
if language.strip().lower() not in {"", "auto", "detect", "none", "null", "und", "undefined"}:
    kwargs["language"] = language.strip()
model = WhisperModel(model_name, device="cpu", compute_type="int8")
segments, _info = model.transcribe(audio_path, beam_size=5, vad_filter=True, **kwargs)
text = "".join(segment.text for segment in segments).strip()
if not text:
    raise SystemExit("Local Whisper returned an empty transcript.")
print(text)
"""
    try:
        completed = subprocess.run(
            [str(python_bin), "-c", script, str(audio_path), model, language],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            env={
                **os.environ,
                "HF_HUB_DISABLE_TELEMETRY": "1",
            },
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"local Whisper fallback timed out after {timeout_seconds:g}s"
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"local Whisper fallback failed to start: {exc}") from exc

    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "no output").strip()
        raise RuntimeError(
            "local Whisper fallback failed"
            + (f": {detail[:500]}" if detail else "")
        )
    transcript = completed.stdout.strip()
    if not transcript:
        raise RuntimeError("local Whisper fallback returned an empty transcript.")
    return transcript


def _request_transcript(
    *,
    audio_path: Path,
    audio_format: str,
    model: str,
    fallback_models: list[str],
    language: str,
    timeout_seconds: float,
) -> str:
    api_key = _get_env_value("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not available to Hermes STT.")

    base_url = _get_env_value("OPENROUTER_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    audio_b64 = base64.b64encode(audio_path.read_bytes()).decode("ascii")
    payload: dict[str, Any] = {
        "model": model,
        "input_audio": {
            "data": audio_b64,
            "format": audio_format,
        },
    }
    if fallback_models:
        payload["models"] = fallback_models
    if language.strip().lower() not in LANGUAGE_AUTO_VALUES:
        payload["language"] = language.strip()

    req = request.Request(
        f"{base_url}/audio/transcriptions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "tinyhat-hermes-runtime/openrouter-stt",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        detail = _read_error_body(exc)
        with suppress(json.JSONDecodeError):
            parsed = json.loads(detail)
            detail = _extract_error_message(parsed) or detail
        raise RuntimeError(
            f"OpenRouter STT failed ({exc.code}): {detail or 'no response body'}"
        ) from exc
    except error.URLError as exc:
        raise RuntimeError(f"OpenRouter STT request failed: {exc.reason}") from exc

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("OpenRouter STT returned invalid JSON.") from exc
    text = ""
    if isinstance(parsed, dict):
        text = str(parsed.get("text") or "").strip()
    if not text:
        raise RuntimeError("OpenRouter STT returned an empty transcript.")
    return text


def _request_transcript_with_fallbacks(
    *,
    audio_path: Path,
    audio_format: str,
    model: str,
    fallback_models: list[str],
    language: str,
    timeout_seconds: float,
    local_fallback_model: str,
) -> str:
    try:
        return _request_transcript(
            audio_path=audio_path,
            audio_format=audio_format,
            model=model,
            fallback_models=fallback_models,
            language=language,
            timeout_seconds=timeout_seconds,
        )
    except Exception as openrouter_exc:
        if not _local_fallback_enabled():
            raise
        try:
            return _request_local_transcript(
                audio_path=audio_path,
                model=local_fallback_model,
                language=language,
                timeout_seconds=_local_fallback_timeout_seconds(),
            )
        except Exception as local_exc:
            raise RuntimeError(
                "OpenRouter STT failed and local Whisper fallback also failed: "
                f"OpenRouter: {openrouter_exc}; local: {local_exc}"
            ) from local_exc


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Audio file to transcribe.")
    parser.add_argument("--output", help="Transcript output path.")
    parser.add_argument(
        "--format",
        default="",
        help="Audio format hint or Hermes output format placeholder.",
    )
    parser.add_argument("--language", default="auto")
    parser.add_argument("--model", default="")
    parser.add_argument(
        "--fallback-models",
        default="",
        help="Comma-separated OpenRouter STT fallback models in priority order.",
    )
    parser.add_argument("--local-fallback-model", default="")
    parser.add_argument("--timeout", type=float, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    audio_path = Path(str(args.input)).expanduser()
    if not audio_path.is_file():
        print(f"Audio file not found: {audio_path}", file=sys.stderr)
        return 2

    model = (
        str(args.model or "").strip()
        or _get_env_value("TINYHAT_HERMES_OPENROUTER_STT_MODEL")
        or DEFAULT_MODEL
    )
    timeout_seconds = (
        float(args.timeout)
        if args.timeout is not None
        else float(
            _get_env_value("TINYHAT_HERMES_OPENROUTER_STT_TIMEOUT_SECONDS")
            or DEFAULT_TIMEOUT_SECONDS
        )
    )
    try:
        transcript = _request_transcript_with_fallbacks(
            audio_path=audio_path,
            audio_format=_audio_format(audio_path, str(args.format or "")),
            model=model,
            fallback_models=_fallback_models(model, str(args.fallback_models or "")),
            language=str(args.language or ""),
            timeout_seconds=timeout_seconds,
            local_fallback_model=_local_fallback_model(
                str(args.local_fallback_model or "")
            ),
        )
    except Exception as exc:  # noqa: BLE001 - command provider needs stderr + code.
        print(str(exc), file=sys.stderr)
        return 1

    output = str(args.output or "").strip()
    if output:
        output_path = Path(output).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(transcript + "\n", encoding="utf-8")
    print(transcript)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
