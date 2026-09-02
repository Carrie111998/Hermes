"""Regression tests for session-aware native audio routing in the gateway."""

import importlib
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource, build_session_key


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="1",
        chat_type="dm",
    )


def _bare_runner():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True)
    runner.adapters = {}
    runner._model = "google/gemini-test"
    runner._base_url = None
    runner._has_setup_skill = lambda: False
    return runner


def test_audio_routing_uses_native_only_for_audio_capable_model(monkeypatch):
    runner = _bare_runner()
    media_routing = importlib.import_module("agent.media_routing")

    monkeypatch.setattr(
        media_routing,
        "supported_input_modalities",
        lambda *_: {"text", "audio"},
    )
    assert (
        runner._decide_audio_input_mode(
            provider="openrouter",
            model="google/gemini-test",
            user_config={},
        )
        == "native"
    )

    monkeypatch.setattr(
        media_routing,
        "supported_input_modalities",
        lambda *_: {"text", "image"},
    )
    assert (
        runner._decide_audio_input_mode(
            provider="openrouter",
            model="text-only-test",
            user_config={},
        )
        == "stt"
    )

    # User explicit config overrides
    assert (
        runner._decide_audio_input_mode(
            provider="openrouter",
            model="text-only-test",
            user_config={"gateway": {"audio_mode": "native"}},
        )
        == "native"
    )

    assert (
        runner._decide_audio_input_mode(
            provider="openrouter",
            model="google/gemini-test",
            user_config={"gateway": {"audio_mode": "stt"}},
        )
        == "stt"
    )

    # The DEFAULT_CONFIG-merged gateway.audio_mode="auto" must not mask the
    # compatibility top-level form when both keys are present.
    assert (
        runner._decide_audio_input_mode(
            provider="openrouter",
            model="google/gemini-test",
            user_config={
                "audio_mode": "stt",
                "gateway": {"audio_mode": "auto"},
            },
        )
        == "stt"
    )


@pytest.mark.asyncio
async def test_voice_message_stages_native_audio_without_stt(tmp_path):
    runner = _bare_runner()
    runner._decide_audio_input_mode = lambda **_: "native"
    source = _source()
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"OggS-test-audio")
    event = MessageEvent(
        text="",
        message_type=MessageType.VOICE,
        source=source,
        media_urls=[str(audio_path)],
        media_types=["audio/ogg"],
    )

    with patch(
        "tools.transcription_tools.transcribe_audio",
        side_effect=AssertionError("native audio must not invoke STT"),
    ):
        result = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

    assert result == ""
    attachments = runner._consume_pending_native_audio_attachments(
        build_session_key(source)
    )
    assert attachments == [
        {
            "path": str(audio_path),
            "mime_type": "audio/ogg",
            "modality": "audio",
        }
    ]
    assert (
        runner._consume_pending_native_audio_attachments(build_session_key(source))
        == []
    )


class _CaptureAgent:
    calls = []

    def __init__(self, **kwargs):
        self.tools = []
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.provider = kwargs.get("provider", "")

    def run_conversation(self, message, **kwargs):
        type(self).calls.append((message, kwargs))
        return {"final_response": "done", "messages": [], "api_calls": 1}


def _pipeline_runner():
    gateway_run = importlib.import_module("gateway.run")
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._session_db = None
    runner._running_agents = {}
    runner._session_run_generation = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(
        thread_sessions_per_user=False,
        group_sessions_per_user=False,
        stt_enabled=True,
    )
    runner._model = "google/gemini-test"
    runner._base_url = None
    return runner


@pytest.mark.asyncio
async def test_agent_pipeline_sends_input_audio_and_persists_compact_marker(
    monkeypatch,
    tmp_path,
):
    _CaptureAgent.calls = []

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *_, **__: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CaptureAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {"api_key": "***", "provider": "openrouter"},
    )

    runner = _pipeline_runner()
    source = _source()
    session_key = build_session_key(source)
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"OggS-test-audio")
    runner._session_state(session_key).persistent.native_audio_attachments = [
        {
            "path": str(audio_path),
            "mime_type": "audio/ogg",
            "modality": "audio",
        }
    ]

    result = await runner._run_agent(
        message="",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-native-audio",
        session_key=session_key,
    )

    assert result["final_response"] == "done"
    assert len(_CaptureAgent.calls) == 1
    message, kwargs = _CaptureAgent.calls[0]
    assert isinstance(message, list)
    assert any(part.get("type") == "input_audio" for part in message)
    audio_part = next(part for part in message if part.get("type") == "input_audio")
    assert audio_part["input_audio"]["format"] == "ogg"
    assert kwargs["persist_user_message"] == "[Voice message attached natively]"
    assert runner._consume_pending_native_audio_attachments(session_key) == []


@pytest.mark.asyncio
async def test_agent_pipeline_falls_back_to_stt_when_native_audio_is_rejected(
    monkeypatch,
    tmp_path,
):
    _CaptureAgent.calls = []

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *_, **__: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CaptureAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    gateway_run = importlib.import_module("gateway.run")
    media_routing = importlib.import_module("agent.media_routing")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {"api_key": "***", "provider": "openai"},
    )
    monkeypatch.setattr(
        media_routing,
        "transcode_audio_to_supported_format",
        lambda *_args, **_kwargs: None,
    )

    runner = _pipeline_runner()
    runner._enrich_message_with_transcription = AsyncMock(
        return_value=('"fallback transcript"', ["fallback transcript"])
    )
    source = _source()
    session_key = build_session_key(source)
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"OggS-test-audio")
    runner._session_state(session_key).persistent.native_audio_attachments = [
        {
            "path": str(audio_path),
            "mime_type": "audio/ogg",
            "modality": "audio",
        }
    ]

    result = await runner._run_agent(
        message="",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-native-stt-fallback",
        session_key=session_key,
    )

    assert result["final_response"] == "done"
    message, kwargs = _CaptureAgent.calls[0]
    assert message == '"fallback transcript"'
    assert kwargs.get("persist_user_message") is None
    runner._enrich_message_with_transcription.assert_awaited_once_with(
        "",
        [str(audio_path)],
    )


@pytest.mark.asyncio
async def test_partial_native_audio_failure_transcribes_only_rejected_clip(
    monkeypatch,
    tmp_path,
):
    _CaptureAgent.calls = []

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *_, **__: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CaptureAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    gateway_run = importlib.import_module("gateway.run")
    media_routing = importlib.import_module("agent.media_routing")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {"api_key": "***", "provider": "openai"},
    )
    monkeypatch.setattr(
        media_routing,
        "transcode_audio_to_supported_format",
        lambda *_args, **_kwargs: None,
    )

    runner = _pipeline_runner()
    runner._enrich_message_with_transcription = AsyncMock(
        return_value=('"second clip transcript"', ["second clip transcript"])
    )
    source = _source()
    session_key = build_session_key(source)
    native_path = tmp_path / "first.mp3"
    native_path.write_bytes(b"ID3-native-audio")
    rejected_path = tmp_path / "second.ogg"
    rejected_path.write_bytes(b"OggS-rejected-audio")
    runner._session_state(session_key).persistent.native_audio_attachments = [
        {
            "path": str(native_path),
            "mime_type": "audio/mpeg",
            "modality": "audio",
        },
        {
            "path": str(rejected_path),
            "mime_type": "audio/ogg",
            "modality": "audio",
        },
    ]

    result = await runner._run_agent(
        message="",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-partial-native-stt-fallback",
        session_key=session_key,
    )

    assert result["final_response"] == "done"
    message, kwargs = _CaptureAgent.calls[0]
    assert isinstance(message, list)
    assert sum(part.get("type") == "input_audio" for part in message) == 1
    text_part = next(part for part in message if part.get("type") == "text")
    assert '"second clip transcript"' in text_part["text"]
    assert kwargs["persist_user_message"] == (
        '"second clip transcript"\n\n[Voice message attached natively]'
    )
    runner._enrich_message_with_transcription.assert_awaited_once_with(
        "",
        [str(rejected_path)],
    )


@pytest.mark.asyncio
async def test_native_audio_stt_fallback_echoes_each_transcript_once():
    runner = _bare_runner()
    adapter = SimpleNamespace(send=AsyncMock())
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._thread_metadata_for_source = lambda *_args: {"thread_id": "topic-1"}
    runner._enrich_message_with_transcription = AsyncMock(
        return_value=('"one"\n\n"two"', ["one", "two"])
    )

    fallback_text = await runner._transcribe_native_audio_fallback(
        ["one.ogg", "two.ogg"],
        source=_source(),
        reply_to_message_id="reply-1",
    )

    assert fallback_text == '"one"\n\n"two"'
    assert adapter.send.await_count == 2
    assert [call.args[1] for call in adapter.send.await_args_list] == [
        '🎙️ "one"',
        '🎙️ "two"',
    ]
    assert all(
        call.kwargs["metadata"] == {"thread_id": "topic-1"}
        for call in adapter.send.await_args_list
    )


@pytest.mark.asyncio
async def test_native_audio_fallback_remains_nonempty_when_stt_is_disabled(
    monkeypatch,
    tmp_path,
):
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(
        gateway_run,
        "_probe_audio_duration",
        AsyncMock(return_value=None),
    )
    runner = _bare_runner()
    runner.config.stt_enabled = False
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"OggS-test-audio")

    fallback_text = await runner._transcribe_native_audio_fallback(
        [str(audio_path)],
        source=_source(),
    )

    assert fallback_text.strip()
    assert "The user sent a voice message" in fallback_text
    assert str(audio_path.resolve()) in fallback_text


def test_audio_routing_forces_stt_for_meta_and_muse_spark(monkeypatch):
    runner = _bare_runner()
    media_routing = importlib.import_module("agent.media_routing")

    monkeypatch.setattr(
        media_routing,
        "supported_input_modalities",
        lambda *_: {"text", "audio"},
    )

    # Meta providers forced to STT
    assert (
        runner._decide_audio_input_mode(
            provider="meta",
            model="llama-3.2-audio",
            user_config={},
        )
        == "stt"
    )
    assert (
        runner._decide_audio_input_mode(
            provider="meta-ai",
            model="llama-3.2-audio",
            user_config={},
        )
        == "stt"
    )

    # Muse spark model forced to STT
    assert (
        runner._decide_audio_input_mode(
            provider="openrouter",
            model="vendor/muse-spark-v1",
            user_config={},
        )
        == "stt"
    )


@pytest.mark.asyncio
async def test_agent_pipeline_sends_mixed_image_and_audio(
    monkeypatch,
    tmp_path,
):
    _CaptureAgent.calls = []

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *_, **__: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CaptureAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {"api_key": "***", "provider": "openrouter"},
    )

    runner = _pipeline_runner()
    source = _source()
    session_key = build_session_key(source)
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"OggS-test-audio")
    img_path = tmp_path / "photo.png"
    img_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

    runner._session_state(session_key).persistent.native_image_paths = [str(img_path)]
    runner._session_state(session_key).persistent.native_audio_attachments = [
        {
            "path": str(audio_path),
            "mime_type": "audio/ogg",
            "modality": "audio",
        }
    ]

    result = await runner._run_agent(
        message="look and listen",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-mixed-media",
        session_key=session_key,
    )

    assert result["final_response"] == "done"
    assert len(_CaptureAgent.calls) == 1
    message, kwargs = _CaptureAgent.calls[0]
    assert isinstance(message, list)
    types_in_msg = [part.get("type") for part in message]
    assert "text" in types_in_msg
    assert "image_url" in types_in_msg
    assert "input_audio" in types_in_msg
    assert runner._consume_pending_native_image_paths(session_key) == []
    assert runner._consume_pending_native_audio_attachments(session_key) == []


@pytest.mark.asyncio
async def test_agent_pipeline_falls_back_to_native_image_when_all_audio_is_skipped(
    monkeypatch,
    tmp_path,
):
    _CaptureAgent.calls = []

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *_, **__: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CaptureAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    gateway_run = importlib.import_module("gateway.run")
    media_routing = importlib.import_module("agent.media_routing")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {"api_key": "***", "provider": "openrouter"},
    )
    monkeypatch.setattr(
        media_routing,
        "build_native_media_content_parts",
        lambda text, *_args, **_kwargs: (
            [{"type": "text", "text": text}],
            ["voice.ogg"],
        ),
    )

    runner = _pipeline_runner()
    runner._enrich_message_with_transcription = AsyncMock(
        return_value=('"fallback transcript"', ["fallback transcript"])
    )
    source = _source()
    session_key = build_session_key(source)
    image_path = tmp_path / "photo.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    runner._session_state(session_key).persistent.native_image_paths = [str(image_path)]
    runner._session_state(session_key).persistent.native_audio_attachments = [
        {
            "path": str(tmp_path / "voice.ogg"),
            "mime_type": "audio/ogg",
            "modality": "audio",
        }
    ]

    result = await runner._run_agent(
        message="look and listen",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-image-fallback",
        session_key=session_key,
    )

    assert result["final_response"] == "done"
    message, kwargs = _CaptureAgent.calls[0]
    assert isinstance(message, list)
    assert any(part.get("type") == "image_url" for part in message)
    assert not any(part.get("type") == "input_audio" for part in message)
    text_part = next(part for part in message if part.get("type") == "text")
    assert '"fallback transcript"' in text_part["text"]
    assert kwargs.get("persist_user_message") is None
