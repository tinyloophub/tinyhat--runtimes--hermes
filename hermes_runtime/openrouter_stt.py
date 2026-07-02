"""OpenRouter speech-to-text command bridge for Hermes command STT providers."""

from __future__ import annotations

import argparse
import base64
from contextlib import suppress
import json
import os
from pathlib import Path
import sys
from typing import Any
from urllib import error, request

DEFAULT_MODEL = "openai/whisper-large-v3-turbo"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_TIMEOUT_SECONDS = 120.0
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


def _request_transcript(
    *,
    audio_path: Path,
    audio_format: str,
    model: str,
    language: str,
    timeout_seconds: float,
) -> str:
    api_key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not available to Hermes STT.")

    base_url = (
        os.environ.get("OPENROUTER_BASE_URL") or DEFAULT_BASE_URL
    ).strip().rstrip("/")
    audio_b64 = base64.b64encode(audio_path.read_bytes()).decode("ascii")
    payload: dict[str, Any] = {
        "model": model,
        "input_audio": {
            "data": audio_b64,
            "format": audio_format,
        },
    }
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
        or (os.environ.get("TINYHAT_HERMES_OPENROUTER_STT_MODEL") or "").strip()
        or DEFAULT_MODEL
    )
    timeout_seconds = (
        float(args.timeout)
        if args.timeout is not None
        else float(
            os.environ.get("TINYHAT_HERMES_OPENROUTER_STT_TIMEOUT_SECONDS")
            or DEFAULT_TIMEOUT_SECONDS
        )
    )
    try:
        transcript = _request_transcript(
            audio_path=audio_path,
            audio_format=_audio_format(audio_path, str(args.format or "")),
            model=model,
            language=str(args.language or ""),
            timeout_seconds=timeout_seconds,
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
