"""SkyAI DEV voice audio provider configuration.

This module is intentionally provider/config only.  It does not call OpenAI,
does not touch SIP/RTP/PBX, and never returns API key material.  The media
gateway can use this as a shared preflight vocabulary before it wires real
STT/TTS calls around the SkyAI `/voice/*` text contract.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from typing import Mapping


OPENAI_AUDIO_KEY_ENV = "VOICE_TOOLS_OPENAI_KEY"
OPENAI_AUDIO_BASE_URL = "https://api.openai.com/v1"

OPENAI_STT_PRIMARY_MODEL = "gpt-4o-transcribe"
OPENAI_STT_FAST_MODEL = "gpt-4o-mini-transcribe"
OPENAI_TTS_MODEL = "gpt-4o-mini-tts"
OPENAI_TTS_VOICE = "marin"
OPENAI_TTS_FALLBACK_VOICE = "alloy"
OPENAI_REALTIME_API_URL = "wss://api.openai.com/v1/realtime"
OPENAI_REALTIME_MODEL = "gpt-realtime-2.1"
OPENAI_REALTIME_FALLBACK_MODEL = "gpt-realtime-2"
OPENAI_REALTIME_TRANSCRIPTION_MODEL = "gpt-realtime-whisper"
OPENAI_REALTIME_VOICE = "marin"
OPENAI_REALTIME_FALLBACK_VOICE = "cedar"
OPENAI_REALTIME_AUDIO_FORMAT = "pcm16"
OPENAI_REALTIME_TURN_DETECTION = "server_vad"
OPENAI_REALTIME_REASONING_EFFORT = "low"

VOICE_AUDIO_PROVIDER_LANE = "hybrid_openai_api_audio_codex_oauth_reasoning"
VOICE_REALTIME_PROVIDER_LANE = "openai_realtime_speech_to_speech_skyai_v2_tools"


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
    realtime_api_url: str
    realtime_model: str
    realtime_fallback_model: str
    realtime_transcription_model: str
    realtime_voice: str
    realtime_fallback_voice: str
    realtime_input_audio_format: str
    realtime_output_audio_format: str
    realtime_turn_detection: str
    realtime_reasoning_effort: str
    realtime_backend_target: str
    request_timeout_seconds: float


def _env_value(
    env: Mapping[str, str],
    name: str,
    default: str = "",
    *,
    allow_empty: bool = True,
) -> str:
    if name not in env:
        value = default
    else:
        value = env[name]
    if type(value) is not str:
        raise ValueError(f"{name} must be an exact string")
    if not allow_empty and not value:
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _env_float(env: Mapping[str, str], name: str, default: float) -> float:
    if name not in env:
        return default
    value = _env_value(env, name, allow_empty=False)
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a decimal number") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be finite")
    if not 1.0 <= parsed <= 120.0:
        raise ValueError(f"{name} must be between 1 and 120 seconds")
    return parsed


def load_voice_audio_settings(
    env: Mapping[str, str] | None = None,
) -> VoiceAudioSettings:
    """Load SkyAI DEV OpenAI audio settings without exposing secrets.

    Use a voice-specific secret (`VOICE_TOOLS_OPENAI_KEY`) rather than the
    generic `OPENAI_API_KEY`.  SkyAI reasoning remains on the Hermes/Codex
    OAuth lane; this key is only for STT/TTS audio calls.
    """

    if env is None:
        env = os.environ
    return VoiceAudioSettings(
        provider_lane=VOICE_AUDIO_PROVIDER_LANE,
        api_key_env=OPENAI_AUDIO_KEY_ENV,
        api_key_configured=bool(_env_value(env, OPENAI_AUDIO_KEY_ENV)),
        base_url=_env_value(
            env,
            "SKYAI_VOICE_OPENAI_BASE_URL",
            OPENAI_AUDIO_BASE_URL,
            allow_empty=False,
        ),
        stt_primary_model=_env_value(
            env,
            "SKYAI_VOICE_STT_MODEL",
            OPENAI_STT_PRIMARY_MODEL,
            allow_empty=False,
        ),
        stt_fast_model=_env_value(
            env,
            "SKYAI_VOICE_STT_FAST_MODEL",
            OPENAI_STT_FAST_MODEL,
            allow_empty=False,
        ),
        tts_model=_env_value(
            env,
            "SKYAI_VOICE_TTS_MODEL",
            OPENAI_TTS_MODEL,
            allow_empty=False,
        ),
        tts_voice=_env_value(
            env,
            "SKYAI_VOICE_TTS_VOICE",
            OPENAI_TTS_VOICE,
            allow_empty=False,
        ),
        tts_fallback_voice=_env_value(
            env,
            "SKYAI_VOICE_TTS_FALLBACK_VOICE",
            OPENAI_TTS_FALLBACK_VOICE,
            allow_empty=False,
        ),
        realtime_api_url=_env_value(
            env,
            "SKYAI_VOICE_REALTIME_API_URL",
            OPENAI_REALTIME_API_URL,
            allow_empty=False,
        ),
        realtime_model=_env_value(
            env,
            "SKYAI_VOICE_REALTIME_MODEL",
            OPENAI_REALTIME_MODEL,
            allow_empty=False,
        ),
        realtime_fallback_model=_env_value(
            env,
            "SKYAI_VOICE_REALTIME_FALLBACK_MODEL",
            OPENAI_REALTIME_FALLBACK_MODEL,
            allow_empty=False,
        ),
        realtime_transcription_model=_env_value(
            env,
            "SKYAI_VOICE_REALTIME_TRANSCRIPTION_MODEL",
            OPENAI_REALTIME_TRANSCRIPTION_MODEL,
            allow_empty=False,
        ),
        realtime_voice=_env_value(
            env,
            "SKYAI_VOICE_REALTIME_VOICE",
            OPENAI_REALTIME_VOICE,
            allow_empty=False,
        ),
        realtime_fallback_voice=_env_value(
            env,
            "SKYAI_VOICE_REALTIME_FALLBACK_VOICE",
            OPENAI_REALTIME_FALLBACK_VOICE,
            allow_empty=False,
        ),
        realtime_input_audio_format=_env_value(
            env,
            "SKYAI_VOICE_REALTIME_INPUT_AUDIO_FORMAT",
            OPENAI_REALTIME_AUDIO_FORMAT,
            allow_empty=False,
        ),
        realtime_output_audio_format=_env_value(
            env,
            "SKYAI_VOICE_REALTIME_OUTPUT_AUDIO_FORMAT",
            OPENAI_REALTIME_AUDIO_FORMAT,
            allow_empty=False,
        ),
        realtime_turn_detection=_env_value(
            env,
            "SKYAI_VOICE_REALTIME_TURN_DETECTION",
            OPENAI_REALTIME_TURN_DETECTION,
            allow_empty=False,
        ),
        realtime_reasoning_effort=_env_value(
            env,
            "SKYAI_VOICE_REALTIME_REASONING_EFFORT",
            OPENAI_REALTIME_REASONING_EFFORT,
            allow_empty=False,
        ),
        realtime_backend_target=_env_value(
            env,
            "SKYAI_VOICE_REALTIME_BACKEND_TARGET",
            "skyai_v2_chatkit",
            allow_empty=False,
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
            "realtime_api_url": settings.realtime_api_url,
            "realtime_model": settings.realtime_model,
            "realtime_fallback_model": settings.realtime_fallback_model,
            "realtime_transcription_model": settings.realtime_transcription_model,
            "realtime_voice": settings.realtime_voice,
            "realtime_fallback_voice": settings.realtime_fallback_voice,
            "realtime_input_audio_format": settings.realtime_input_audio_format,
            "realtime_output_audio_format": settings.realtime_output_audio_format,
            "realtime_turn_detection": settings.realtime_turn_detection,
            "realtime_reasoning_effort": settings.realtime_reasoning_effort,
            "timeout_seconds": settings.request_timeout_seconds,
        },
        "voice_gateway_contract": {
            "audio_layer_only": True,
            "reasoning_path_unchanged": True,
            "raw_audio_stored_by_default": False,
            "customer_mutations_allowed": False,
        },
        "realtime_layer": _realtime_layer_summary(settings),
        "latency_masking": {
            "gateway_owned": True,
            "turn_based_only": True,
            "deprecated_for_realtime": True,
            "first_filler_after_ms": 900,
            "slow_turn_filler_after_ms": 5500,
            "examples_bg": [
                "Проверявам.",
                "Гледам свободните варианти.",
                "Още секунда, почти съм готов.",
            ],
        },
    }


def _realtime_layer_summary(settings: VoiceAudioSettings) -> dict[str, object]:
    return {
        "provider_lane": VOICE_REALTIME_PROVIDER_LANE,
        "audio_loop_owner": "openai_realtime_session",
        "skyai_brain": "skyai_v2_hermes_tools",
        "backend_target": settings.realtime_backend_target,
        "gateway_repeated_filler_phrases_allowed": False,
        "preamble_owner": "realtime_model",
        "preamble_policy": "brief_contextual_model_owned",
        "barge_in_required": True,
        "keyword_guards_allowed": False,
        "customer_mutations_allowed": False,
        "raw_audio_stored_by_default": False,
        "model": settings.realtime_model,
        "fallback_model": settings.realtime_fallback_model,
        "voice": settings.realtime_voice,
        "fallback_voice": settings.realtime_fallback_voice,
    }


def voice_realtime_preflight(settings: VoiceAudioSettings | None = None) -> dict[str, object]:
    settings = settings or load_voice_audio_settings()
    return {
        "status": "pass" if settings.api_key_configured else "blocked",
        "provider_lane": VOICE_REALTIME_PROVIDER_LANE,
        "api_key": {
            "configured": settings.api_key_configured,
            "env": settings.api_key_env,
            "value_printed": False,
        },
        "openai_realtime": {
            "api_url": settings.realtime_api_url,
            "model": settings.realtime_model,
            "fallback_model": settings.realtime_fallback_model,
            "voice": settings.realtime_voice,
            "fallback_voice": settings.realtime_fallback_voice,
            "input_audio_format": settings.realtime_input_audio_format,
            "output_audio_format": settings.realtime_output_audio_format,
            "turn_detection": settings.realtime_turn_detection,
            "reasoning_effort": settings.realtime_reasoning_effort,
            "timeout_seconds": settings.request_timeout_seconds,
        },
        "skyai_brain": {
            "runtime": "skyai_v2_hermes",
            "backend_target": settings.realtime_backend_target,
            "toolset": "skyai_customer",
            "voice_http_contract": "/voice/*",
            "keyword_guards_allowed": False,
            "customer_mutations_allowed": False,
        },
        "conversation_behavior": {
            "live_speech_to_speech": True,
            "barge_in_required": True,
            "gateway_repeated_filler_phrases_allowed": False,
            "preamble_owner": "realtime_model",
            "preamble_policy": "brief_contextual_model_owned",
            "voice_customer_already_on_phone_line": True,
        },
        "latency_targets_ms": {
            "first_audio_p50": 900,
            "first_audio_p95": 1800,
        },
        "privacy": {
            "raw_audio_stored_by_default": False,
            "transcripts_sanitized_before_discord": True,
            "secrets_printed": False,
        },
    }
