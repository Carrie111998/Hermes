"""Tests for the provider-agnostic streaming TTS backend (tools.tts_streaming)
and its dispatch through tools.tts_tool.stream_tts_to_speaker.

No live audio or network: the ElevenLabs/OpenAI SDKs, sounddevice, and the sync
synth path are all mocked. Covers the registry/resolver, provider availability,
the chunked-streamer playback path, and the universal per-sentence sync fallback.
"""

import queue
import threading
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
        assert c.feed("<THINK>secret reason.") == []
        assert c.feed(" More.</ThInK>The actual spoken answer. ") == [
            "The actual spoken answer. "
        ]

    def test_partial_think_opener_is_held_across_deltas(self):
        c = ts.SentenceChunker()
        assert c.feed("This visible answer is long enough. <thi") == []
        assert c.holding_protected_text is True
        out = c.feed("nk>secret reason.</think>Another visible answer follows. ")
        assert "secret" not in "".join(out)
        assert "This visible answer is long enough." in "".join(out)
        assert c.holding_protected_text is False

    def test_fenced_code_is_held_across_deltas_and_not_used_as_boundary(self):
        c = ts.SentenceChunker()
        assert c.feed("```python\nsecret = 'do not speak'.") == []
        assert c.feed("\n```The actual spoken answer. ") == [
            "```python\nsecret = 'do not speak'.\n```The actual spoken answer. "
        ]

    def test_partial_fence_opener_is_held_across_deltas(self):
        c = ts.SentenceChunker()
        assert c.feed("This visible answer is long enough. ``") == []
        assert c.holding_protected_text is True
        out = c.feed("`python\nsecret.\n```Another visible answer follows. ")
        assert "secret" in "".join(out)  # retained for display; stripped before speech
        assert c.holding_protected_text is False

    def test_verifier_footer_and_later_deltas_are_never_spoken(self):
        c = ts.SentenceChunker()
        assert c.feed(
            "The public answer is ready. ⚠️ File-mutation verifier: hidden. "
        ) == ["The public answer is ready. "]
        assert c.feed("More private verifier instructions. ") == []
        assert c.flush() == []

    def test_verifier_literal_inside_fenced_code_does_not_discard_visible_tail(self):
        c = ts.SentenceChunker()
        raw = (
            "```text\n"
            "⚠️ File-mutation verifier: literal documentation sample.\n"
            "```The actual visible answer follows."
        )
        assert c.feed(raw) == []
        assert c.flush() == [raw]

    @pytest.mark.parametrize(
        "partial",
        ["⚠️ File-mut", "⚠ File-mut", "File-muta", "Fıle-muta", "Fİle-muta"],
    )
    def test_partial_verifier_header_is_held_across_deltas(self, partial):
        c = ts.SentenceChunker()
        assert c.feed(f"This visible answer is long enough.\n{partial}") == []
        assert c.holding_protected_text is True
        continuation = (
            "ation verifier: hidden. More hidden details. "
            if partial.endswith("mut")
            else "tion verifier: hidden. More hidden details. "
        )
        out = c.feed(continuation)
        assert [item.strip() for item in out] == ["This visible answer is long enough."]
        assert c.holding_protected_text is False

    def test_pronunciation_phrase_can_span_sentence_boundary_and_unicode_case_delta(self):
        c = ts.SentenceChunker(
            pronunciation_substitutions={"Dr. Ipek": "Doctor Ipek"},
        )
        assert c.feed("Please book an appointment with Dr. ") == []
        assert c.holding_pronunciation_lookahead is True
        assert c.feed("ıpek tomorrow. ") == [
            "Please book an appointment with Dr. ıpek tomorrow. "
        ]
        assert c.holding_pronunciation_lookahead is False

    def test_pronunciation_tail_holds_until_right_word_boundary_is_known(self):
        c = ts.SentenceChunker(
            pronunciation_substitutions={"Dr. Ipek": "Doctor Ipek"},
        )
        prefix = "Visible ordinary words " * 6
        assert c.feed(prefix + "Dr. Ipek") == []
        assert c.holding_pronunciation_lookahead is True

        output = c.feed("son arrives. ")
        assert "".join(output + c.flush()) == prefix + "Dr. Ipekson arrives."
        assert c.holding_pronunciation_lookahead is False

    def test_idle_flush_retains_left_word_boundary_context(self):
        c = ts.SentenceChunker(
            pronunciation_substitutions={"Ipek": "EE-peck"},
        )
        prefix = "Visible ordinary words " * 6
        assert c.feed(prefix + "my") == []
        ready = c.flush_for_idle()
        assert "".join(ready) + c.buf == prefix + "my"
        assert c.buf == "my"

        output = c.feed("Ipek arrives. ")
        assert "".join(ready + output + c.flush()) == prefix + "myIpek arrives."

    def test_idle_flush_retains_punctuation_source_before_trailing_word(self):
        c = ts.SentenceChunker(
            pronunciation_substitutions={"C++": "C plus plus"},
        )
        prefix = "Visible ordinary words " * 6
        assert c.feed(prefix + "C++s") == []
        ready = c.flush_for_idle()
        assert "".join(ready) + c.buf == prefix + "C++s"
        assert c.buf == "C++s"

        output = c.feed("on concludes. ")
        assert "".join(ready + output + c.flush()) == prefix + "C++son concludes."

    def test_unicode_ignorecase_partial_think_opener_is_held(self):
        c = ts.SentenceChunker()
        prefix = "Visible ordinary words " * 6
        assert c.feed(prefix + "<thı") == []
        assert c.holding_protected_text is True
        assert c.feed("nk>SECRET reasoning.</thınk>Final visible answer. ") == [
            prefix + "Final visible answer. "
        ]
        assert c.holding_protected_text is False


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


_IDLE = object()


class _IdleOnceQueue:
    """Queue stub that inserts one producer-idle timeout between deltas."""

    def __init__(self, items):
        self._items = list(items)

    def get(self, timeout=None):
        if not self._items:
            raise queue.Empty
        item = self._items.pop(0)
        if item is _IDLE:
            raise queue.Empty
        return item

    def get_nowait(self):
        if not self._items:
            raise queue.Empty
        return self._items.pop(0)


def _sd_mock():
    sd = MagicMock()
    out = MagicMock()
    sd.OutputStream.return_value = out
    return sd, out


def test_streamer_path_applies_symbol_bearing_pronunciation_before_normalization(monkeypatch):
    from tools import tts_tool

    seen = []
    displayed = []

    class _Fake(ts.StreamingTTSProvider):
        sample_rate = 24000
        channels = 1

        @staticmethod
        def available():
            return True

        def stream(self, text):
            seen.append(text)
            yield b"\x01\x00" * 10

    sd, _ = _sd_mock()
    raw_reply = (
        "```text\n"
        "⚠️ File-mutation verifier: literal documentation sample.\n"
        "<think>literal unclosed documentation example.\n"
        "```Our R&D team shipped."
    )
    q = _drain_queue([raw_reply])
    stop, done = threading.Event(), threading.Event()
    config = {
        "provider": "openai",
        "pronunciation": {
            "substitutions": {
                "R&D": "Research and Development",
                "Research and Development": "WRONG",
            },
        },
    }

    with (
        patch.object(tts_tool, "_load_tts_config", return_value=config),
        patch(
            "tools.tts_streaming.resolve_streaming_provider",
            return_value=_Fake({}, {}),
        ),
        patch.object(tts_tool, "_import_sounddevice", return_value=sd),
        patch.object(tts_tool.platform, "system", return_value="Linux"),
    ):
        tts_tool.stream_tts_to_speaker(
            q,
            stop,
            done,
            display_callback=displayed.append,
        )

    assert seen == ["Our Research and Development team shipped."]
    assert displayed == [raw_reply]
    assert done.is_set()


def test_streamer_pronunciation_collision_does_not_suppress_raw_display(monkeypatch):
    from tools import tts_tool

    spoken = []
    displayed = []

    class _Fake(ts.StreamingTTSProvider):
        sample_rate = 24000
        channels = 1

        @staticmethod
        def available():
            return True

        def stream(self, text):
            spoken.append(text)
            yield b"\x01\x00" * 10

    sd, _ = _sd_mock()
    q = _drain_queue(["Alpha is definitely here. Beta is definitely here."])
    stop, done = threading.Event(), threading.Event()
    config = {
        "provider": "openai",
        "pronunciation": {
            "substitutions": {"Alpha": "Same", "Beta": "Same"},
        },
    }

    with (
        patch.object(tts_tool, "_load_tts_config", return_value=config),
        patch(
            "tools.tts_streaming.resolve_streaming_provider",
            return_value=_Fake({}, {}),
        ),
        patch.object(tts_tool, "_import_sounddevice", return_value=sd),
        patch.object(tts_tool.platform, "system", return_value="Linux"),
    ):
        tts_tool.stream_tts_to_speaker(
            q,
            stop,
            done,
            display_callback=displayed.append,
        )

    assert spoken == ["Same is definitely here."]
    assert displayed == ["Alpha is definitely here. ", "Beta is definitely here."]
    assert done.is_set()


# ── Dispatch: universal per-sentence sync fallback ───────────────────────


def test_sync_fallback_preserves_symbols_for_single_pronunciation_pass(monkeypatch):
    from tools import tts_tool

    config = {
        "provider": "openai",
        "openai": {},
        "pronunciation": {
            "substitutions": {
                "R&D": "Research and Development",
                "Research and Development": "WRONG",
            },
        },
    }
    generated = MagicMock()
    q = _drain_queue(["Our R&D team shipped."])
    stop, done = threading.Event(), threading.Event()

    with (
        patch.object(tts_tool, "_load_tts_config", return_value=config),
        patch.object(tts_tool, "_get_provider", return_value="openai"),
        patch.object(tts_tool, "_resolve_command_provider_config", return_value=None),
        patch.object(tts_tool, "_resolve_max_text_length", return_value=4096),
        patch.object(tts_tool, "_generate_openai_tts", generated),
        patch("tools.tts_streaming.resolve_streaming_provider", return_value=None),
        patch("gateway.session_context.get_session_env", return_value=""),
    ):
        tts_tool.stream_tts_to_speaker(q, stop, done)

    assert generated.call_args[0][0] == "Our Research and Development team shipped."
    assert done.is_set()


def test_sync_fallback_keeps_unicode_ignorecase_pronunciation_across_queue_deltas(monkeypatch):
    from tools import tts_tool

    config = {
        "provider": "openai",
        "openai": {},
        "pronunciation": {
            "substitutions": {"Dr. Ipek": "Doctor Ipek"},
        },
    }
    generated = MagicMock()
    q = _drain_queue([
        "Please book an appointment with Dr. ",
        "ıpek tomorrow.",
    ])
    stop, done = threading.Event(), threading.Event()

    with (
        patch.object(tts_tool, "_load_tts_config", return_value=config),
        patch.object(tts_tool, "_get_provider", return_value="openai"),
        patch.object(tts_tool, "_resolve_command_provider_config", return_value=None),
        patch.object(tts_tool, "_resolve_max_text_length", return_value=4096),
        patch.object(tts_tool, "_generate_openai_tts", generated),
        patch("tools.tts_streaming.resolve_streaming_provider", return_value=None),
        patch("gateway.session_context.get_session_env", return_value=""),
    ):
        tts_tool.stream_tts_to_speaker(q, stop, done)

    assert generated.call_args[0][0] == (
        "Please book an appointment with Doctor Ipek tomorrow."
    )
    assert done.is_set()


def test_sync_fallback_does_not_idle_flush_incomplete_pronunciation(monkeypatch):
    from tools import tts_tool

    config = {
        "provider": "openai",
        "openai": {},
        "pronunciation": {
            "substitutions": {"Dr. Smith": "Doctor Smith"},
        },
    }
    generated = MagicMock()
    prefix = "Please book " + ("a very important appointment " * 5) + "with Dr. "
    q = _IdleOnceQueue([prefix, _IDLE, "Smith tomorrow.", None])
    stop, done = threading.Event(), threading.Event()

    with (
        patch.object(tts_tool, "_load_tts_config", return_value=config),
        patch.object(tts_tool, "_get_provider", return_value="openai"),
        patch.object(tts_tool, "_resolve_command_provider_config", return_value=None),
        patch.object(tts_tool, "_resolve_max_text_length", return_value=4096),
        patch.object(tts_tool, "_generate_openai_tts", generated),
        patch("tools.tts_streaming.resolve_streaming_provider", return_value=None),
        patch("gateway.session_context.get_session_env", return_value=""),
    ):
        tts_tool.stream_tts_to_speaker(q, stop, done)

    generated.assert_called_once()
    assert generated.call_args[0][0].endswith("with Doctor Smith tomorrow.")
    assert done.is_set()


def test_sync_idle_waits_for_pronunciation_right_word_boundary(monkeypatch):
    from tools import tts_tool

    config = {
        "provider": "openai",
        "openai": {},
        "pronunciation": {
            "substitutions": {"Dr. Ipek": "Doctor Ipek"},
        },
    }
    generated = MagicMock()
    prefix = ("Visible ordinary words " * 6) + "Dr. Ipek"
    q = _IdleOnceQueue([prefix, _IDLE, "son arrives.", None])
    stop, done = threading.Event(), threading.Event()

    with (
        patch.object(tts_tool, "_load_tts_config", return_value=config),
        patch.object(tts_tool, "_get_provider", return_value="openai"),
        patch.object(tts_tool, "_resolve_command_provider_config", return_value=None),
        patch.object(tts_tool, "_resolve_max_text_length", return_value=4096),
        patch.object(tts_tool, "_generate_openai_tts", generated),
        patch("tools.tts_streaming.resolve_streaming_provider", return_value=None),
        patch("gateway.session_context.get_session_env", return_value=""),
    ):
        tts_tool.stream_tts_to_speaker(q, stop, done)

    spoken = " ".join(call.args[0] for call in generated.call_args_list)
    assert "Doctor Ipek" not in spoken
    assert "Dr. Ipekson arrives." in spoken
    assert done.is_set()


def test_sync_idle_preserves_pronunciation_left_word_boundary(monkeypatch):
    from tools import tts_tool

    config = {
        "provider": "openai",
        "openai": {},
        "pronunciation": {
            "substitutions": {"Ipek": "EE-peck"},
        },
    }
    generated = MagicMock()
    prefix = ("Visible ordinary words " * 6) + "my"
    q = _IdleOnceQueue([prefix, _IDLE, "Ipek arrives.", None])
    stop, done = threading.Event(), threading.Event()

    with (
        patch.object(tts_tool, "_load_tts_config", return_value=config),
        patch.object(tts_tool, "_get_provider", return_value="openai"),
        patch.object(tts_tool, "_resolve_command_provider_config", return_value=None),
        patch.object(tts_tool, "_resolve_max_text_length", return_value=4096),
        patch.object(tts_tool, "_generate_openai_tts", generated),
        patch("tools.tts_streaming.resolve_streaming_provider", return_value=None),
        patch("gateway.session_context.get_session_env", return_value=""),
    ):
        tts_tool.stream_tts_to_speaker(q, stop, done)

    spoken = " ".join(call.args[0] for call in generated.call_args_list)
    assert "EE-peck" not in spoken
    assert "myIpek arrives." in spoken
    assert done.is_set()


def test_sync_idle_preserves_punctuation_source_right_word_boundary(monkeypatch):
    from tools import tts_tool

    config = {
        "provider": "openai",
        "openai": {},
        "pronunciation": {
            "substitutions": {"C++": "C plus plus"},
        },
    }
    generated = MagicMock()
    prefix = ("Visible ordinary words " * 6) + "C++s"
    q = _IdleOnceQueue([prefix, _IDLE, "on concludes.", None])
    stop, done = threading.Event(), threading.Event()

    with (
        patch.object(tts_tool, "_load_tts_config", return_value=config),
        patch.object(tts_tool, "_get_provider", return_value="openai"),
        patch.object(tts_tool, "_resolve_command_provider_config", return_value=None),
        patch.object(tts_tool, "_resolve_max_text_length", return_value=4096),
        patch.object(tts_tool, "_generate_openai_tts", generated),
        patch("tools.tts_streaming.resolve_streaming_provider", return_value=None),
        patch("gateway.session_context.get_session_env", return_value=""),
    ):
        tts_tool.stream_tts_to_speaker(q, stop, done)

    spoken = " ".join(call.args[0] for call in generated.call_args_list)
    assert "C plus plus" not in spoken
    assert "C++son concludes." in spoken
    assert done.is_set()


@pytest.mark.parametrize(
    ("partial", "continuation", "expected_tail"),
    [
        ("<thi", "nk>SECRET reasoning.</think>Final visible answer.", "Final visible answer."),
        ("<thı", "nk>SECRET reasoning.</thınk>Final visible answer.", "Final visible answer."),
        ("``", "`SECRET code.\n```Final visible answer.", "Final visible answer."),
        ("File-muta", "tion verifier: SECRET verifier.", "Visible ordinary words"),
        ("Fıle-muta", "tion verifier: SECRET verifier.", "Visible ordinary words"),
    ],
)
def test_sync_fallback_does_not_idle_flush_partial_protected_opener(
    monkeypatch,
    partial,
    continuation,
    expected_tail,
):
    from tools import tts_tool

    config = {"provider": "openai", "openai": {}}
    generated = MagicMock()
    prefix = ("Visible ordinary words " * 6) + partial
    q = _IdleOnceQueue([
        prefix,
        _IDLE,
        continuation,
        None,
    ])
    stop, done = threading.Event(), threading.Event()

    with (
        patch.object(tts_tool, "_load_tts_config", return_value=config),
        patch.object(tts_tool, "_get_provider", return_value="openai"),
        patch.object(tts_tool, "_resolve_command_provider_config", return_value=None),
        patch.object(tts_tool, "_resolve_max_text_length", return_value=4096),
        patch.object(tts_tool, "_generate_openai_tts", generated),
        patch("tools.tts_streaming.resolve_streaming_provider", return_value=None),
        patch("gateway.session_context.get_session_env", return_value=""),
    ):
        tts_tool.stream_tts_to_speaker(q, stop, done)

    generated.assert_called_once()
    spoken = generated.call_args[0][0]
    assert "SECRET" not in spoken
    assert expected_tail in spoken
    assert done.is_set()


def test_true_streamer_uses_resolved_streamer_provider_cap(monkeypatch):
    from tools import tts_tool

    spoken = []

    class _OpenAIStreamer(ts.StreamingTTSProvider):
        provider_name = "openai"

        @staticmethod
        def available():
            return True

        def stream(self, text):
            spoken.append(text)
            yield b"\x01\x00" * 10

    sd, _ = _sd_mock()
    config = {
        "provider": "edge",
        "streaming": {"provider": "openai"},
    }
    q = _drain_queue([("x" * 5000) + "."])
    stop, done = threading.Event(), threading.Event()

    with (
        patch.object(tts_tool, "_load_tts_config", return_value=config),
        patch.object(
            tts_tool,
            "_resolve_max_text_length",
            side_effect=lambda provider, _cfg: 4096 if provider == "openai" else 5000,
        ),
        patch(
            "tools.tts_streaming.resolve_streaming_provider",
            return_value=_OpenAIStreamer(config, {}),
        ),
        patch.object(tts_tool, "_import_sounddevice", return_value=sd),
        patch.object(tts_tool.platform, "system", return_value="Linux"),
    ):
        tts_tool.stream_tts_to_speaker(q, stop, done)

    assert len(spoken) == 1
    assert len(spoken[0]) == 4096
    assert done.is_set()


def test_true_elevenlabs_streamer_uses_streaming_model_cap():
    from tools import tts_tool

    section = {
        "model_id": "eleven_flash_v2_5",
        "streaming_model_id": "eleven_v3",
    }
    assert ts.ElevenLabsStreamer({}, section).model_id == "eleven_v3"

    spoken = []

    class _ElevenLabsStreamer(ts.StreamingTTSProvider):
        provider_name = "elevenlabs"
        model_id = "eleven_v3"

        @staticmethod
        def available():
            return True

        def stream(self, text):
            spoken.append(text)
            yield b"\x01\x00" * 10

    sd, _ = _sd_mock()
    config = {
        "provider": "edge",
        "streaming": {"provider": "elevenlabs"},
        "elevenlabs": {
            "model_id": "eleven_flash_v2_5",
            "streaming_model_id": "eleven_v3",
        },
    }
    q = _drain_queue([("x" * 6000) + "."])
    stop, done = threading.Event(), threading.Event()

    with (
        patch.object(tts_tool, "_load_tts_config", return_value=config),
        patch(
            "tools.tts_streaming.resolve_streaming_provider",
            return_value=_ElevenLabsStreamer(config, config["elevenlabs"]),
        ),
        patch.object(tts_tool, "_import_sounddevice", return_value=sd),
        patch.object(tts_tool.platform, "system", return_value="Linux"),
    ):
        tts_tool.stream_tts_to_speaker(q, stop, done)

    assert len(spoken) == 1
    assert len(spoken[0]) == 5000
    assert done.is_set()


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
