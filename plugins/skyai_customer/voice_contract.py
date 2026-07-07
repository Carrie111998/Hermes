"""Static SkyAI voice adapter contract.

This module intentionally contains no runtime routes, SIP client, STT, TTS, or
PBX integration.  It gives the future SkyAI Voice Gateway and the SkyAI chat
adapter a shared vocabulary that can be tested without touching production.
"""

from __future__ import annotations

VOICE_CONTRACT_VERSION = "skyai-voice-contract.v0.1"

VOICE_ADAPTER_PATHS: dict[str, str] = {
    "start_call": "/voice/start",
    "send_user_transcript": "/voice/turn",
    "send_call_event": "/voice/event",
    "end_call": "/voice/end",
}

VOICE_ACTIONS: tuple[str, ...] = (
    "speak",
    "clarify",
    "transfer_to_human",
    "end_call",
)

VOICE_REQUIRED_CALL_METADATA: tuple[str, ...] = (
    "call_id",
    "conversation_id",
    "caller_id",
    "did",
    "pbx_extension",
    "department",
    "language",
    "source",
)

VOICE_SUPPORTED_PBX_PROFILE: dict[str, str] = {
    "pbx": "ZYCOO CooVox-U20",
    "asterisk": "1.8.7.1",
    "sip_transport": "UDP/5060",
    "dtmf": "rfc2833",
    "preferred_codec": "alaw",
    "fallback_codec": "ulaw",
}

VOICE_BACKEND_TARGETS: dict[str, dict[str, str]] = {
    "skyai_v1_chatkit": {
        "role": "current_prod_compatible_adapter",
        "path": "/chatkit/message",
        "streaming": "final_response_only",
    },
    "skyai_v2_chatkit": {
        "role": "hermes_v2_canary_adapter",
        "path": "/chatkit/message",
        "streaming": "final_response_only",
    },
}

VOICE_AUTH_MODES: tuple[str, ...] = (
    "private_network_bearer",
    "gateway_bearer",
    "future_mtls",
)

VOICE_PROVIDER_LANES: dict[str, dict[str, str]] = {
    "mvp_codex_oauth_text": {
        "purpose": "reuse_existing_skyai_text_lane",
        "model_auth": "chatgpt_oauth_pro_via_codex",
        "audio_auth": "external_or_local_stt_tts_provider",
        "note": "OpenAI public audio APIs are not assumed to accept ChatGPT Pro OAuth.",
    },
    "hybrid_openai_api_audio_codex_oauth_reasoning": {
        "purpose": "openai_api_stt_tts_with_existing_skyai_reasoning",
        "model_auth": "chatgpt_oauth_pro_via_codex",
        "audio_auth": "openai_api_key",
        "stt_primary_model": "gpt-4o-transcribe",
        "stt_fast_model": "gpt-4o-mini-transcribe",
        "tts_model": "gpt-4o-mini-tts",
        "key_env": "VOICE_TOOLS_OPENAI_KEY",
    },
    "openai_realtime_api": {
        "purpose": "lowest_latency_speech_to_speech_candidate",
        "model": "gpt-realtime-2",
        "transcription_model": "gpt-realtime-whisper",
        "auth": "openai_api_key_or_short_lived_access_token",
    },
}

VOICE_LATENCY_TARGETS_MS: dict[str, dict[str, int]] = {
    "turn_based_mvp": {
        "first_audio_p50": 2500,
        "first_audio_p95": 6000,
    },
    "realtime_target": {
        "first_audio_p50": 900,
        "first_audio_p95": 1800,
    },
}

VOICE_PRIVACY_DEFAULTS: dict[str, bool] = {
    "store_raw_audio_by_default": False,
    "store_transcript_by_default": True,
    "redact_secrets_before_logs": True,
    "require_recording_notice_before_recording": True,
    "allow_customer_mutations_without_verified_auth": False,
}
