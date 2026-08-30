"""Coverage for the TTS streaming pipeline in ``tools/tts_tool.py``.

Covers the classes/functions introduced for per-sentence streaming:
``_SyncSentencePipeline`` (its ``__init__``, ``speak``, ``close``,
``_synthesize_to_tmp`` and ``_drain``) plus the orchestrator
``stream_tts_to_speaker`` in its sync-pipeline fallback form (when no
chunked streaming provider is available) and in its chunked-streamer form on
macOS (where ``output_stream`` is intentionally left ``None`` so audio goes
through the tempfile -> ``play_audio_file`` path).

No real audio device is ever touched: ``tools.voice_mode.play_audio_file``
is replaced by a recorder stub and the chunked streamer is a fake object that
yields tiny int16 PCM chunks.
"""

import os
import queue
import sys
import tempfile
import threading
import time
import types
import wave

import pytest

import tools.tts_streaming as tts_streaming
import tools.tts_tool as tts_tool
from tools.tts_tool import _SyncSentencePipeline, stream_tts_to_speaker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_tiny_wav(path: str, frames: bytes = b"\x00\x00" * 8, rate: int = 24000) -> None:
    """Write a tiny, valid 16-bit mono WAV to *path* so playback sees a
    non-empty, real audio file (the pipeline only checks ``getsize > 0``)."""
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(frames)


def _make_tmp_wav_path() -> str:
    """Create a tiny WAV temp file and return its path (caller unlinks)."""
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    _write_tiny_wav(path)
    return path


def _settled_assert(baseline: int) -> None:
    """Assert no threads leaked past *baseline* (with a grace window)."""
    deadline = time.time() + 3.0
    while threading.active_count() > baseline and time.time() < deadline:
        time.sleep(0.01)
    assert threading.active_count() <= baseline, (
        f"leaked threads: active={threading.active_count()} baseline={baseline}"
    )


class _FakeStreamer:
    """Stands in for a chunked StreamingTTSProvider.

    Yields a few int16-aligned PCM chunks so the Darwin path can run the
    tempfile playback branch without any real audio device or numpy.
    """

    sample_rate = 24000
    channels = 1

    def __init__(self):
        self.streamed: list[str] = []

    def stream(self, text):
        self.streamed.append(text)

        def _gen():
            for chunk in (b"\x01\x00", b"\x02\x00", b"\x03\x00", b"\x04\x00"):
                yield chunk

        return _gen()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_voice_mode(monkeypatch):
    """Replace ``tools.voice_mode`` with a recorder stub.

    The pipeline imports ``play_audio_file`` lazily inside the worker/player
    threads, so swapping the module object in ``sys.modules`` lets those
    threads see the stub. Returns the list of played paths.
    """
    played: list[str] = []
    mod = types.ModuleType("tools.voice_mode")
    mod.play_audio_file = lambda path: played.append(path)
    mod.mark_audio_output_active = lambda _active: None
    monkeypatch.setitem(sys.modules, "tools.voice_mode", mod)
    return played


def _patch_sync_fallback(monkeypatch):
    """Force the per-sentence sync-pipeline fallback (no chunked streamer)."""
    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {})
    monkeypatch.setattr(
        tts_streaming, "resolve_streaming_provider", lambda cfg, preferred=None: None
    )


# ---------------------------------------------------------------------------
# _SyncSentencePipeline
# ---------------------------------------------------------------------------

def test_sync_pipeline_speaks_in_order_with_lookahead(fake_voice_mode, monkeypatch):
    """Sentences are synthesized FIFO and played FIFO with the lookahead bound."""
    played = fake_voice_mode
    synth_texts: list[str] = []

    def fake_tts(text, output_path, **kwargs):
        synth_texts.append(text)
        _write_tiny_wav(output_path)
        return {"ok": True}

    monkeypatch.setattr(tts_tool, "text_to_speech_tool", fake_tts)

    baseline = threading.active_count()
    pipe = _SyncSentencePipeline(threading.Event(), lookahead=2)
    sentences = [
        "First sentence here has enough length to speak.",
        "Second sentence here has enough length too.",
        "Third sentence here carries a bit more content.",
    ]
    for s in sentences:
        pipe.speak(s)
    pipe.close()

    assert synth_texts == sentences
    assert len(played) == len(sentences)
    _settled_assert(baseline)


def test_sync_pipeline_speak_returns_when_stop_set(fake_voice_mode, monkeypatch):
    """``speak`` must no-op once stop_event is set, and ``close`` still joins."""
    played = fake_voice_mode
    synth: list[str] = []

    def fake_tts(text, output_path, **kwargs):
        synth.append(text)
        _write_tiny_wav(output_path)
        return {"ok": True}

    monkeypatch.setattr(tts_tool, "text_to_speech_tool", fake_tts)

    baseline = threading.active_count()
    stop = threading.Event()
    stop.set()
    pipe = _SyncSentencePipeline(stop)
    pipe.speak("This should never be queued.")
    pipe.close()

    assert synth == []
    assert played == []
    _settled_assert(baseline)


def test_synthesize_to_tmp_returns_path_on_success(monkeypatch):
    """Real ``_synthesize_to_tmp`` returns a non-empty file when synthesis works."""
    def fake_tts(text, output_path, **kwargs):
        _write_tiny_wav(output_path)
        return {"ok": True}

    monkeypatch.setattr(tts_tool, "text_to_speech_tool", fake_tts)

    pipe = _SyncSentencePipeline.__new__(_SyncSentencePipeline)
    pipe._stop = threading.Event()
    result = pipe._synthesize_to_tmp("speak this aloud")
    assert result is not None
    assert os.path.isfile(result)
    assert os.path.getsize(result) > 0
    os.unlink(result)


def test_synthesize_to_tmp_returns_none_when_stop_set(monkeypatch):
    """``_synthesize_to_tmp`` bails out to ``None`` when stop_event is set."""
    def fake_tts(text, output_path, **kwargs):
        _write_tiny_wav(output_path)
        return {"ok": True}

    monkeypatch.setattr(tts_tool, "text_to_speech_tool", fake_tts)

    pipe = _SyncSentencePipeline.__new__(_SyncSentencePipeline)
    pipe._stop = threading.Event()
    pipe._stop.set()
    assert pipe._synthesize_to_tmp("speak this aloud") is None


def test_synthesize_to_tmp_unlinks_tmp_on_failure(monkeypatch):
    """A synthesis exception is swallowed, the temp file is removed, None returned."""
    def failing_tts(text, output_path, **kwargs):
        raise RuntimeError("synthesis exploded")

    monkeypatch.setattr(tts_tool, "text_to_speech_tool", failing_tts)

    pipe = _SyncSentencePipeline.__new__(_SyncSentencePipeline)
    pipe._stop = threading.Event()
    assert pipe._synthesize_to_tmp("speak this aloud") is None


# ---------------------------------------------------------------------------
# stream_tts_to_speaker — sync fallback
# ---------------------------------------------------------------------------

def test_stream_empty_input_flush(fake_voice_mode, monkeypatch):
    """A lone ``None`` sentinel flushes an empty buffer: nothing spoken, done set."""
    played = fake_voice_mode
    _patch_sync_fallback(monkeypatch)

    text_queue = queue.Queue()
    stop = threading.Event()
    done = threading.Event()
    baseline = threading.active_count()
    text_queue.put(None)

    stream_tts_to_speaker(text_queue, stop, done)

    assert done.is_set()
    assert played == []
    _settled_assert(baseline)


def test_stream_sync_speaks_and_drains_remaining(fake_voice_mode, monkeypatch):
    """Sync fallback speaks buffered sentences on the sentinel and drains leftovers."""
    played = fake_voice_mode
    _patch_sync_fallback(monkeypatch)
    synth: list[str] = []

    def fake_tts(text, output_path, **kwargs):
        synth.append(text)
        _write_tiny_wav(output_path)
        return {"ok": True}

    monkeypatch.setattr(tts_tool, "text_to_speech_tool", fake_tts)

    text_queue = queue.Queue()
    stop = threading.Event()
    done = threading.Event()
    displayed: list[str] = []
    baseline = threading.active_count()

    text_queue.put("The first sentence is long enough to speak. And the second one too, here it is.")
    text_queue.put(None)
    text_queue.put("ignored-after-sentinel")  # drained (not fed) at the end

    stream_tts_to_speaker(text_queue, stop, done, display_callback=displayed.append)

    assert done.is_set()
    assert displayed
    assert len(synth) >= 1
    assert len(played) == len(synth)
    _settled_assert(baseline)


def test_stream_long_buffer_idle_flush(fake_voice_mode, monkeypatch):
    """A long buffer without a sentence boundary is flushed on queue idle."""
    played = fake_voice_mode
    _patch_sync_fallback(monkeypatch)

    text_queue = queue.Queue()
    stop = threading.Event()
    done = threading.Event()
    baseline = threading.active_count()

    def producer():
        text_queue.put("x" * 150)  # no sentence boundary; > long_flush_len(100)
        time.sleep(1.0)
        text_queue.put(None)

    prod = threading.Thread(target=producer, daemon=True)
    prod.start()
    stream_tts_to_speaker(text_queue, stop, done)
    prod.join(timeout=5.0)

    assert done.is_set()
    assert len(played) == 1
    _settled_assert(baseline)


def test_stream_sync_skips_duplicate_sentences(fake_voice_mode, monkeypatch):
    """Near-identical repeated sentences should be spoken only once."""
    played = fake_voice_mode
    _patch_sync_fallback(monkeypatch)

    def fake_tts(text, output_path, **kwargs):
        _write_tiny_wav(output_path)
        return {"ok": True}

    monkeypatch.setattr(tts_tool, "text_to_speech_tool", fake_tts)

    text_queue = queue.Queue()
    stop = threading.Event()
    done = threading.Event()
    baseline = threading.active_count()

    text_queue.put("Repeat sentence here. Repeat sentence here.")
    text_queue.put(None)

    stream_tts_to_speaker(text_queue, stop, done)

    assert done.is_set()
    assert len(played) == 1
    _settled_assert(baseline)


def test_stream_barge_in_stops_playback(fake_voice_mode, monkeypatch):
    """Setting stop_event mid-stream cancels pending playback and still sets done."""
    played = fake_voice_mode
    _patch_sync_fallback(monkeypatch)

    synth_started = threading.Event()
    release = threading.Event()

    def gated_synth(self, cleaned):
        synth_started.set()
        release.wait(timeout=5.0)
        return _make_tmp_wav_path()

    monkeypatch.setattr(_SyncSentencePipeline, "_synthesize_to_tmp", gated_synth)

    text_queue = queue.Queue()
    stop = threading.Event()
    done = threading.Event()
    baseline = threading.active_count()

    # One full sentence (so chunker produces it) plus a trailing fragment buffered.
    text_queue.put("First sentence right here. Second sentence goes here too.")

    th = threading.Thread(
        target=stream_tts_to_speaker, args=(text_queue, stop, done), daemon=True
    )
    th.start()

    assert synth_started.wait(3.0), "synthesis never started"
    stop.set()          # barge in before the pending sentence plays
    release.set()       # let the gated synthesis finish
    th.join(timeout=6.0)

    assert not th.is_alive()
    assert done.is_set()
    assert played == []  # nothing played after barge-in
    _settled_assert(baseline)


def test_stream_sets_done_when_synth_raises(fake_voice_mode, monkeypatch):
    """Even when synthesis raises, tts_done_event is set in the finally block."""
    played = fake_voice_mode
    _patch_sync_fallback(monkeypatch)

    def raising_synth(self, cleaned):
        raise RuntimeError("synthesis exploded")

    monkeypatch.setattr(_SyncSentencePipeline, "_synthesize_to_tmp", raising_synth)

    text_queue = queue.Queue()
    stop = threading.Event()
    done = threading.Event()
    baseline = threading.active_count()

    text_queue.put("A sufficiently long sentence to trigger per sentence processing.")
    text_queue.put(None)

    stream_tts_to_speaker(text_queue, stop, done)

    assert done.is_set()
    assert played == []
    _settled_assert(baseline)


def test_stream_sets_done_when_config_load_raises(fake_voice_mode, monkeypatch):
    """A failure inside the try block still lands in finally and sets done."""
    played = fake_voice_mode

    def boom():
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(tts_tool, "_load_tts_config", boom)

    text_queue = queue.Queue()
    stop = threading.Event()
    done = threading.Event()
    baseline = threading.active_count()

    stream_tts_to_speaker(text_queue, stop, done)

    assert done.is_set()
    assert played == []
    _settled_assert(baseline)


# ---------------------------------------------------------------------------
# stream_tts_to_speaker — chunked streamer on macOS (output_stream stays None)
# ---------------------------------------------------------------------------

def test_stream_chunked_darwin_plays_via_tempfile(fake_voice_mode, monkeypatch):
    """A chunked streamer with no real output stream plays through temp WAV."""
    played = fake_voice_mode
    streamer = _FakeStreamer()
    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {})
    monkeypatch.setattr(
        tts_streaming, "resolve_streaming_provider", lambda cfg, preferred=None: streamer
    )
    # Force a tiny per-request cap so the truncation path is exercised.
    monkeypatch.setattr(
        tts_tool, "_resolve_max_text_length", lambda provider, tts_config=None, **kw: 10
    )

    # Sanity guard: this branch is the macOS output_stream=None route.
    assert tts_tool.platform.system() == "Darwin"

    text_queue = queue.Queue()
    stop = threading.Event()
    done = threading.Event()
    displayed: list[str] = []
    baseline = threading.active_count()

    text_queue.put("This is a very long sentence for the streaming provider to speak aloud.")
    text_queue.put(None)

    stream_tts_to_speaker(text_queue, stop, done, display_callback=displayed.append)

    assert done.is_set()
    assert streamer.streamed, "stream() was never called"
    # Truncated to the 10-char cap.
    assert len(streamer.streamed[0]) == 10
    assert played, "chunked path should have played one tempfile"
    assert displayed
    _settled_assert(baseline)
