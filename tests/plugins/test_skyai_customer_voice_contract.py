from __future__ import annotations

from pathlib import Path

from plugins.skyai_customer import voice_audio, voice_contract
from scripts import skyai_voice_contract_smoke
from scripts import skyai_voice_openai_audio_preflight
from scripts import skyai_voice_openai_audio_smoke


VOICE_DOC_PATH = Path("docs/skyai-voice-contract-v0.1.md")
JOINT_CONTRACT_REFERENCE_PATH = Path("docs/voice/skyai-voice-joint-contract-v0.1.md")
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
    assert (
        lanes["hybrid_openai_api_audio_codex_oauth_reasoning"]["model_auth"]
        == "chatgpt_oauth_pro_via_codex"
    )
    assert lanes["hybrid_openai_api_audio_codex_oauth_reasoning"]["audio_auth"] == "openai_api_key"
    assert (
        lanes["hybrid_openai_api_audio_codex_oauth_reasoning"]["key_env"]
        == "VOICE_TOOLS_OPENAI_KEY"
    )
    assert lanes["openai_realtime_api"]["model"] == "gpt-realtime-2"
    assert lanes["openai_realtime_api"]["transcription_model"] == "gpt-realtime-whisper"
    assert lanes["openai_realtime_api"]["auth"] == "openai_api_key_or_short_lived_access_token"


def test_voice_audio_preflight_uses_dedicated_audio_key_without_printing_secret() -> None:
    settings = voice_audio.load_voice_audio_settings(
        {
            "VOICE_TOOLS_OPENAI_KEY": "sk-secret-value",
            "OPENAI_API_KEY": "sk-generic-should-not-be-used",
        }
    )

    result = voice_audio.voice_audio_preflight(settings)

    assert result["status"] == "pass"
    assert result["reasoning_auth"] == "hermes_codex_oauth_pro"
    assert result["audio_auth"] == "openai_api_key"
    assert result["api_key"] == {
        "configured": True,
        "env": "VOICE_TOOLS_OPENAI_KEY",
        "value_printed": False,
    }
    serialized = str(result)
    assert "sk-secret-value" not in serialized
    assert "sk-generic-should-not-be-used" not in serialized


def test_voice_audio_preflight_blocks_without_audio_key() -> None:
    settings = voice_audio.load_voice_audio_settings({"OPENAI_API_KEY": "sk-generic-only"})

    result = voice_audio.voice_audio_preflight(settings)

    assert result["status"] == "blocked"
    assert result["api_key"]["configured"] is False
    assert result["api_key"]["env"] == "VOICE_TOOLS_OPENAI_KEY"


def test_voice_audio_preflight_declares_hybrid_models_and_gateway_latency_masking() -> None:
    result = voice_audio.voice_audio_preflight(
        voice_audio.load_voice_audio_settings(
            {
                "VOICE_TOOLS_OPENAI_KEY": "sk-test",
                "SKYAI_VOICE_TTS_VOICE": "cedar",
            }
        )
    )

    assert result["provider_lane"] == "hybrid_openai_api_audio_codex_oauth_reasoning"
    assert result["openai"]["stt_primary_model"] == "gpt-4o-transcribe"
    assert result["openai"]["stt_fast_model"] == "gpt-4o-mini-transcribe"
    assert result["openai"]["tts_model"] == "gpt-4o-mini-tts"
    assert result["openai"]["tts_voice"] == "cedar"
    assert result["openai"]["realtime_transcription_model"] == "gpt-realtime-whisper"
    assert result["voice_gateway_contract"]["reasoning_path_unchanged"] is True
    assert result["latency_masking"]["gateway_owned"] is True
    assert result["latency_masking"]["first_filler_after_ms"] == 900


def test_voice_privacy_defaults_are_safe_by_default() -> None:
    defaults = voice_contract.VOICE_PRIVACY_DEFAULTS

    assert defaults["store_raw_audio_by_default"] is False
    assert defaults["redact_secrets_before_logs"] is True
    assert defaults["require_recording_notice_before_recording"] is True
    assert defaults["allow_customer_mutations_without_verified_auth"] is False


def test_voice_contract_doc_records_current_architecture_and_mvp_scope() -> None:
    text = VOICE_DOC_PATH.read_text(encoding="utf-8")

    required_phrases = [
        "DEV HTTP adapter skeleton",
        "does not deploy",
        "registered on the DEV/canary HTTP gateway",
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
    assert "Hybrid OpenAI API audio + Hermes/OAuth reasoning" in text
    assert "VOICE_TOOLS_OPENAI_KEY" in text


def test_voice_gate_registers_http_adapter_only_no_pbx_audio_stack() -> None:
    source = DEV_GATEWAY_PATH.read_text(encoding="utf-8")

    assert 'add_post("/voice/start"' in source
    assert 'add_post("/voice/turn"' in source
    assert 'add_post("/voice/event"' in source
    assert 'add_post("/voice/end"' in source
    assert "SIP" not in source
    assert "PBX" not in source
    assert "RTP" not in source
    assert "pjsip" not in source.casefold()
    assert "asterisk" not in source.casefold()


def test_voice_contract_is_referenced_from_plugin_docs() -> None:
    readme = README_PATH.read_text(encoding="utf-8")
    architecture = ARCHITECTURE_PATH.read_text(encoding="utf-8")

    assert "docs/skyai-voice-contract-v0.1.md" in readme
    assert "SkyAI Voice Contract v0.1" in architecture


def test_joint_voice_contract_reference_points_to_canonical_shared_doc() -> None:
    text = JOINT_CONTRACT_REFERENCE_PATH.read_text(encoding="utf-8")

    assert "/Users/emillomliev/.hermes/knowledge/skyai-voice/skyai-voice-joint-contract-v0.1.md" in text
    assert "Do not fork endpoint names, action values, or field semantics" in text


def test_voice_contract_smoke_builds_dev_safe_endpoint_sequence() -> None:
    requests = skyai_voice_contract_smoke.build_smoke_requests(
        call_id="call-test",
        conversation_id="voice-test",
        backend_target="skyai_v2_chatkit",
    )

    assert [item.path for item in requests] == [
        "/voice/start",
        "/voice/turn",
        "/voice/turn",
        "/voice/event",
        "/voice/end",
    ]
    assert [item.expected_action for item in requests] == [
        "speak",
        "speak",
        "clarify",
        "transfer_to_human",
        "end_call",
    ]
    serialized = "\n".join(
        str(value)
        for request in requests
        for value in request.payload.values()
    )
    assert "password" not in serialized.casefold()
    assert "token" not in serialized.casefold()
    assert "raw_audio" not in serialized.casefold()


def test_voice_contract_smoke_validates_canonical_response_shape() -> None:
    request = skyai_voice_contract_smoke.SmokeRequest(
        path="/voice/event",
        payload={"call_id": "call-test", "conversation_id": "voice-test"},
        expected_action="transfer_to_human",
    )
    response = {
        "status": "ok",
        "version": "skyai-v",
        "contract_version": "skyai-voice-contract.v0.1",
        "call_id": "call-test",
        "conversation_id": "voice-test",
        "action": "transfer_to_human",
        "spoken_reply": "Ще Ви прехвърля.",
        "display_reply": "Transfer.",
        "cards": [],
        "transfer": {"target": "operator_queue", "reason": "dtmf_0"},
        "transfer_reason": "dtmf_0",
        "target": "operator_queue",
        "end_call": False,
        "session_state": {"handoff_allowed": True},
        "trace": {"raw_audio_stored": False},
        "notes": [],
        "unavailable": False,
    }

    assert skyai_voice_contract_smoke.validate_response(request, response) == []


def test_openai_audio_preflight_cli_never_prints_secret(monkeypatch, capsys) -> None:
    monkeypatch.setenv("VOICE_TOOLS_OPENAI_KEY", "sk-super-secret")

    exit_code = skyai_voice_openai_audio_preflight.main([])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "SkyAI OpenAI audio preflight: PASS" in output
    assert "api_key_configured=true" in output
    assert "VOICE_TOOLS_OPENAI_KEY" in output
    assert "sk-super-secret" not in output


def test_openai_audio_live_smoke_blocks_without_key(monkeypatch, capsys) -> None:
    monkeypatch.delenv("VOICE_TOOLS_OPENAI_KEY", raising=False)

    exit_code = skyai_voice_openai_audio_smoke.main(["--live-openai"])

    output = capsys.readouterr().out
    assert exit_code == 2
    assert "SkyAI OpenAI audio live smoke: BLOCKED" in output
    assert "missing_voice_tools_openai_key" in output


def test_openai_audio_live_smoke_requires_explicit_live_flag(monkeypatch, capsys) -> None:
    monkeypatch.setenv("VOICE_TOOLS_OPENAI_KEY", "sk-super-secret")

    exit_code = skyai_voice_openai_audio_smoke.main([])

    output = capsys.readouterr().out
    assert exit_code == 2
    assert "live_openai_flag_required" in output
    assert "sk-super-secret" not in output


def test_openai_audio_live_smoke_calls_tts_then_stt_without_printing_secret(monkeypatch, capsys) -> None:
    calls = []

    class FakeResponse:
        def __init__(self, *, content: bytes = b"", payload: dict | None = None) -> None:
            self.content = content
            self._payload = payload or {}

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/audio/speech"):
            return FakeResponse(content=b"fake-mp3-audio")
        if url.endswith("/audio/transcriptions"):
            return FakeResponse(payload={"text": "Здравейте, това е кратък тест на гласа на SkyAI."})
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setenv("VOICE_TOOLS_OPENAI_KEY", "sk-super-secret")
    monkeypatch.setattr(skyai_voice_openai_audio_smoke.requests, "post", fake_post)

    exit_code = skyai_voice_openai_audio_smoke.main(["--live-openai"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "SkyAI OpenAI audio live smoke: PASS" in output
    assert "fake-mp3-audio" not in output
    assert "sk-super-secret" not in output
    assert calls[0][0].endswith("/audio/speech")
    assert calls[0][1]["json"]["model"] == "gpt-4o-mini-tts"
    assert calls[0][1]["json"]["voice"] == "marin"
    assert calls[1][0].endswith("/audio/transcriptions")
    assert calls[1][1]["data"]["model"] == "gpt-4o-transcribe"
    assert calls[1][1]["data"]["language"] == "bg"
