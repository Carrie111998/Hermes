"""Content-identity dedup at the gateway STT boundary (#91513).

The same Telegram voice can reach one turn through both the current-media
and replied-to-media routes; each download gets its own local cache path,
so the old path-only dedup transcribed (and billed) it twice. The gateway
must collapse identical audio BYTES across distinct paths, keep distinct
audio distinct, and never merge unreadable paths.
"""

from unittest.mock import patch

import pytest

from gateway.config import GatewayConfig
from gateway.session import SessionSource


def _make_runner() -> "GatewayRunner":  # type: ignore[name-defined]
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True)
    runner.adapters = {}
    runner._model = "test-model"
    runner._base_url = ""
    runner._has_setup_skill = lambda: False
    return runner


_SOURCE = SessionSource(platform="telegram", chat_id="1", chat_type="dm")


def _fake_transcribe(transcript: str = "hello world"):
    return patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={"success": True, "transcript": transcript, "provider": "whisper"},
    )


@pytest.mark.asyncio
async def test_same_bytes_different_paths_transcribed_once(tmp_path):
    """Two cache paths with identical audio bytes -> one STT call (#91513)."""
    a = tmp_path / "audio_A.ogg"
    b = tmp_path / "audio_B.ogg"
    payload = b"same-voice-bytes" * 4
    a.write_bytes(payload)
    b.write_bytes(payload)

    runner = _make_runner()
    with _fake_transcribe() as mock_transcribe:
        enriched, transcripts = await runner._enrich_message_with_transcription(
            "check this", [str(a), str(b)]
        )

    assert mock_transcribe.call_count == 1, (
        "identical bytes via distinct cache paths must be transcribed once"
    )
    assert transcripts == ["hello world"]


@pytest.mark.asyncio
async def test_different_bytes_both_transcribed(tmp_path):
    """Distinct audio files must remain distinct after dedup."""
    a = tmp_path / "audio_A.ogg"
    b = tmp_path / "audio_B.ogg"
    a.write_bytes(b"voice-one" * 4)
    b.write_bytes(b"voice-two" * 4)

    runner = _make_runner()
    with _fake_transcribe() as mock_transcribe:
        enriched, transcripts = await runner._enrich_message_with_transcription(
            "check this", [str(a), str(b)]
        )

    assert mock_transcribe.call_count == 2
    assert transcripts == ["hello world", "hello world"]


@pytest.mark.asyncio
async def test_repeated_identical_path_still_once(tmp_path):
    """The pre-existing path dedup behavior is preserved."""
    a = tmp_path / "audio_A.ogg"
    a.write_bytes(b"voice" * 4)

    runner = _make_runner()
    with _fake_transcribe() as mock_transcribe:
        await runner._enrich_message_with_transcription(
            "check this", [str(a), str(a)]
        )

    assert mock_transcribe.call_count == 1


@pytest.mark.asyncio
async def test_unreadable_paths_do_not_collapse_or_crash(tmp_path):
    """Missing/unreadable paths fall back to path identity: no crash, and two
    distinct unreadable paths are never merged into one another."""
    missing_a = str(tmp_path / "missing_A.ogg")
    missing_b = str(tmp_path / "missing_B.ogg")

    runner = _make_runner()
    with _fake_transcribe() as mock_transcribe:
        enriched, transcripts = await runner._enrich_message_with_transcription(
            "check this", [missing_a, missing_b]
        )

    # Two distinct unreadable paths must never merge with each other (the
    # dedup key falls back to path identity). The mock always succeeds, so
    # both attempted transcriptions "succeed" — the call count is the
    # no-accidental-collapse proof.
    assert mock_transcribe.call_count == 2
    assert transcripts == ["hello world", "hello world"]
