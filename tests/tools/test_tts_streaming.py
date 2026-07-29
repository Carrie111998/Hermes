"""Tests for the provider-agnostic streaming TTS backend (tools.tts_streaming)
and its dispatch through tools.tts_tool.stream_tts_to_speaker.

No live audio or network: the ElevenLabs/OpenAI SDKs, sounddevice, and the sync
synth path are all mocked. Covers the registry/resolver, provider availability,
the chunked-streamer playback path, and the universal per-sentence sync fallback.
"""

import os
import queue
import tempfile
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

import tools.tts_streaming as ts

pytest.importorskip("numpy")


# ── SentenceChunker ──────────────────────────────────────────────────────


class TestSentenceChunker:
    def test_cuts_sentence_the_moment_its_boundary_arrives(self):
        c = ts.SentenceChunker()
        assert c.feed("This is the first full") == []
        assert c.feed(" sentence of it all. And") == ["This is the first full sentence of it all. "]
        assert c.flush() == ["And"]


    def test_think_blocks_are_stripped_even_across_deltas(self):
        c = ts.SentenceChunker()
        assert c.feed("<think>secret reason") == []
        assert c.feed("ing</think>The actual spoken answer. ") == ["The actual spoken answer. "]


    def test_paragraph_break_is_a_boundary(self):
        c = ts.SentenceChunker()
        assert c.feed("A paragraph without punctuation\n\nnext one") == [
            "A paragraph without punctuation\n\n"
        ]


# ── Interruption latch ───────────────────────────────────────────────────


class TestSpeechInterruptedLatch:
    def test_take_pops_and_reports_recent_barge(self):
        ts.mark_speech_interrupted()
        assert ts.take_speech_interrupted() is True
        assert ts.take_speech_interrupted() is False  # one-shot


    def test_stale_barge_expires(self, monkeypatch):
        ts.mark_speech_interrupted()
        at = ts._interrupted_at
        monkeypatch.setattr(ts.time, "monotonic", lambda: at + ts._INTERRUPT_TTL_S + 1)
        assert ts.take_speech_interrupted() is False


# ── Registry + resolver ──────────────────────────────────────────────────


def _register_fake(monkeypatch, name, available=True, chunks=(b"\x00\x00",)):
    class _Fake(ts.StreamingTTSProvider):
        sample_rate = 24000

        @staticmethod
        def available():
            return available

        def stream(self, text):
            yield from chunks

    monkeypatch.setitem(ts._REGISTRY, name, _Fake)
    return _Fake


def test_resolve_returns_configured_streamer(monkeypatch):
    _register_fake(monkeypatch, "faketts")
    prov = ts.resolve_streaming_provider({"provider": "faketts"})
    assert isinstance(prov, ts.StreamingTTSProvider)


def test_never_swaps_provider_for_streaming(monkeypatch):
    # A registered streamer must NOT be substituted when the user picked another
    # (non-streaming) provider — that would silently change their voice.
    _register_fake(monkeypatch, "elevenlabs")
    assert ts.resolve_streaming_provider({"provider": "edge"}) is None


# ── Built-in provider availability ───────────────────────────────────────


def test_elevenlabs_available_reflects_key(monkeypatch):
    # Key lookups now route through the provider-secret resolver
    # (config > env/.env > credential pool), not bare get_env_value.
    monkeypatch.setattr(ts, "_resolve_key", lambda env, pid: "key" if env == "ELEVENLABS_API_KEY" else "")
    assert ts.ElevenLabsStreamer.available() is True
    monkeypatch.setattr(ts, "_resolve_key", lambda env, pid: "")
    assert ts.ElevenLabsStreamer.available() is False


def test_openai_available_reflects_audio_key_resolution(monkeypatch):
    monkeypatch.setattr(ts, "_openai_config_api_key", lambda: "")
    monkeypatch.setattr(ts, "resolve_openai_audio_api_key", lambda: "voice-key")
    assert ts.OpenAIStreamer.available() is True
    monkeypatch.setattr(ts, "resolve_openai_audio_api_key", lambda: "")
    assert ts.OpenAIStreamer.available() is False
    # tts.openai.api_key from config.yaml counts too
    monkeypatch.setattr(ts, "_openai_config_api_key", lambda: "cfg-key")
    assert ts.OpenAIStreamer.available() is True


def test_openai_streamer_prefers_configured_api_key(monkeypatch):
    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def iter_bytes(self):
            yield b"\x01\x00"

    class _StreamingCreate:
        @staticmethod
        def create(**kwargs):
            return _Response()

    class _OpenAI:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.audio = MagicMock()
            self.audio.speech.with_streaming_response = _StreamingCreate()

    monkeypatch.setattr(ts, "resolve_openai_audio_api_key", lambda: "env-key")
    monkeypatch.setattr(ts, "get_env_value", lambda key, *args: None)
    monkeypatch.setattr("openai.OpenAI", _OpenAI)

    config = {
        "provider": "openai",
        "openai": {"api_key": "cfg-key", "base_url": "http://local-tts.example/v1"},
    }
    streamer = ts.resolve_streaming_provider(config)

    assert streamer is not None
    assert list(streamer.stream("Streaming test.")) == [b"\x01\x00"]
    assert captured["client"]["api_key"] == "cfg-key"


# ── Dispatch: chunked streamer path ──────────────────────────────────────


def _drain_queue(sentences):
    q = queue.Queue()
    for s in sentences:
        q.put(s)
    q.put(None)
    return q


def _sd_mock():
    sd = MagicMock()
    out = MagicMock()
    sd.OutputStream.return_value = out
    return sd, out


# ── Dispatch: universal per-sentence sync fallback ───────────────────────


# ── tts.streaming.provider config knob (salvaged from PR #47588) ─────────


# ── Credential routing: resolve_provider_secret, never bare env ──────────


def test_elevenlabs_available_routes_through_secret_resolver(monkeypatch):
    calls = []

    def _fake_resolve(env_var, provider_id):
        calls.append((env_var, provider_id))
        return "pool-key"

    monkeypatch.setattr(ts, "_resolve_key", _fake_resolve)
    assert ts.ElevenLabsStreamer.available() is True
    assert ("ELEVENLABS_API_KEY", "elevenlabs") in calls


def test_xai_available_uses_oauth_credential_resolver(monkeypatch):
    import sys
    import types

    fake = types.ModuleType("tools.xai_http")
    fake.resolve_xai_http_credentials = lambda: {"api_key": "xai-key"}
    monkeypatch.setitem(sys.modules, "tools.xai_http", fake)
    assert ts.XAIStreamer.available() is True
    fake.resolve_xai_http_credentials = lambda: {"api_key": ""}
    assert ts.XAIStreamer.available() is False


# ── Gemini SSE parsing ────────────────────────────────────────────────────


# ── xAI WebSocket bridge ─────────────────────────────────────────────────


# ── 16 MiB per-sentence stream cap ───────────────────────────────────────


def test_stream_cap_truncates_runaway_upstream(monkeypatch):
    monkeypatch.setattr(ts, "_STREAM_SENTENCE_BYTE_CAP", 100)

    def _endless():
        while True:
            yield b"\x00" * 64

    out = list(ts._capped(_endless(), "test"))
    assert len(out) == 1  # 64 ok, 128 > cap → stop
    assert sum(len(c) for c in out) <= 100


# ── Sync fallback: one-ahead synthesis/playback pipeline ─────────────────
#
# The universal per-sentence sync path pipelines synthesis with playback:
# while sentence n plays, sentence n+1 is already synthesizing. For local
# model providers (RTF near 1) the serial path spent as long silent between
# sentences as speaking; these pin the overlap, ordering, stop, failure
# isolation, and temp-file hygiene of the pipelined path.


def _timed_sync_run(monkeypatch, sentences, *, synth_s=0.12, play_s=0.12,
                    synth_fail_on=None, stop_after_plays=None):
    """Drive stream_tts_to_speaker over the sync path with timed fakes.

    Returns (events, stop, done): events is [(kind, sentence, t_start, t_end)]
    with kinds "synth"/"play", timestamps from a shared monotonic origin.
    """
    from tools import tts_tool

    origin = time.monotonic()
    events = []
    lock = threading.Lock()
    stop, done = threading.Event(), threading.Event()

    def fake_synth(text, output_path):
        t0 = time.monotonic() - origin
        if synth_fail_on and synth_fail_on in text:
            raise RuntimeError("synth exploded")
        time.sleep(synth_s)
        with open(output_path, "wb") as fh:
            fh.write(b"x" * 100)
        with lock:
            events.append(("synth", text, t0, time.monotonic() - origin))

    def fake_play(path):
        t0 = time.monotonic() - origin
        time.sleep(play_s)
        with lock:
            events.append(("play", path, t0, time.monotonic() - origin))
            plays = sum(1 for e in events if e[0] == "play")
        if stop_after_plays is not None and plays >= stop_after_plays:
            stop.set()

    monkeypatch.setattr(tts_tool, "text_to_speech_tool", fake_synth)
    fake_vm = MagicMock()
    fake_vm.play_audio_file.side_effect = fake_play
    monkeypatch.setitem(__import__("sys").modules, "tools.voice_mode", fake_vm)

    q = _drain_queue(sentences)
    with patch("tools.tts_streaming.resolve_streaming_provider", return_value=None):
        tts_tool.stream_tts_to_speaker(q, stop, done)
    return events, stop, done


def test_sync_pipeline_overlaps_synthesis_with_playback(monkeypatch):
    sentences = ["First full sentence here. ", "Second full sentence here. ",
                 "Third full sentence here. "]
    events, _stop, done = _timed_sync_run(monkeypatch, sentences)

    synths = [e for e in events if e[0] == "synth"]
    plays = [e for e in events if e[0] == "play"]
    assert len(synths) == 3 and len(plays) == 3
    assert done.is_set()

    # The point of the pipeline: sentence 2's synthesis STARTS before
    # sentence 1's playback ENDS (serial code could never do this).
    synth2_start = synths[1][2]
    play1_end = plays[0][3]
    assert synth2_start < play1_end, (
        f"no overlap: synth2 started at {synth2_start:.3f}, "
        f"play1 ended at {play1_end:.3f}"
    )


def test_sync_pipeline_preserves_order_and_isolates_failures(monkeypatch):
    sentences = ["Alpha sentence spoken first. ", "Bravo sentence explodes here. ",
                 "Charlie sentence still plays. "]
    events, _stop, done = _timed_sync_run(monkeypatch, sentences,
                                          synth_fail_on="Bravo")

    synths = [e[1] for e in events if e[0] == "synth"]
    plays = [e for e in events if e[0] == "play"]
    # Bravo's synth raised: never synthesized-to-file, never played — but
    # Alpha and Charlie both played, in submission order.
    assert [s.split()[0] for s in synths] == ["Alpha", "Charlie"]
    assert len(plays) == 2
    assert done.is_set()


def test_sync_pipeline_stop_skips_queued_playback(monkeypatch):
    sentences = ["First full sentence here. ", "Second full sentence here. ",
                 "Third full sentence here. ", "Fourth full sentence here. "]
    events, stop, done = _timed_sync_run(monkeypatch, sentences,
                                         stop_after_plays=1)

    plays = [e for e in events if e[0] == "play"]
    assert len(plays) == 1, f"stop after first play must skip the rest, got {len(plays)}"
    assert stop.is_set() and done.is_set()


def test_sync_pipeline_cleans_temp_files(monkeypatch):
    from tools import tts_tool

    created = []
    real_mkstemp = tempfile.mkstemp

    def tracking_mkstemp(*a, **k):
        fd, path = real_mkstemp(*a, **k)
        created.append(path)
        return fd, path

    monkeypatch.setattr(tts_tool.tempfile, "mkstemp", tracking_mkstemp)
    events, _stop, done = _timed_sync_run(monkeypatch,
                                          ["First full sentence here. ",
                                           "Second full sentence here. "])
    assert len([e for e in events if e[0] == "play"]) == 2
    assert created, "expected temp files to be created via mkstemp"
    leftovers = [p for p in created if os.path.exists(p)]
    assert not leftovers, f"temp files not cleaned: {leftovers}"
