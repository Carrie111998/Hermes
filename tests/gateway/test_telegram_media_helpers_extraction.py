"""Regression tests for the media-delivery helpers extracted to
``MediaSendMixin`` (shard s4 of the adapter god-file decomposition).

Covers the PURE helpers that moved verbatim: media size validation, the
file-not-found error builder, duration coercion and the WAV probing path,
plus the ``_MEDIA_SEND_READ_TIMEOUT`` constant re-export.
"""

import io
import struct
import wave
from types import SimpleNamespace

from gateway.config import Platform, PlatformConfig
from plugins.platforms.telegram.adapter import (
    TelegramAdapter,
    _MEDIA_SEND_READ_TIMEOUT as adapter_media_timeout,
    _coerce_duration_seconds as adapter_coerce,
    _probe_voice_duration_seconds as adapter_probe,
)
from plugins.platforms.telegram.media_send_mixin import (
    MediaSendMixin,
    _MEDIA_SEND_READ_TIMEOUT,
    _coerce_duration_seconds,
    _probe_voice_duration_seconds,
)


def _make_adapter(**extra):
    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="***", extra=extra)
    adapter._bot = SimpleNamespace(id=999, username="hermes_bot")
    return adapter


def _source(size):
    return SimpleNamespace(file_size=size)


# ---------------------------------------------------------------------------
# MRO wiring + re-exported helpers
# ---------------------------------------------------------------------------

def test_media_send_mixin_wired_into_adapter_mro():
    assert isinstance(_make_adapter(), MediaSendMixin)


def test_media_helpers_reexported_by_adapter():
    assert _MEDIA_SEND_READ_TIMEOUT is adapter_media_timeout
    assert _coerce_duration_seconds is adapter_coerce
    assert _probe_voice_duration_seconds is adapter_probe
    assert _MEDIA_SEND_READ_TIMEOUT == 60.0


# ---------------------------------------------------------------------------
# Size validation
# ---------------------------------------------------------------------------

def test_missing_media_path_error():
    adapter = _make_adapter()
    plain = adapter._missing_media_path_error("Image", "/tmp/nope.png")
    assert plain == "Image file not found: /tmp/nope.png"
    sandbox = adapter._missing_media_path_error("Audio", "/workspace/out.mp3")
    assert "Docker sandbox" in sandbox and "host-visible" in sandbox


def test_media_too_large_note_formatting():
    adapter = _make_adapter()
    note = adapter._telegram_media_too_large_note("Image", 21 * 1024 * 1024, 20 * 1024 * 1024)
    assert "21.0 MB" in note and "20 MB limit" in note
    # None size renders as 0.0 MB (int(None or 0)); a non-numeric value is
    # the case that produces the "unknown size" fallback.
    note_zero = adapter._telegram_media_too_large_note("Image", None, 20 * 1024 * 1024)
    assert "0.0 MB" in note_zero
    note_unknown = adapter._telegram_media_too_large_note("Image", "n/a", 20 * 1024 * 1024)
    assert "unknown size" in note_unknown


def test_media_size_allowed():
    adapter = _make_adapter()
    assert adapter._telegram_media_size_allowed(_source(1024), "Image") == (True, None)
    assert adapter._telegram_media_size_allowed(_source(0), "Image") == (True, None)
    assert adapter._telegram_media_size_allowed(_source(None), "Image") == (True, None)
    ok, note = adapter._telegram_media_size_allowed(
        _source(25 * 1024 * 1024), "Image")
    assert ok is False and "25.0 MB" in note
    # configurable cap
    adapter_big = _make_adapter()
    adapter_big._max_doc_bytes = 100 * 1024 * 1024
    assert adapter_big._telegram_media_size_allowed(
        _source(50 * 1024 * 1024), "Image") == (True, None)


# ---------------------------------------------------------------------------
# Duration helpers
# ---------------------------------------------------------------------------

def test_coerce_duration_seconds():
    assert _coerce_duration_seconds(1.4) == 1
    assert _coerce_duration_seconds(1.6) == 2
    assert _coerce_duration_seconds(0) is None
    assert _coerce_duration_seconds(-3) is None
    assert _coerce_duration_seconds("12.9") == 13
    assert _coerce_duration_seconds("nope") is None
    assert _coerce_duration_seconds(None) is None


def test_probe_voice_duration_seconds_reads_real_wav(tmp_path):
    path = tmp_path / "tone.wav"
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(8000)
        wf.writeframes(b"\x00\x00" * 8000)  # 1 second of silence
    assert _probe_voice_duration_seconds(str(path)) == 1


def test_probe_voice_duration_seconds_missing_file_returns_none():
    assert _probe_voice_duration_seconds("C:/definitely/not/here.wav") is None


def test_probe_wav_header_roundtrip_via_wave_module(tmp_path):
    # tiny valid wav written with struct, read back by the probe
    path = tmp_path / "tiny.wav"
    data = b"\x00\x00" * 4000  # 0.5s at 8kHz
    with open(path, "wb") as f:
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + len(data)))
        f.write(b"WAVEfmt ")
        f.write(struct.pack("<IHHIIHH", 16, 1, 1, 8000, 16000, 2, 16))
        f.write(b"data")
        f.write(struct.pack("<I", len(data)))
        f.write(data)
    # probe should round to whole seconds (0.5 -> 0 -> None)
    assert _probe_voice_duration_seconds(str(path)) is None
