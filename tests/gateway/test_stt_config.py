"""Gateway STT config tests — config bridging and fail-open voice handling."""

import asyncio
import threading
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from gateway.config import GatewayConfig, Platform, load_gateway_config
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource


async def _wait_for_thread_event(event: threading.Event) -> None:
    """Wait without consuming another executor thread."""
    for _ in range(200):
        if event.is_set():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("worker thread did not start")


def test_gateway_config_stt_disabled_from_dict_nested():
    config = GatewayConfig.from_dict({"stt": {"enabled": False}})
    assert config.stt_enabled is False


def test_gateway_config_stt_timeout_defaults_and_accepts_nested_override():
    assert GatewayConfig.from_dict({}).stt_timeout_seconds == 45.0
    config = GatewayConfig.from_dict({"stt": {"gateway_timeout_seconds": 12.5}})
    assert config.stt_timeout_seconds == 12.5


def test_gateway_config_stt_timeout_rejects_invalid_values():
    assert GatewayConfig.from_dict({"stt": {"gateway_timeout_seconds": 0}}).stt_timeout_seconds == 45.0
    assert GatewayConfig.from_dict({"stt": {"gateway_timeout_seconds": "bad"}}).stt_timeout_seconds == 45.0


def test_load_gateway_config_bridges_stt_enabled_from_config_yaml(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.dump({"stt": {"enabled": False, "gateway_timeout_seconds": 7.5}}),
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    config = load_gateway_config()

    assert config.stt_enabled is False
    assert config.stt_timeout_seconds == 7.5


@pytest.mark.asyncio
async def test_enrich_message_with_transcription_surfaces_path_when_stt_disabled():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=False)
    runner._has_setup_skill = lambda: True  # Should NOT be consulted in disabled branch.

    with patch(
        "tools.transcription_tools.transcribe_audio",
        side_effect=AssertionError("transcribe_audio should not be called when STT is disabled"),
    ), patch(
        "gateway.run._probe_audio_duration",
        new=AsyncMock(return_value="0:12"),
    ):
        result, transcripts = await runner._enrich_message_with_transcription(
            "caption",
            ["/tmp/voice.ogg"],
        )

    assert "voice.ogg" in result
    assert "voice message" in result.lower()
    assert "(duration: 0:12)" in result
    assert "caption" in result
    assert transcripts == []


@pytest.mark.asyncio
async def test_enrich_message_with_transcription_omits_duration_on_probe_failure():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=False)

    with patch(
        "gateway.run._probe_audio_duration",
        new=AsyncMock(return_value=None),
    ):
        result, transcripts = await runner._enrich_message_with_transcription(
            "",
            ["/tmp/voice.ogg"],
        )

    assert "voice.ogg" in result
    assert "duration" not in result.lower()
    assert transcripts == []


@pytest.mark.asyncio
async def test_enrich_message_with_transcription_avoids_bogus_no_provider_message_for_backend_key_errors():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True)

    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={"success": False, "error": "VOICE_TOOLS_OPENAI_KEY not set"},
    ), patch(
        "tools.transcription_tools.transcribe_audio_local_fallback",
        return_value={"success": False, "error": "not installed"},
    ):
        result, transcripts = await runner._enrich_message_with_transcription(
            "caption",
            ["/tmp/voice.ogg"],
        )

    assert "No STT provider is configured" not in result
    assert "voice message could not be transcribed automatically" in result
    assert "voice.ogg" in result
    # The opaque backend cause must NOT leak into the LLM-visible prompt.
    assert "VOICE_TOOLS_OPENAI_KEY" not in result
    assert "caption" in result
    assert transcripts == []


@pytest.mark.asyncio
async def test_enrich_message_with_transcription_falls_back_to_installed_local_stt():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True)

    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={"success": False, "error": "configured provider unavailable"},
    ), patch(
        "tools.transcription_tools.transcribe_audio_local_fallback",
        return_value={
            "success": True,
            "transcript": "recovered locally",
            "provider": "local",
        },
    ) as local_fallback:
        result, transcripts = await runner._enrich_message_with_transcription(
            "",
            ["/tmp/voice.ogg"],
        )

    assert result == '"recovered locally"'
    assert transcripts == ["recovered locally"]
    local_fallback.assert_called_once_with("/tmp/voice.ogg")


@pytest.mark.asyncio
async def test_enrich_message_with_transcription_times_out_fail_open():
    """A stuck STT worker must not hold the gateway/chat lock indefinitely."""
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True, stt_timeout_seconds=0.2)
    worker_started = threading.Event()
    release_worker = threading.Event()

    def stuck_transcription(_path):
        worker_started.set()
        release_worker.wait(timeout=5)
        return {"success": True, "transcript": "too late"}

    try:
        with patch(
            "tools.transcription_tools.transcribe_audio",
            side_effect=stuck_transcription,
        ):
            transcription_task = asyncio.create_task(
                runner._enrich_message_with_transcription(
                    "caption",
                    ["/tmp/stuck-voice.ogg"],
                )
            )
            await _wait_for_thread_event(worker_started)
            result, transcripts = await transcription_task
    finally:
        release_worker.set()

    assert "voice message could not be transcribed automatically" in result
    assert "stuck-voice.ogg" in result
    assert "caption" in result
    assert transcripts == []


@pytest.mark.asyncio
async def test_enrich_message_with_transcription_bounds_local_fallback_too():
    """The same gateway deadline must cover a stuck local fallback."""
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True, stt_timeout_seconds=0.2)
    fallback_started = threading.Event()
    release_worker = threading.Event()

    def stuck_local_fallback(_path):
        fallback_started.set()
        release_worker.wait(timeout=5)
        return {"success": True, "transcript": "too late"}

    try:
        with patch(
            "tools.transcription_tools.transcribe_audio",
            return_value={"success": False, "error": "configured provider unavailable"},
        ), patch(
            "tools.transcription_tools.transcribe_audio_local_fallback",
            side_effect=stuck_local_fallback,
        ) as local_fallback:
            transcription_task = asyncio.create_task(
                runner._enrich_message_with_transcription(
                    "",
                    ["/tmp/stuck-fallback.ogg"],
                )
            )
            await _wait_for_thread_event(fallback_started)
            result, transcripts = await transcription_task
            local_fallback.assert_called_once_with("/tmp/stuck-fallback.ogg")
    finally:
        release_worker.set()

    assert "voice message could not be transcribed automatically" in result
    assert "stuck-fallback.ogg" in result
    assert transcripts == []


@pytest.mark.asyncio
async def test_enrich_message_with_transcription_returns_tuple_for_empty_content_placeholder():
    """A successful transcription whose caption is the empty-content placeholder
    must still return the ``(text, transcripts)`` tuple.

    The Discord adapter delivers a captionless voice note as the literal
    ``"(The user sent a message with no text content)"`` placeholder. When STT
    succeeds we strip that redundant placeholder and return just the transcript
    prefix — but the method's contract (and every caller, which unpacks the
    result as ``text, transcripts = ...``) requires a 2-tuple. Returning a bare
    string here raised ``ValueError: too many values to unpack`` and dropped the
    whole voice message on the floor.
    """
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True)
    runner._has_setup_skill = lambda: False

    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={
            "success": True,
            "transcript": "hello from a captionless voice note",
            "provider": "local_command",
        },
    ):
        result, transcripts = await runner._enrich_message_with_transcription(
            "(The user sent a message with no text content)",
            ["/tmp/voice.ogg"],
        )

    # The redundant placeholder is stripped, leaving only the transcript prefix.
    assert "hello from a captionless voice note" in result
    assert "(The user sent a message with no text content)" not in result
    # Crucially, the transcripts are still surfaced so callers can echo them.
    assert transcripts == ["hello from a captionless voice note"]


@pytest.mark.asyncio
async def test_enrich_message_with_transcription_guards_empty_transcript():
    """success=True with an empty/whitespace transcript must not emit empty
    quotes — it gets a sentinel note and is excluded from transcripts (#41603)."""
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True)
    runner._has_setup_skill = lambda: False

    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={"success": True, "transcript": "   \n\t", "provider": "local_command"},
    ):
        result, transcripts = await runner._enrich_message_with_transcription(
            "caption",
            ["/tmp/voice.ogg"],
        )

    assert "empty or inaudible" in result
    assert '""' not in result
    assert transcripts == []


