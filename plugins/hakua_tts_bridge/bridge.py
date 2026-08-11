"""Loopback OpenAI-compatible TTS bridge for Hakua voices."""
from __future__ import annotations

import json
import mimetypes
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
HOST = os.environ.get("HAKUA_TTS_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("HAKUA_TTS_BRIDGE_PORT", "8765"))
_lock = Lock()


def _tts_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly
        data = load_config_readonly() or {}
        section = data.get("tts") or {}
        return dict(section) if isinstance(section, dict) else {}
    except Exception:
        return {}


def _provider() -> str:
    configured = os.environ.get("HAKUA_TTS_PROVIDER") or _tts_config().get("provider") or "fishaudio"
    return str(configured).strip().lower().replace("-tts", "")


def _synthesize(payload: dict[str, Any]) -> tuple[bytes, str, dict[str, Any]]:
    text = str(payload.get("input") or payload.get("text") or "").strip()
    if not text:
        raise ValueError("input must not be empty")
    voice = str(payload.get("voice") or "hakua")
    model = str(payload.get("model") or "").strip() or None
    speed = payload.get("speed")
    requested = str(payload.get("response_format") or "").lower().strip()
    provider = _provider()
    with _lock:
        if provider in {"fish", "fishaudio", "fish_audio"}:
            from plugins.fish_audio_tts.core import synthesize_text
            cfg = _tts_config().get("fishaudio") or {}
            fmt = requested if requested in {"mp3", "wav", "opus", "pcm"} else str(cfg.get("format") or "mp3")
            result = synthesize_text(text, voice=voice, model=model, output_format=fmt, speed=speed)
        elif provider in {"irodori", "irodori_tts"}:
            from plugins.irodori_tts.core import synthesize_text
            cfg = _tts_config().get("irodori") or {}
            # `hakua` is the logical voice/model exposed to AIRI and ST;
            # Irodori's API model remains its actual engine identifier.
            irodori_model = str(cfg.get("model") or "irodori-tts")
            if not model or model.lower() in {"hakua", "hakua-tts"}:
                model = irodori_model
            fmt = requested if requested in {"wav", "mp3", "flac", "opus", "aac", "pcm"} else "wav"
            result = synthesize_text(text, voice=voice, model=model, output_format=fmt, speed=speed)
        else:
            raise RuntimeError(f"unsupported TTS provider: {provider}")
    path = Path(str(result["file_path"]))
    return path.read_bytes(), path.suffix.lower().lstrip("."), {
        "provider": provider,
        "voice": result.get("voice") or voice,
        "model": result.get("model") or model,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "HakuaTTSBridge/0.1"

    def log_message(self, *_args: Any) -> None:
        return

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _json(self, status: int, data: dict[str, Any]) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in {"", "/health", "/v1/health"}:
            self._json(200, {"ok": True, "service": "hakua-tts-bridge", "provider": _provider()})
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/v1/audio/speech":
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            audio, fmt, meta = _synthesize(payload)
            content_type = mimetypes.types_map.get(f".{fmt}", "application/octet-stream")
            if fmt == "pcm":
                content_type = "audio/pcm"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("X-Hakua-TTS-Provider", meta["provider"])
            self.send_header("Content-Length", str(len(audio)))
            self.end_headers()
            self.wfile.write(audio)
        except Exception as exc:
            self._json(502, {"ok": False, "error": str(exc)})


def main() -> int:
    with ThreadingHTTPServer((HOST, PORT), Handler) as server:
        print(f"Hakua TTS bridge listening on http://{HOST}:{PORT}", flush=True)
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
