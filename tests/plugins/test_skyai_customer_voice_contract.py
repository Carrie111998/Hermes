from __future__ import annotations

from pathlib import Path

from plugins.skyai_customer import voice_contract


VOICE_DOC_PATH = Path("docs/skyai-voice-contract-v0.1.md")
README_PATH = Path("plugins/skyai_customer/README.md")
ARCHITECTURE_PATH = Path("plugins/skyai_customer/ARCHITECTURE.md")
DEV_GATEWAY_PATH = Path("plugins/skyai_customer/dev_gateway.py")


def test_voice_contract_declares_adapter_paths_and_actions() -> None:
    assert voice_contract.VOICE_CONTRACT_VERSION == "skyai-voice-contract.v0.1"
    assert voice_contract.VOICE_ADAPTER_PATHS == {
        "start_call": "/voice/start",
        "send_user_transcript": "/voice/turn",
        "send_call_event": "/voice/event",
        "end_call": "/voice/end",
    }
    assert set(voice_contract.VOICE_ACTIONS) == {
        "speak",
        "clarify",
        "transfer_to_human",
        "end_call",
    }


def test_voice_contract_covers_pbx_metadata_and_codecs() -> None:
    assert set(voice_contract.VOICE_REQUIRED_CALL_METADATA) >= {
        "call_id",
        "conversation_id",
        "caller_id",
        "did",
        "pbx_extension",
        "department",
        "language",
        "source",
    }
    assert voice_contract.VOICE_SUPPORTED_PBX_PROFILE["pbx"] == "ZYCOO CooVox-U20"
    assert voice_contract.VOICE_SUPPORTED_PBX_PROFILE["asterisk"] == "1.8.7.1"
    assert voice_contract.VOICE_SUPPORTED_PBX_PROFILE["preferred_codec"] == "alaw"
    assert voice_contract.VOICE_SUPPORTED_PBX_PROFILE["fallback_codec"] == "ulaw"
    assert voice_contract.VOICE_SUPPORTED_PBX_PROFILE["dtmf"] == "rfc2833"


def test_voice_contract_keeps_v1_and_v2_backend_targets_swappable() -> None:
    targets = voice_contract.VOICE_BACKEND_TARGETS

    assert targets["skyai_v1_chatkit"]["path"] == "/chatkit/message"
    assert targets["skyai_v2_chatkit"]["path"] == "/chatkit/message"
    assert targets["skyai_v1_chatkit"]["streaming"] == "final_response_only"
    assert targets["skyai_v2_chatkit"]["streaming"] == "final_response_only"


def test_voice_contract_documents_oauth_and_openai_api_boundary() -> None:
    lanes = voice_contract.VOICE_PROVIDER_LANES

    assert lanes["mvp_codex_oauth_text"]["model_auth"] == "chatgpt_oauth_pro_via_codex"
    assert lanes["mvp_codex_oauth_text"]["audio_auth"] == "external_or_local_stt_tts_provider"
    assert "not assumed" in lanes["mvp_codex_oauth_text"]["note"]
    assert lanes["openai_realtime_api"]["model"] == "gpt-realtime-2"
    assert lanes["openai_realtime_api"]["transcription_model"] == "gpt-realtime-whisper"
    assert lanes["openai_realtime_api"]["auth"] == "openai_api_key_or_short_lived_access_token"


def test_voice_privacy_defaults_are_safe_by_default() -> None:
    defaults = voice_contract.VOICE_PRIVACY_DEFAULTS

    assert defaults["store_raw_audio_by_default"] is False
    assert defaults["redact_secrets_before_logs"] is True
    assert defaults["require_recording_notice_before_recording"] is True
    assert defaults["allow_customer_mutations_without_verified_auth"] is False


def test_voice_contract_doc_records_current_architecture_and_mvp_scope() -> None:
    text = VOICE_DOC_PATH.read_text(encoding="utf-8")

    required_phrases = [
        "design and tests only",
        "does not deploy",
        "does not register them",
        "POST /chatkit/message",
        "POST /chatkit/dev-message",
        "POST /qa/compare",
        "final response only",
        "ZYCOO CooVox-U20",
        "Asterisk 1.8.7.1",
        "alaw",
        "ulaw",
        "rfc2833",
        "SIP extension",
        "DTMF `0`",
        "transfer_to_human",
        "do not store raw audio by default",
    ]
    for phrase in required_phrases:
        assert phrase in text


def test_voice_contract_doc_documents_low_latency_and_oauth_tradeoffs() -> None:
    text = VOICE_DOC_PATH.read_text(encoding="utf-8")
    compact_text = " ".join(text.split())

    assert "lowest latency" in text
    assert "gpt-realtime-2" in text
    assert "gpt-realtime-whisper" in text
    assert "ChatGPT Pro OAuth" in text
    assert "ChatGPT Pro OAuth is not a supported audio API auth path" in compact_text
    assert "OpenAI API billing" in text
    assert "MVP without OpenAI API billing" in text


def test_voice_gate_is_docs_only_no_runtime_routes_registered() -> None:
    source = DEV_GATEWAY_PATH.read_text(encoding="utf-8")

    assert 'add_post("/voice/start"' not in source
    assert 'add_post("/voice/turn"' not in source
    assert 'add_post("/voice/event"' not in source
    assert 'add_post("/voice/end"' not in source
    assert "SIP" not in source
    assert "PBX" not in source


def test_voice_contract_is_referenced_from_plugin_docs() -> None:
    readme = README_PATH.read_text(encoding="utf-8")
    architecture = ARCHITECTURE_PATH.read_text(encoding="utf-8")

    assert "docs/skyai-voice-contract-v0.1.md" in readme
    assert "SkyAI Voice Contract v0.1" in architecture
