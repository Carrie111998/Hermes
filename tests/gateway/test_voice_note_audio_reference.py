"""A successful voice-note transcription must still carry the audio reference (#93982).

The STT success branch emitted only the quoted transcript — no path, no
duration, no indication the message was spoken rather than typed — so
audio-aware workflows (verifying a questionable transcription, analysing
non-speech audio, filing a voice memo) could not reach the media, and a bare
quoted string read as something the user typed. The failure and
stt-disabled branches already emit the agent-visible cache path; the success
branch now appends the same convention as a terse bracketed suffix after the
transcript (keeping the plain quoted line the meta-instruction comment guards).
"""

from unittest.mock import patch

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource


def _make_runner(stt_enabled: bool = True):
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=stt_enabled)
    runner.adapters = {}
    runner._model = "test-model"
    runner._base_url = ""
    runner._has_setup_skill = lambda: False
    return runner


@pytest.mark.asyncio
async def test_successful_transcript_keeps_audio_reference(monkeypatch, tmp_path):
    runner = _make_runner(stt_enabled=True)
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"ogg")

    def fake_visible(path):
        return f"/root{path}"

    async def fake_duration(path):
        return "0:04"

    monkeypatch.setattr(
        "tools.credential_files.to_agent_visible_cache_path", fake_visible
    )
    monkeypatch.setattr("gateway.run._probe_audio_duration", fake_duration)

    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={"success": True, "transcript": "hello world", "provider": "whisper"},
    ):
        enriched, transcripts = await runner._enrich_message_with_transcription(
            "", [str(audio)]
        )

    assert transcripts == ["hello world"]
    # The quoted transcript line survives verbatim…
    assert '"hello world"' in enriched
    # …and the bracketed suffix carries the agent-visible audio reference
    # plus the duration, matching the convention the other branches use.
    assert "[voice note, 0:04, audio: /root" in enriched
    assert str(audio) in enriched


@pytest.mark.asyncio
async def test_successful_transcript_without_duration(monkeypatch, tmp_path):
    runner = _make_runner(stt_enabled=True)
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"ogg")

    def fake_visible(path):
        return f"/root{path}"

    async def fake_duration(path):
        return None

    monkeypatch.setattr(
        "tools.credential_files.to_agent_visible_cache_path", fake_visible
    )
    monkeypatch.setattr("gateway.run._probe_audio_duration", fake_duration)

    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={"success": True, "transcript": "hi", "provider": "whisper"},
    ):
        enriched, transcripts = await runner._enrich_message_with_transcription(
            "", [str(audio)]
        )

    assert transcripts == ["hi"]
    # No duration probe result → the suffix degrades to the path only.
    assert "[voice note, audio: /root" in enriched
    assert "0:" not in enriched


@pytest.mark.asyncio
async def test_transcript_still_reaches_agent_unchanged(monkeypatch, tmp_path):
    """End-to-end shape: the turn text contains both the quoted transcript
    and the bracketed audio reference (#93982), not a bare quoted string."""
    runner = _make_runner(stt_enabled=True)
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"ogg")

    def fake_visible(path):
        return f"/root{path}"

    async def fake_duration(path):
        return None

    monkeypatch.setattr(
        "tools.credential_files.to_agent_visible_cache_path", fake_visible
    )
    monkeypatch.setattr("gateway.run._probe_audio_duration", fake_duration)

    event = MessageEvent(
        text="",
        message_type=MessageType.VOICE,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="1", chat_type="dm"),
        media_urls=[str(audio)],
        media_types=["audio/ogg"],
    )
    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={"success": True, "transcript": "hello world", "provider": "whisper"},
    ):
        result = await runner._prepare_inbound_message_text(
            event=event,
            source=event.source,
            history=[],
        )

    assert '"hello world"' in result
    assert "[voice note, audio: /root" in result
