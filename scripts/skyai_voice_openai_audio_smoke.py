#!/usr/bin/env python3
"""DEV-only live OpenAI STT/TTS smoke for SkyAI Voice.

This script verifies the approved hybrid lane:

    OpenAI API TTS -> OpenAI API STT -> SkyAI reasoning remains separate

It does not call SkyAI, PBX, SIP, RTP, Discord, Shopify, orders, vouchers, or
production traffic.  It requires an explicit `--live-openai` flag because it
makes billable OpenAI API calls when a real `VOICE_TOOLS_OPENAI_KEY` is set.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plugins.skyai_customer.voice_audio import load_voice_audio_settings


DEFAULT_BG_TEXT = "Здравейте, това е кратък тест на гласа на SkyAI."


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-openai", action="store_true", help="Allow real OpenAI API calls")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    parser.add_argument("--text", default=DEFAULT_BG_TEXT, help="Bulgarian sample text for TTS")
    parser.add_argument("--keep-audio", action="store_true", help="Keep generated audio under --output-dir")
    parser.add_argument("--output-dir", default="", help="Directory for generated smoke audio when kept")
    return parser.parse_args(argv)


def _auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _post_tts(settings: Any, api_key: str, text: str, voice: str) -> tuple[bytes, float]:
    started = time.perf_counter()
    response = requests.post(
        f"{settings.base_url}/audio/speech",
        headers=_auth_headers(api_key),
        json={
            "model": settings.tts_model,
            "voice": voice,
            "input": text,
            "response_format": "mp3",
        },
        timeout=settings.request_timeout_seconds,
    )
    response.raise_for_status()
    return response.content, (time.perf_counter() - started) * 1000


def _post_stt(settings: Any, api_key: str, audio_bytes: bytes) -> tuple[str, float]:
    started = time.perf_counter()
    response = requests.post(
        f"{settings.base_url}/audio/transcriptions",
        headers=_auth_headers(api_key),
        data={
            "model": settings.stt_primary_model,
            "language": "bg",
            "response_format": "json",
        },
        files={
            "file": ("skyai-voice-openai-smoke.mp3", BytesIO(audio_bytes), "audio/mpeg"),
        },
        timeout=settings.request_timeout_seconds,
    )
    response.raise_for_status()
    payload = response.json()
    return str(payload.get("text", "")).strip(), (time.perf_counter() - started) * 1000


def run_live_smoke(args: argparse.Namespace) -> dict[str, Any]:
    settings = load_voice_audio_settings()
    api_key = settings.api_key_env and os.environ.get(settings.api_key_env, "")
    if not api_key:
        return {
            "status": "blocked",
            "reason": "missing_voice_tools_openai_key",
            "api_key": {
                "configured": False,
                "env": settings.api_key_env,
                "value_printed": False,
            },
            "settings": {
                **asdict(settings),
                "api_key_configured": False,
            },
        }
    if not args.live_openai:
        return {
            "status": "blocked",
            "reason": "live_openai_flag_required",
            "api_key": {
                "configured": True,
                "env": settings.api_key_env,
                "value_printed": False,
            },
            "settings": {
                **asdict(settings),
                "api_key_configured": True,
            },
        }

    voice_used = settings.tts_voice
    try:
        audio_bytes, tts_latency_ms = _post_tts(settings, api_key, args.text, settings.tts_voice)
    except requests.HTTPError:
        if settings.tts_fallback_voice == settings.tts_voice:
            raise
        voice_used = settings.tts_fallback_voice
        audio_bytes, tts_latency_ms = _post_tts(settings, api_key, args.text, settings.tts_fallback_voice)

    transcript, stt_latency_ms = _post_stt(settings, api_key, audio_bytes)

    kept_audio_path = ""
    if args.keep_audio:
        output_dir = Path(args.output_dir) if args.output_dir else Path(tempfile.mkdtemp(prefix="skyai-voice-smoke-"))
        output_dir.mkdir(parents=True, exist_ok=True)
        kept_audio_path = str(output_dir / "skyai-voice-openai-smoke.mp3")
        Path(kept_audio_path).write_bytes(audio_bytes)

    return {
        "status": "pass",
        "provider_lane": settings.provider_lane,
        "api_key": {
            "configured": True,
            "env": settings.api_key_env,
            "value_printed": False,
        },
        "models": {
            "tts": settings.tts_model,
            "tts_voice": voice_used,
            "stt": settings.stt_primary_model,
        },
        "sample_text": args.text,
        "transcript": transcript,
        "latency_ms": {
            "tts": round(tts_latency_ms),
            "stt": round(stt_latency_ms),
            "total": round(tts_latency_ms + stt_latency_ms),
        },
        "audio": {
            "bytes": len(audio_bytes),
            "stored": bool(kept_audio_path),
            "path": kept_audio_path,
        },
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = run_live_smoke(args)
    except Exception as exc:
        result = {
            "status": "fail",
            "error_class": type(exc).__name__,
            "error": str(exc),
            "secret_printed": False,
        }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"SkyAI OpenAI audio live smoke: {str(result['status']).upper()}")
        if result["status"] == "pass":
            print(f"models=tts:{result['models']['tts']} voice:{result['models']['tts_voice']} stt:{result['models']['stt']}")
            print(f"audio_bytes={result['audio']['bytes']} stored={str(result['audio']['stored']).lower()}")
            print(f"latency_ms={result['latency_ms']}")
            print(f"transcript={result['transcript']}")
        else:
            print(f"reason={result.get('reason') or result.get('error_class')}")
            if "api_key" in result:
                print(f"api_key_configured={str(result['api_key']['configured']).lower()} env={result['api_key']['env']} value_printed=false")

    return 0 if result["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
