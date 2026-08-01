"""One-shot faster-whisper worker for transient local STT.

The gateway launches this module once per local transcription.  The request and
response are JSON files in a private temporary directory; no shell command or
user-controlled executable is involved.  The process exits after one request so
native faster-whisper/CTranslate2/PyTorch allocations belong to the child and
are reclaimed by the OS on Windows.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_AUDIO_BYTES = 512 * 1024 * 1024
MAX_MODEL_NAME_BYTES = 256
MAX_DEVICE_NAME_BYTES = 64
MAX_COMPUTE_TYPE_BYTES = 64


def _failure(error: str) -> dict[str, Any]:
    return {"success": False, "transcript": "", "provider": "local", "error": error}


def _read_json(path: Path, max_bytes: int) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError("worker request file is missing")
    if path.stat().st_size > max_bytes:
        raise ValueError("worker request is too large")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid worker request: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("worker request must be a JSON object")
    return payload


def _bounded_string(payload: dict[str, Any], key: str, max_bytes: int, *, required: bool = True) -> str:
    value = payload.get(key)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(f"worker request field {key!r} must be text")
    if len(value.encode("utf-8")) > max_bytes:
        raise ValueError(f"worker request field {key!r} is too large")
    return value.strip()


def _validate_request(payload: dict[str, Any]) -> tuple[str, str, str, str, dict[str, Any], dict[str, Any]]:
    if payload.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("unsupported worker protocol version")

    input_path = _bounded_string(payload, "input_path", 4096)
    audio = Path(input_path)
    if not audio.is_file():
        raise ValueError("worker input audio is missing")
    if audio.is_symlink():
        raise ValueError("worker input audio must not be a symbolic link")
    if audio.stat().st_size > MAX_AUDIO_BYTES:
        raise ValueError("worker input audio is too large")

    model = _bounded_string(payload, "model", MAX_MODEL_NAME_BYTES)
    device = _bounded_string(payload, "device", MAX_DEVICE_NAME_BYTES, required=False) or "auto"
    compute_type = _bounded_string(
        payload, "compute_type", MAX_COMPUTE_TYPE_BYTES, required=False
    ) or "auto"

    transcribe_kwargs = payload.get("transcribe_kwargs", {})
    if not isinstance(transcribe_kwargs, dict):
        raise ValueError("worker transcribe_kwargs must be an object")
    local_config = payload.get("local_config", {})
    if not isinstance(local_config, dict):
        raise ValueError("worker local_config must be an object")
    return input_path, model, device, compute_type, transcribe_kwargs, local_config


def _transcribe(payload: dict[str, Any]) -> dict[str, Any]:
    (
        input_path,
        model_name,
        device,
        compute_type,
        transcribe_kwargs,
        local_config,
    ) = _validate_request(payload)

    # These imports happen only in the transient worker.  The gateway process
    # never imports faster-whisper just to run worker-mode STT.
    from tools.transcription_tools import _join_confident_segments, _load_local_whisper_model

    model = _load_local_whisper_model(
        model_name,
        device=device,
        compute_type=compute_type,
    )
    segments, _info = model.transcribe(input_path, **transcribe_kwargs)
    transcript = _join_confident_segments(segments, local_config)
    return {"success": True, "transcript": transcript, "provider": "local"}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_RESPONSE_BYTES:
        payload = _failure("worker response is too large")
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="response-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            Path(temp_name).chmod(0o600)
        except OSError:
            pass
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def run(request_path: Path, response_path: Path) -> int:
    """Process one request and write one response; return a process status."""
    try:
        payload = _read_json(request_path, MAX_REQUEST_BYTES)
        result = _transcribe(payload)
    except Exception as exc:
        result = _failure(str(exc))

    try:
        _write_json(response_path, result)
    except Exception:
        # The parent treats a nonzero status as a child failure.  Do not print
        # arbitrary model/provider output to the gateway's inherited streams.
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one transient local STT transcription")
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--response", required=True, type=Path)
    args = parser.parse_args(argv)
    return run(args.request, args.response)


if __name__ == "__main__":
    sys.exit(main())
