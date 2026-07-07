"""SkyAI DEV voice audio provider configuration.

This module is intentionally provider/config only.  It does not call OpenAI,
does not touch SIP/RTP/PBX, and never returns API key material.  The media
gateway can use this as a shared preflight vocabulary before it wires real
STT/TTS calls around the SkyAI `/voice/*` text contract.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Mapping


OPENAI_AUDIO_KEY_ENV = "VOICE_TOOLS_OPENAI_KEY"
OPENAI_AUDIO_BASE_URL = "https://api.openai.com/v1"

OPENAI_STT_PRIMARY_MODEL = "gpt-4o-transcribe"
OPENAI_STT_FAST_MODEL = "gpt-4o-mini-transcribe"
OPENAI_TTS_MODEL = "gpt-4o-mini-tts"
OPENAI_TTS_VOICE = "marin"
OPENAI_TTS_FALLBACK_VOICE = "alloy"
OPENAI_REALTIME_MODEL = "gpt-realtime-2"
OPENAI_REALTIME_TRANSCRIPTION_MODEL = "gpt-realtime-whisper"

VOICE_AUDIO_PROVIDER_LANE = "hybrid_openai_api_audio_codex_oauth_reasoning"


@dataclass(frozen=True)
class VoiceAudioSettings:
    provider_lane: str
    api_key_env: str
    api_key_configured: bool
    base_url: str
    stt_primary_model: str
    stt_fast_model: str
    tts_model: str
    tts_voice: str
    tts_fallback_voice: str
    realtime_model: str
    realtime_transcription_model: str
    request_timeout_seconds: float


def _env_value(env: Mapping[str, str], name: str, default: str = "") -> str:
    return str(env.get(name, default) or "").strip()


def _env_float(env: Mapping[str, str], name: str, default: float) -> float:
    value = _env_value(env, name)
    if not value:
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    return max(1.0, min(parsed, 120.0))


def load_voice_audio_settings(
    env: Mapping[str, str] | None = None,
) -> VoiceAudioSettings:
    """Load SkyAI DEV OpenAI audio settings without exposing secrets.

    Use a voice-specific secret (`VOICE_TOOLS_OPENAI_KEY`) rather than the
    generic `OPENAI_API_KEY`.  SkyAI reasoning remains on the Hermes/Codex
    OAuth lane; this key is only for STT/TTS audio calls.
    """

    env = env or os.environ
    return VoiceAudioSettings(
        provider_lane=VOICE_AUDIO_PROVIDER_LANE,
        api_key_env=OPENAI_AUDIO_KEY_ENV,
        api_key_configured=bool(_env_value(env, OPENAI_AUDIO_KEY_ENV)),
        base_url=_env_value(env, "SKYAI_VOICE_OPENAI_BASE_URL", OPENAI_AUDIO_BASE_URL),
        stt_primary_model=_env_value(env, "SKYAI_VOICE_STT_MODEL", OPENAI_STT_PRIMARY_MODEL),
        stt_fast_model=_env_value(env, "SKYAI_VOICE_STT_FAST_MODEL", OPENAI_STT_FAST_MODEL),
        tts_model=_env_value(env, "SKYAI_VOICE_TTS_MODEL", OPENAI_TTS_MODEL),
        tts_voice=_env_value(env, "SKYAI_VOICE_TTS_VOICE", OPENAI_TTS_VOICE),
        tts_fallback_voice=_env_value(env, "SKYAI_VOICE_TTS_FALLBACK_VOICE", OPENAI_TTS_FALLBACK_VOICE),
        realtime_model=_env_value(env, "SKYAI_VOICE_REALTIME_MODEL", OPENAI_REALTIME_MODEL),
        realtime_transcription_model=_env_value(
            env,
            "SKYAI_VOICE_REALTIME_TRANSCRIPTION_MODEL",
            OPENAI_REALTIME_TRANSCRIPTION_MODEL,
        ),
        request_timeout_seconds=_env_float(env, "SKYAI_VOICE_OPENAI_TIMEOUT_SECONDS", 30.0),
    )


def voice_audio_preflight(settings: VoiceAudioSettings | None = None) -> dict[str, object]:
    settings = settings or load_voice_audio_settings()
    return {
        "status": "pass" if settings.api_key_configured else "blocked",
        "provider_lane": settings.provider_lane,
        "reasoning_auth": "hermes_codex_oauth_pro",
        "audio_auth": "openai_api_key",
        "api_key": {
            "configured": settings.api_key_configured,
            "env": settings.api_key_env,
            "value_printed": False,
        },
        "openai": {
            "base_url": settings.base_url,
            "stt_primary_model": settings.stt_primary_model,
            "stt_fast_model": settings.stt_fast_model,
            "tts_model": settings.tts_model,
            "tts_voice": settings.tts_voice,
            "tts_fallback_voice": settings.tts_fallback_voice,
            "realtime_model": settings.realtime_model,
            "realtime_transcription_model": settings.realtime_transcription_model,
            "timeout_seconds": settings.request_timeout_seconds,
        },
        "voice_gateway_contract": {
            "audio_layer_only": True,
            "reasoning_path_unchanged": True,
            "raw_audio_stored_by_default": False,
            "customer_mutations_allowed": False,
        },
        "latency_masking": {
            "gateway_owned": True,
            "first_filler_after_ms": 900,
            "slow_turn_filler_after_ms": 5500,
            "examples_bg": [
                "Проверявам.",
                "Гледам свободните варианти.",
                "Още секунда, почти съм готов.",
            ],
        },
    }
