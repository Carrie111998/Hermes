"""Regression tests: a voice message must reach Whisper exactly once.

Production bug: when a voice message arrives during an active agent run, the
adapter queues the event and signals an interrupt. The interrupt path
(``_dequeue_pending_with_transcription``) transcribes the cached audio file,
and then the very same event/file is preprocessed again through the normal
inbound path (``_prepare_inbound_message_text``), which transcribes it a second
time. Logs showed a single cached audio file and two consecutive Whisper calls
— and, with ``stt.echo_transcripts: true``, two 🎙️ echoes for one voice note.

These tests pin: one STT call per audio file across both paths, exactly one
echo when echoing is enabled, none when it is disabled, distinct files still
transcribed independently, and failures never silently reused.
"""

import asyncio
import threading
import time
from unittest.mock import patch

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource


class _FakeAdapter:
    """Minimal adapter double: pending-message queue + recorded sends."""

    def __init__(self, pending=None):
        self._pending = dict(pending or {})
        self.sent = []

    def get_pending_message(self, session_key):
        return self._pending.pop(session_key, None)

    async def send(self, chat_id, text, metadata=None):
        self.sent.append((chat_id, text, metadata))
        return True


def _make_runner(adapter, *, echo=True):
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True, stt_echo_transcripts=echo)
    runner.adapters = {}
    runner._model = "test-model"
    runner._base_url = ""
    runner._has_setup_skill = lambda: False
    runner._adapter_for_source = lambda source: adapter
    runner._thread_metadata_for_source = lambda source, anchor=None: None
    runner._reply_anchor_for_event = lambda event: None
    return runner


def _make_source():
    return SessionSource(platform=Platform.TELEGRAM, chat_id="123", chat_type="dm")


def _make_voice_event(source, path):
    return MessageEvent(
        text="",
        message_type=MessageType.VOICE,
        source=source,
        media_urls=[str(path)],
        media_types=["audio/ogg"],
    )


def _write_audio(tmp_path, name="voice.ogg", content=b"OggS-fake-audio"):
    path = tmp_path / name
    path.write_bytes(content)
    return path


class _CountingTranscriber:
    """Stand-in for ``transcribe_audio`` that counts calls per path."""

    def __init__(self, transcript="hello from the voice note", delay=0.0):
        self.transcript = transcript
        self.delay = delay
        self.calls = []
        self._lock = threading.Lock()

    def __call__(self, path):
        with self._lock:
            self.calls.append(path)
        if self.delay:
            time.sleep(self.delay)
        return {
            "success": True,
            "transcript": self.transcript,
            "provider": "local_command",
        }


@pytest.mark.asyncio
async def test_interrupt_then_normal_path_transcribes_and_echoes_once(tmp_path):
    """The interrupt path and the normal path must share one transcription."""
    audio = _write_audio(tmp_path)
    adapter = _FakeAdapter()
    source = _make_source()
    event = _make_voice_event(source, audio)
    session_key = "telegram:123"
    adapter._pending[session_key] = event

    runner = _make_runner(adapter, echo=True)
    transcriber = _CountingTranscriber()

    with patch("tools.transcription_tools.transcribe_audio", transcriber):
        # 1. Voice message arrives mid-run: the adapter queues it and the
        #    interrupt path drains + transcribes it.
        interrupt_text = await runner._dequeue_pending_with_transcription(
            adapter, session_key, source,
        )
        # 2. The same event is then preprocessed by the normal inbound path.
        normal_text = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

    # Both paths still hand the agent the real transcript...
    assert interrupt_text is not None and transcriber.transcript in interrupt_text
    assert normal_text is not None and transcriber.transcript in normal_text
    # ...but Whisper is called exactly once for the one cached audio file.
    assert transcriber.calls == [str(audio)], (
        f"expected a single STT call, got {len(transcriber.calls)}: {transcriber.calls}"
    )
    # And the user sees exactly one 🎙️ echo, not two.
    echoes = [text for _chat, text, _meta in adapter.sent if text.startswith("🎙️")]
    assert echoes == [f'🎙️ "{transcriber.transcript}"'], f"duplicate echoes: {echoes}"


@pytest.mark.asyncio
async def test_normal_path_then_interrupt_path_transcribes_and_echoes_once(tmp_path):
    """Order must not matter: the reverse sequence dedupes identically."""
    audio = _write_audio(tmp_path)
    adapter = _FakeAdapter()
    source = _make_source()
    event = _make_voice_event(source, audio)
    session_key = "telegram:123"
    adapter._pending[session_key] = event

    runner = _make_runner(adapter, echo=True)
    transcriber = _CountingTranscriber()

    with patch("tools.transcription_tools.transcribe_audio", transcriber):
        normal_text = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )
        interrupt_text = await runner._dequeue_pending_with_transcription(
            adapter, session_key, source,
        )

    assert normal_text is not None and transcriber.transcript in normal_text
    assert interrupt_text is not None and transcriber.transcript in interrupt_text
    assert len(transcriber.calls) == 1
    echoes = [text for _chat, text, _meta in adapter.sent if text.startswith("🎙️")]
    assert len(echoes) == 1


@pytest.mark.asyncio
async def test_concurrent_calls_for_same_file_share_one_transcription(tmp_path):
    """Two paths racing on the same file must not both hit Whisper."""
    audio = _write_audio(tmp_path)
    adapter = _FakeAdapter()
    runner = _make_runner(adapter, echo=True)
    transcriber = _CountingTranscriber(delay=0.15)

    with patch("tools.transcription_tools.transcribe_audio", transcriber):
        first, second = await asyncio.gather(
            runner._enrich_message_with_transcription("", [str(audio)]),
            runner._enrich_message_with_transcription("", [str(audio)]),
        )

    assert len(transcriber.calls) == 1
    # Both callers get usable enriched text for the agent...
    assert transcriber.transcript in first[0]
    assert transcriber.transcript in second[0]
    # ...but only one of them owns the echo, so the user sees one 🎙️ line.
    assert sorted([len(first[1]), len(second[1])]) == [0, 1]


@pytest.mark.asyncio
async def test_sequential_calls_for_same_file_share_one_transcription(tmp_path):
    audio = _write_audio(tmp_path)
    adapter = _FakeAdapter()
    runner = _make_runner(adapter, echo=True)
    transcriber = _CountingTranscriber()

    with patch("tools.transcription_tools.transcribe_audio", transcriber):
        first = await runner._enrich_message_with_transcription("", [str(audio)])
        second = await runner._enrich_message_with_transcription("", [str(audio)])

    assert len(transcriber.calls) == 1
    assert transcriber.transcript in first[0]
    assert transcriber.transcript in second[0]
    assert first[1] == [transcriber.transcript]
    assert second[1] == []


@pytest.mark.asyncio
async def test_distinct_files_are_each_transcribed_and_echoed(tmp_path):
    """Dedupe is per-file: two different voice notes stay independent."""
    audio_a = _write_audio(tmp_path, "a.ogg", b"OggS-a")
    audio_b = _write_audio(tmp_path, "b.ogg", b"OggS-b")
    adapter = _FakeAdapter()
    source = _make_source()
    runner = _make_runner(adapter, echo=True)
    transcriber = _CountingTranscriber()

    with patch("tools.transcription_tools.transcribe_audio", transcriber):
        text_a = await runner._prepare_inbound_message_text(
            event=_make_voice_event(source, audio_a),
            source=source,
            history=[],
        )
        text_b = await runner._prepare_inbound_message_text(
            event=_make_voice_event(source, audio_b),
            source=source,
            history=[],
        )

    assert sorted(transcriber.calls) == sorted([str(audio_a), str(audio_b)])
    assert text_a is not None and text_b is not None
    echoes = [text for _chat, text, _meta in adapter.sent if text.startswith("🎙️")]
    assert len(echoes) == 2


@pytest.mark.asyncio
async def test_no_echo_when_stt_echo_transcripts_disabled(tmp_path):
    """echo_transcripts=false: still one STT call, and zero 🎙️ messages."""
    audio = _write_audio(tmp_path)
    adapter = _FakeAdapter()
    source = _make_source()
    event = _make_voice_event(source, audio)
    session_key = "telegram:123"
    adapter._pending[session_key] = event

    runner = _make_runner(adapter, echo=False)
    transcriber = _CountingTranscriber()

    with patch("tools.transcription_tools.transcribe_audio", transcriber):
        interrupt_text = await runner._dequeue_pending_with_transcription(
            adapter, session_key, source,
        )
        normal_text = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

    assert len(transcriber.calls) == 1
    assert transcriber.transcript in interrupt_text
    assert transcriber.transcript in normal_text
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_failed_transcription_is_not_cached(tmp_path):
    """Failures must not be reused: a later attempt retries the provider."""
    audio = _write_audio(tmp_path)
    adapter = _FakeAdapter()
    runner = _make_runner(adapter, echo=True)

    calls = []

    def _flaky(path):
        calls.append(path)
        if len(calls) == 1:
            return {"success": False, "error": "backend timeout"}
        return {"success": True, "transcript": "second time lucky", "provider": "x"}

    with patch("tools.transcription_tools.transcribe_audio", _flaky):
        first_text, first_transcripts = await runner._enrich_message_with_transcription(
            "", [str(audio)],
        )
        second_text, second_transcripts = await runner._enrich_message_with_transcription(
            "", [str(audio)],
        )

    assert len(calls) == 2, "a failed transcription must be retried, not cached"
    assert "[voice message could not be transcribed]" in first_text
    assert first_transcripts == []
    assert "second time lucky" in second_text
    assert second_transcripts == ["second time lucky"]


@pytest.mark.asyncio
async def test_raising_transcriber_is_not_cached_and_stays_visible(tmp_path, caplog):
    """A raising provider must be logged and retried, never silently reused."""
    audio = _write_audio(tmp_path)
    adapter = _FakeAdapter()
    runner = _make_runner(adapter, echo=True)

    calls = []

    def _boom(path):
        calls.append(path)
        raise RuntimeError("whisper exploded")

    with caplog.at_level("ERROR"), patch(
        "tools.transcription_tools.transcribe_audio", _boom
    ):
        text_one, _ = await runner._enrich_message_with_transcription("", [str(audio)])
        text_two, _ = await runner._enrich_message_with_transcription("", [str(audio)])

    assert len(calls) == 2
    assert "[voice message could not be transcribed]" in text_one
    assert "[voice message could not be transcribed]" in text_two
    assert "whisper exploded" in caplog.text


@pytest.mark.asyncio
async def test_transcription_cache_is_bounded(tmp_path):
    """The per-runner cache must not grow without bound."""
    from gateway.run import GatewayRunner

    adapter = _FakeAdapter()
    runner = _make_runner(adapter, echo=True)
    transcriber = _CountingTranscriber()

    limit = GatewayRunner._STT_RESULT_CACHE_MAX
    with patch("tools.transcription_tools.transcribe_audio", transcriber):
        for i in range(limit + 5):
            audio = _write_audio(tmp_path, f"voice-{i}.ogg", f"OggS-{i}".encode())
            await runner._enrich_message_with_transcription("", [str(audio)])

    assert len(transcriber.calls) == limit + 5
    assert len(runner._stt_result_cache) <= limit
