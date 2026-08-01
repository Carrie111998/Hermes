"""Tests for the provider-agnostic streaming TTS backend (tools.tts_streaming)
and its dispatch through tools.tts_tool.stream_tts_to_speaker.

No live audio or network: the ElevenLabs/OpenAI SDKs, sounddevice, and the sync
synth path are all mocked. Covers the registry/resolver, provider availability,
the chunked-streamer playback path, and the universal per-sentence sync fallback.
"""

import queue
import struct
import threading
from unittest.mock import MagicMock, patch

import pytest

import tools.tts_streaming as ts

pytest.importorskip("numpy")


# ── Provider-neutral PCM framing ────────────────────────────────────────


def test_audio_framer_handles_arbitrary_chunk_boundaries_and_monotonic_timing():
    audio_format = ts.AudioFormat(sample_rate=1000)
    framer = ts.AudioFramer(audio_format)
    pcm = b"".join(struct.pack("<h", sample) for sample in range(47))

    frames = []
    for chunk in (pcm[:1], pcm[1:7], pcm[7:17], pcm[17:58], pcm[58:]):
        frames.extend(framer.feed(chunk))
    frames.extend(framer.flush())

    assert [frame.seq for frame in frames] == [0, 1, 2]
    assert [frame.start_sample for frame in frames] == [0, 20, 40]
    assert [frame.sample_count for frame in frames] == [20, 20, 7]
    assert b"".join(frame.pcm for frame in frames) == pcm
    assert all(frame.format == audio_format for frame in frames)


def test_audio_framer_rejects_invalid_encoding_and_sample_alignment():
    with pytest.raises(ValueError, match="pcm_s16le"):
        ts.AudioFormat(sample_rate=24000, encoding="audio/wav")
    with pytest.raises(ValueError, match="mono"):
        ts.AudioFormat(sample_rate=24000, channels=2)

    framer = ts.AudioFramer(ts.AudioFormat(sample_rate=1000))
    assert framer.feed(b"\x00") == []
    with pytest.raises(ValueError, match="partial sample"):
        framer.flush()


def test_provider_stream_frames_preserves_legacy_stream_and_flushes_partial():
    class _Provider(ts.StreamingTTSProvider):
        sample_rate = 1000

        @staticmethod
        def available():
            return True

        def stream(self, text):
            yield b"\x01\x00" * 20
            yield b"\x02\x00" * 3

    provider = _Provider({}, {})
    assert list(provider.stream("hello")) == [b"\x01\x00" * 20, b"\x02\x00" * 3]
    frames = list(provider.stream_frames("hello"))
    assert [(frame.seq, frame.start_sample, frame.sample_count) for frame in frames] == [
        (0, 0, 20),
        (1, 20, 3),
    ]


def test_finite_fish_wav_header_can_arrive_split_across_http_chunks():
    header = bytearray(44)
    header[:4] = b"RIFF"
    header[8:12] = b"WAVE"
    header[12:16] = b"fmt "
    struct.pack_into("<I", header, 16, 16)
    struct.pack_into("<HHIIHH", header, 20, 1, 1, 24000, 48000, 2, 16)
    header[36:40] = b"data"
    struct.pack_into("<I", header, 40, 6)
    pcm = b"\x01\x00\x02\x00\x03\x00"

    chunks = [bytes(header[:3]), bytes(header[3:17]), bytes(header[17:44]) + pcm[:1], pcm[1:]]
    assert b"".join(ts._streaming_wav_pcm(iter(chunks))) == pcm


def test_finite_fish_rejects_incompatible_wav_format():
    header = bytearray(44)
    header[:4] = b"RIFF"
    header[8:12] = b"WAVE"
    header[12:16] = b"fmt "
    struct.pack_into("<I", header, 16, 16)
    struct.pack_into("<HHIIHH", header, 20, 1, 2, 24000, 96000, 4, 16)
    header[36:40] = b"data"
    struct.pack_into("<I", header, 40, 0)
    with pytest.raises(RuntimeError, match="incompatible"):
        list(ts._streaming_wav_pcm(iter([bytes(header)])))


def test_finite_fish_cancel_closes_the_active_http_response():
    response = MagicMock()
    streamer = ts.FiniteFishStreamer({}, {})
    streamer._active_response = response

    streamer.cancel()

    response.close.assert_called_once_with()


def test_streaming_provider_cancellation_capability_is_explicit():
    for provider_name, provider_class in ts._REGISTRY.items():
        capability = provider_class.upstream_cancellable
        assert isinstance(capability, bool), provider_name


def test_openai_cancel_latch_closes_response_assigned_after_cancel(monkeypatch):
    import openai

    create_started = threading.Event()
    release_create = threading.Event()
    response_closed = threading.Event()

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def close(self):
            response_closed.set()

        def iter_bytes(self):
            yield b"\x00\x00"

    response = _Response()

    class _Create:
        def create(self, **_kwargs):
            create_started.set()
            release_create.wait(timeout=2)
            return response

    class _OpenAI:
        def __init__(self, **_kwargs):
            self.audio = MagicMock()
            self.audio.speech.with_streaming_response = _Create()

    monkeypatch.setattr(openai, "OpenAI", _OpenAI)
    streamer = ts.OpenAIStreamer({}, {})
    errors = []

    def _consume():
        try:
            list(streamer.stream("cancel race"))
        except Exception as exc:  # pragma: no cover - defensive
            errors.append(exc)

    worker = threading.Thread(target=_consume)
    worker.start()
    assert create_started.wait(timeout=2)
    streamer.cancel()
    release_create.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert not errors
    response_closed.wait(timeout=1)
    assert response_closed.is_set()
    # The latch is idempotent and does not close the already-detached handle twice.
    streamer.cancel()


def test_openai_cancel_closes_active_response_cross_thread(monkeypatch):
    import openai

    response_started = threading.Event()
    release_iter = threading.Event()
    response_closed = threading.Event()

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def close(self):
            response_closed.set()
            release_iter.set()

        def iter_bytes(self):
            response_started.set()
            release_iter.wait(timeout=2)
            return iter(())

    class _Create:
        @staticmethod
        def create(**_kwargs):
            return _Response()

    class _OpenAI:
        def __init__(self, **_kwargs):
            self.audio = MagicMock()
            self.audio.speech.with_streaming_response = _Create()

    monkeypatch.setattr(openai, "OpenAI", _OpenAI)
    streamer = ts.OpenAIStreamer({}, {})
    worker = threading.Thread(target=lambda: list(streamer.stream("active cancel")))
    worker.start()
    assert response_started.wait(timeout=2)
    streamer.cancel()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert response_closed.is_set()


def test_gemini_cancel_closes_active_response_cross_thread(monkeypatch):
    import requests

    response_started = threading.Event()
    release_iter = threading.Event()
    response_closed = threading.Event()

    class _Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            return None

        def close(self):
            response_closed.set()
            release_iter.set()

        def iter_lines(self, **_kwargs):
            response_started.set()
            release_iter.wait(timeout=2)
            return iter(())

    response = _Response()
    monkeypatch.setattr(requests, "post", lambda *_args, **_kwargs: response)
    monkeypatch.setattr(ts, "_resolve_key", lambda *_args: "gemini-key")

    streamer = ts.GeminiStreamer({}, {})
    errors = []

    def _consume():
        try:
            list(streamer.stream("cancel active"))
        except Exception as exc:  # pragma: no cover - defensive
            errors.append(exc)

    worker = threading.Thread(target=_consume)
    worker.start()
    assert response_started.wait(timeout=2)
    streamer.cancel()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert not errors
    assert response_closed.is_set()


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
