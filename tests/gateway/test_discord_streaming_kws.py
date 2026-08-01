"""Deterministic tests for playback-scoped Discord streaming KWS."""
from __future__ import annotations

import threading
import time
import types

import pytest

from plugins.platforms.discord.streaming_kws import (
    DiscordStreamingKwsManager,
    StreamingKwsConfig,
    _build_engine,
    _normalize,
)


class _FakeEngine:
    def __init__(self, _config, _phrases, *, fire_on=1, block=None):
        self.fire_on = fire_on
        self.block = block
        self.closed = False

    def create_stream(self):
        return {"frames": 0}

    def process(self, stream, _pcm):
        if self.block is not None:
            self.block.wait(timeout=2)
        stream["frames"] += 1
        return 0 if stream["frames"] >= self.fire_on else None

    def close(self):
        self.closed = True


def test_config_is_fail_closed_and_clamped():
    default = StreamingKwsConfig.from_mapping({})
    assert default.enabled is False
    assert default.shadow_only is True
    assert default.provider == "faster_whisper"

    configured = StreamingKwsConfig.from_mapping(
        {
            "enabled": "yes",
            "shadow_only": "false",
            "provider": " Whisper ",
            "model_dir": " ~/models/ko ",
            "hotword_bias": "yes",
            "contrast_wake_names": [" 유나야 ", "미나야", "유나야"],
            "num_threads": 0,
            "queue_frames": 2,
        }
    )
    assert configured.enabled is True
    assert configured.shadow_only is False
    assert configured.provider == "whisper"
    assert configured.model_dir == "~/models/ko"
    assert configured.hotword_bias is True
    assert configured.contrast_wake_names == ("유나야", "미나야")
    assert configured.num_threads == 1
    assert configured.queue_frames == 32

    invalid = StreamingKwsConfig.from_mapping(
        {
            "window_ms": "bad",
            "stride_ms": None,
            "min_audio_ms": object(),
            "num_threads": "bad",
            "queue_frames": "bad",
            "contrast_wake_names": 123,
        }
    )
    assert invalid.window_ms == 1600
    assert invalid.stride_ms == 320
    assert invalid.min_audio_ms == 640
    assert invalid.num_threads == 4
    assert invalid.queue_frames == 256
    assert invalid.contrast_wake_names == ()

    single_name = StreamingKwsConfig.from_mapping(
        {"contrast_wake_names": "유나야"}
    )
    assert single_name.contrast_wake_names == ("유나야",)


def test_normalization_is_exact_except_for_known_korean_contraction():
    assert _normalize(" 하나야, 잠깐! ") == "하나야잠깐"
    assert _normalize("하나야 멈추어") == "하나야멈춰"


def test_unknown_provider_fails_closed_before_loading_model():
    with pytest.raises(ValueError, match="Unsupported"):
        _build_engine(
            StreamingKwsConfig(provider="unknown"),
            ("하나야 잠깐",),
        )


def test_faster_whisper_engine_downsamples_and_detects_rolling_window(
    monkeypatch,
):
    import sys

    pytest.importorskip("numpy")
    from plugins.platforms.discord.streaming_kws import FasterWhisperRollingEngine

    calls = []
    transcript = ["하나야 잠깐"]

    class FakeWhisperModel:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

        def transcribe(self, audio, **kwargs):
            calls.append((len(audio), kwargs))
            return iter([types.SimpleNamespace(text=transcript[0])]), object()

    fake_module = types.ModuleType("faster_whisper")
    fake_module.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *args, **kwargs: None)
    engine = FasterWhisperRollingEngine(
        StreamingKwsConfig(
            enabled=True,
            provider="faster_whisper",
            model="base",
            window_ms=1600,
            stride_ms=400,
            min_audio_ms=800,
            hotword_bias=True,
            contrast_wake_names=("유나야", "미나야"),
        ),
        ("하나야 잠깐", "하나야 멈춰"),
    )
    stream = engine.create_stream()
    assert engine.process(stream, b"") is None
    assert engine.flush(stream) is None
    # 20 ms of 48 kHz stereo int16 per frame; 40 frames = 800 ms.
    frame = b"\x10\x00\xf0\xff" * 960
    detected = None
    for _ in range(40):
        detected = engine.process(stream, frame)
    assert detected == 0
    assert calls[0][1]["compute_type"] == "int8"
    assert calls[-1][0] == 12800
    assert calls[-1][1]["language"] == "ko"
    assert calls[-1][1]["beam_size"] == 1
    assert "하나야 잠깐" in calls[-1][1]["hotwords"]
    assert "유나야 잠깐" in calls[-1][1]["hotwords"]
    assert "미나야 멈춰" in calls[-1][1]["hotwords"]

    transcript[0] = "유나야 잠깐"
    negative_stream = engine.create_stream()
    negative = None
    for _ in range(40):
        negative = engine.process(negative_stream, frame)
    assert negative is None
    assert engine.flush(negative_stream) is None

    transcript[0] = "정하나야 잠깐"
    prefixed_name_stream = engine.create_stream()
    prefixed_match = None
    for _ in range(40):
        prefixed_match = engine.process(prefixed_name_stream, frame)
    assert prefixed_match is None
    engine.close()

    with pytest.raises(RuntimeError, match="at least one phrase"):
        FasterWhisperRollingEngine(
            StreamingKwsConfig(enabled=True),
            (),
        )


def test_manager_fires_once_per_playback_and_resets_for_next_token():
    events = []
    fired = threading.Event()

    def callback(event):
        events.append(event)
        fired.set()

    engine = _FakeEngine(None, None, fire_on=2)
    manager = DiscordStreamingKwsManager(
        StreamingKwsConfig(enabled=True, queue_frames=32),
        ("하나야 잠깐",),
        callback,
        engine_factory=lambda *_args: engine,
    )
    pcm = b"\x00" * 3840
    try:
        assert manager.begin_playback(1, 10)
        assert manager.offer_pcm(1, 10, 42, pcm, received_at=time.monotonic())
        assert manager.offer_pcm(1, 10, 42, pcm, received_at=time.monotonic())
        assert fired.wait(timeout=1)
        for _ in range(5):
            manager.offer_pcm(1, 10, 42, pcm)
        time.sleep(0.05)
        assert len(events) == 1
        assert events[0]["token"] == 10
        assert events[0]["user_id"] == 42
        assert events[0]["keyword_index"] == 0
        assert events[0]["audio_ms"] == 40

        fired.clear()
        assert manager.end_playback(1, 10)
        assert manager.begin_playback(1, 11)
        assert manager.offer_pcm(1, 11, 42, pcm)
        assert manager.offer_pcm(1, 11, 42, pcm)
        assert fired.wait(timeout=1)
        assert [event["token"] for event in events] == [10, 11]
    finally:
        manager.close()
    assert engine.closed is True


def test_manager_ignores_unknown_user_and_stale_token():
    events = []
    manager = DiscordStreamingKwsManager(
        StreamingKwsConfig(enabled=True, queue_frames=32),
        ("하나야 잠깐",),
        events.append,
        engine_factory=lambda *_args: _FakeEngine(None, None),
    )
    try:
        manager.begin_playback(1, 5)
        assert manager.offer_pcm(1, 5, 0, b"x") is False
        assert manager.offer_pcm(1, 4, 42, b"\x00" * 3840)
        time.sleep(0.05)
        assert events == []
        manager.end_playback(1, 5)
        assert manager.offer_pcm(1, 5, 42, b"\x00" * 3840)
        time.sleep(0.05)
        assert events == []
    finally:
        manager.close()


def test_manager_queue_is_bounded_and_reports_drops():
    release = threading.Event()
    manager = DiscordStreamingKwsManager(
        StreamingKwsConfig(enabled=True, queue_frames=32),
        ("하나야 잠깐",),
        lambda _event: None,
        engine_factory=lambda *_args: _FakeEngine(None, None, fire_on=999, block=release),
    )
    pcm = b"\x00" * 3840
    try:
        manager.begin_playback(1, 9)
        for _ in range(128):
            manager.offer_pcm(1, 9, 42, pcm)
        assert manager.snapshot_stats()["queue_drops"] > 0
        started = time.monotonic()
        assert manager.end_playback(1, 9) is False
        assert time.monotonic() - started < 0.05
    finally:
        release.set()
        manager.close()


def test_manager_idle_flush_can_detect_final_short_phrase():
    events = []
    fired = threading.Event()

    class FlushEngine(_FakeEngine):
        def process(self, stream, _pcm):
            stream["frames"] += 1
            return None

        def flush(self, stream):
            return 0 if stream["frames"] else None

    def callback(event):
        events.append(event)
        fired.set()

    manager = DiscordStreamingKwsManager(
        StreamingKwsConfig(enabled=True, queue_frames=32),
        ("하나야 잠깐",),
        callback,
        engine_factory=lambda *_args: FlushEngine(None, None),
    )
    try:
        manager.begin_playback(1, 20)
        manager.offer_pcm(1, 20, 42, b"\x00" * 3840)
        assert fired.wait(timeout=1)
        assert len(events) == 1
        assert events[0]["token"] == 20
    finally:
        manager.close()


def test_manager_default_startup_is_non_blocking():
    release = threading.Event()

    def slow_factory(*_args):
        assert release.wait(timeout=2)
        return _FakeEngine(None, None)

    started = time.monotonic()
    manager = DiscordStreamingKwsManager(
        StreamingKwsConfig(enabled=True),
        ("하나야 잠깐",),
        lambda _event: None,
        engine_factory=slow_factory,
    )
    try:
        assert time.monotonic() - started < 0.2
        assert manager.snapshot_stats()["ready"] == 0
        release.set()
        assert manager._ready.wait(timeout=1)
        assert manager.snapshot_stats()["startup_failed"] == 0
    finally:
        release.set()
        manager.close()


def test_manager_synchronous_startup_check_surfaces_bounded_error(caplog):
    def broken_factory(*_args):
        raise RuntimeError("sensitive model path")

    with pytest.raises(RuntimeError, match="failed to start"):
        DiscordStreamingKwsManager(
            StreamingKwsConfig(enabled=True),
            ("하나야 잠깐",),
            lambda _event: None,
            engine_factory=broken_factory,
            start_timeout=1,
        )
    assert "type=RuntimeError" in caplog.text
    assert "sensitive model path" not in caplog.text


def test_manager_callback_error_is_bounded_and_worker_survives(caplog):
    caplog.set_level("INFO")
    fired = threading.Event()

    def callback(_event):
        fired.set()
        raise RuntimeError("sensitive callback data")

    manager = DiscordStreamingKwsManager(
        StreamingKwsConfig(enabled=True),
        ("하나야 잠깐",),
        callback,
        engine_factory=lambda *_args: _FakeEngine(None, None, fire_on=1),
    )
    try:
        manager.begin_playback(1, 30)
        manager.offer_pcm(1, 30, 42, b"\x00" * 3840)
        assert fired.wait(timeout=1)
        deadline = time.monotonic() + 1
        while (
            (
                manager.snapshot_stats()["worker_errors"] < 1
                or "type=RuntimeError" not in caplog.text
            )
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert manager.snapshot_stats()["worker_errors"] == 1
        assert "type=RuntimeError" in caplog.text
        assert "sensitive callback data" not in caplog.text
    finally:
        manager.close()


def test_closed_manager_rejects_new_audio_and_control():
    manager = DiscordStreamingKwsManager(
        StreamingKwsConfig(enabled=True),
        ("하나야 잠깐",),
        lambda _event: None,
        engine_factory=lambda *_args: _FakeEngine(None, None),
    )
    manager.close()
    manager.close()
    assert manager.begin_playback(1, 40) is False
    assert manager.offer_pcm(1, 40, 42, b"pcm") is False


def test_worker_inference_and_idle_flush_errors_are_bounded(caplog):
    caplog.set_level("INFO")

    class BrokenEngine(_FakeEngine):
        def process(self, stream, _pcm):
            stream["frames"] += 1
            raise RuntimeError("sensitive inference data")

        def flush(self, _stream):
            raise RuntimeError("sensitive flush data")

    manager = DiscordStreamingKwsManager(
        StreamingKwsConfig(enabled=True),
        ("하나야 잠깐",),
        lambda _event: None,
        engine_factory=lambda *_args: BrokenEngine(None, None),
    )
    try:
        manager.begin_playback(1, 50)
        manager.offer_pcm(1, 50, 42, b"\x00" * 3840)
        deadline = time.monotonic() + 1.5
        while (
            (
                manager.snapshot_stats()["worker_errors"] < 2
                or "frame failed type=RuntimeError" not in caplog.text
                or "idle flush failed type=RuntimeError" not in caplog.text
            )
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert manager.snapshot_stats()["worker_errors"] >= 2
        assert "frame failed type=RuntimeError" in caplog.text
        assert "idle flush failed type=RuntimeError" in caplog.text
        assert "sensitive inference data" not in caplog.text
        assert "sensitive flush data" not in caplog.text
    finally:
        manager.close()
