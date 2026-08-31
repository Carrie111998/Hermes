"""Tests for the Discord continuous voice mixer (ambient + ducked speech)
and the verbal-ack-before-tool-calls hook.

The mixer (plugins/platforms/discord/voice_mixer.py) is pure-PCM and has no
discord.py dependency, so its core is tested directly.  The adapter
integration (install on join, play routing, ack) is tested with the standard
``object.__new__(DiscordAdapter)`` helper used elsewhere in the voice suite.
"""

import asyncio
import os
import struct
import sys
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# numpy ships only in the optional "voice" extra (not [all,dev]); the mixer
# math needs it, so skip this whole module when it isn't installed.
np = pytest.importorskip("numpy")

# voice_mixer lives inside the discord plugin package dir; import by path the
# same way the adapter does.
_DISCORD_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "plugins", "platforms", "discord",
)
if _DISCORD_DIR not in sys.path:
    sys.path.insert(0, _DISCORD_DIR)

import voice_mixer as vm  # noqa: E402


# =====================================================================
# Pure mixer unit tests
# =====================================================================

class TestVoiceMixerCore:
    def test_frame_geometry_matches_discord(self):
        # 20ms @ 48kHz stereo s16 == 3840 bytes (discord.opus.Encoder.FRAME_SIZE)
        assert vm.FRAME_SIZE == 3840
        assert vm.SAMPLES_PER_FRAME == 960
        assert len(vm.SILENCE_FRAME) == vm.FRAME_SIZE

    def test_empty_mixer_returns_silence_frames(self):
        mx = vm.VoiceMixer()
        for _ in range(5):
            frame = mx.read()
            assert len(frame) == vm.FRAME_SIZE
            assert frame == vm.SILENCE_FRAME

    def test_is_opus_false(self):
        # discord.py sends raw PCM when is_opus() is False.
        assert vm.VoiceMixer().is_opus() is False

    def test_streaming_child_reads_fifo_and_drains_after_final_padding(self):
        mixer = vm.VoiceMixer()
        child = mixer.begin_streaming_speech(fade_in_ms=0)
        first = b"\x01\x00" * (vm.FRAME_SIZE // 2)
        second = b"\x02\x00" * (vm.FRAME_SIZE // 2)
        partial = b"\x03\x00" * 10

        child.write(first + second + partial)
        child.finish()

        np.testing.assert_array_equal(
            child.read_frame(), np.frombuffer(first, dtype=np.int16).astype(np.float32)
        )
        np.testing.assert_array_equal(
            child.read_frame(), np.frombuffer(second, dtype=np.int16).astype(np.float32)
        )
        np.testing.assert_array_equal(
            child.read_frame(),
            np.frombuffer(
                partial + b"\x00" * (vm.FRAME_SIZE - len(partial)), dtype=np.int16
            ).astype(np.float32),
        )
        assert child.drained is True
        assert child.read_frame() is None

    def test_final_padded_streaming_frame_removes_active_child_on_same_read(self):
        mixer = vm.VoiceMixer()
        child = mixer.begin_streaming_speech(fade_in_ms=0)
        partial = b"\x03\x00" * 10
        child.write(partial)
        child.finish()

        frame = mixer.read()

        np.testing.assert_array_equal(
            np.frombuffer(frame, dtype=np.int16),
            np.frombuffer(
                partial + b"\x00" * (vm.FRAME_SIZE - len(partial)), dtype=np.int16
            ),
        )
        assert mixer.speech_active is False
        assert child not in mixer._speech
        assert child not in mixer._streaming_speech

    def test_read_discards_finished_never_activated_stream_only(self):
        mixer = vm.VoiceMixer()
        unfinished = mixer.begin_streaming_speech(fade_in_ms=0)
        finished = mixer.begin_streaming_speech(fade_in_ms=0)
        finished.finish()

        mixer.read()

        assert finished not in mixer._streaming_speech
        assert unfinished in mixer._streaming_speech
        assert mixer.speech_active is False

    def test_streaming_child_abort_wakes_writer_blocked_at_capacity(self):
        mixer = vm.VoiceMixer()
        child = mixer.begin_streaming_speech(fade_in_ms=0)
        child.write(b"\x01\x00" * (
            vm.STREAMING_BUFFER_FRAME_CAPACITY * vm.FRAME_SIZE // 2
        ))
        started = threading.Event()
        completed = threading.Event()

        def write_one_more_frame():
            started.set()
            child.write(b"\x02\x00" * (vm.FRAME_SIZE // 2))
            completed.set()

        writer = threading.Thread(target=write_one_more_frame)
        writer.start()
        assert started.wait(timeout=1)
        assert not completed.wait(timeout=0.1)

        child.abort()

        writer.join(timeout=1)
        assert not writer.is_alive()
        assert completed.is_set()
        assert child.read_frame() is None
        assert child.drained is True

    def test_ambient_loops_and_is_quiet(self):
        mx = vm.VoiceMixer(ambient_gain=0.2)
        amb = vm.synth_ambient_pcm(seconds=0.5)
        assert len(amb) % vm.FRAME_SIZE == 0  # frame-aligned for seamless loop
        mx.set_ambient(amb)
        peaks = [int(np.max(np.abs(np.frombuffer(mx.read(), dtype=np.int16))))
                 for _ in range(100)]  # 2s >> 0.5s loop
        # Produces audio after the fade-in and stays under the configured gain.
        assert any(p > 0 for p in peaks[10:])
        assert max(peaks) < int(32767 * 0.5)


# =====================================================================
# Adapter integration
# =====================================================================

def _make_adapter(fx_cfg=None):
    from plugins.platforms.discord.adapter import DiscordAdapter
    from gateway.config import Platform, PlatformConfig
    config = PlatformConfig(enabled=True, extra={})
    config.token = "fake-token"
    adapter = object.__new__(DiscordAdapter)
    adapter.platform = Platform.DISCORD
    adapter.config = config
    adapter._client = MagicMock()
    adapter._voice_clients = {}
    adapter._voice_locks = {}
    adapter._voice_text_channels = {}
    adapter._voice_sources = {}
    adapter._voice_timeout_tasks = {}
    adapter._voice_receivers = {}
    adapter._voice_listen_tasks = {}
    adapter._voice_mixers = {}
    adapter._ambient_pcm_cache = None
    adapter._voice_fx_cfg = fx_cfg if fx_cfg is not None else {
        "enabled": True, "ambient_enabled": True, "ambient_path": "",
        "ambient_gain": 0.18, "duck_gain": 0.06, "speech_gain": 1.0,
        "ack_enabled": True, "ack_phrases": ["One moment."],
    }
    return adapter


class TestDiscordStreamingTTS:
    @pytest.mark.asyncio
    async def test_connected_bound_mixer_supports_and_begins_stream(self):
        from gateway.platforms.base import AudioFormat

        adapter = _make_adapter()
        vc = MagicMock()
        vc.is_connected.return_value = True
        mixer = vm.VoiceMixer()
        adapter._voice_clients[111] = vc
        adapter._voice_text_channels[111] = 222
        adapter._voice_mixers[111] = mixer
        adapter._cancel_voice_timeout = MagicMock()

        audio_format = AudioFormat(24000, 1, 2)
        assert adapter.supports_streaming_tts("222", audio_format) is True

        handle = await adapter.begin_streaming_tts("222", audio_format)

        assert handle is not None
        assert handle.guild_id == 111
        assert handle.mixer is mixer
        assert handle.child in mixer._streaming_speech
        adapter._cancel_voice_timeout.assert_called_once_with(111)

    @pytest.mark.asyncio
    async def test_split_24k_mono_pcm_writes_as_fifo_discord_pcm(self):
        from gateway.platforms.base import AudioFormat

        adapter = _make_adapter()
        vc = MagicMock()
        vc.is_connected.return_value = True
        mixer = vm.VoiceMixer()
        adapter._voice_clients[111] = vc
        adapter._voice_text_channels[111] = 222
        adapter._voice_mixers[111] = mixer
        adapter._cancel_voice_timeout = MagicMock()

        handle = await adapter.begin_streaming_tts("222", AudioFormat(24000, 1, 2))
        source = b"\x01\x00\xfe\xff\x03\x00"
        for chunk in (source[:1], source[1:3], source[3:]):
            await adapter.write_streaming_tts(handle, chunk)
        await adapter.finish_streaming_tts(handle)

        handle.child.fade_frames = 0
        frame = handle.child.read_frame()
        expected = b"".join(struct.pack("<hhhh", sample, sample, sample, sample)
                            for sample in (1, -2, 3))
        assert frame is not None
        np.testing.assert_array_equal(
            frame[:12], np.frombuffer(expected, dtype=np.int16).astype(np.float32),
        )

    @pytest.mark.asyncio
    async def test_unavailable_or_stale_voice_state_declines_streaming(self):
        from gateway.platforms.base import AudioFormat

        audio_format = AudioFormat(24000, 1, 2)
        adapter = _make_adapter()
        assert adapter.supports_streaming_tts("222", audio_format) is False
        assert await adapter.begin_streaming_tts("222", audio_format) is None

        vc = MagicMock()
        vc.is_connected.return_value = False
        adapter._voice_clients[111] = vc
        adapter._voice_text_channels[111] = 222
        assert adapter.supports_streaming_tts("222", audio_format) is False
        assert await adapter.begin_streaming_tts("222", audio_format) is None

        vc.is_connected.return_value = True
        with patch.object(adapter, "_discord_streaming_numpy_available", return_value=False):
            assert adapter.supports_streaming_tts("222", audio_format) is False
            assert await adapter.begin_streaming_tts("222", audio_format) is None

        child = MagicMock()

        class StaleMixer:
            def begin_streaming_speech(self):
                adapter._voice_mixers.pop(111)
                return child

        adapter._voice_mixers[111] = StaleMixer()
        assert await adapter.begin_streaming_tts("222", audio_format) is None
        child.abort.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_finish_waits_for_drain_and_abort_drops_late_writes(self):
        from gateway.platforms.base import AudioFormat

        class Child:
            def __init__(self):
                self.drained = False
                self.finish = MagicMock()
                self.abort = MagicMock()
                self.write = MagicMock()

        class Mixer:
            def __init__(self):
                self.child = Child()
                self.speech_active = True

            def begin_streaming_speech(self):
                return self.child

        adapter = _make_adapter()
        vc = MagicMock()
        vc.is_connected.return_value = True
        mixer = Mixer()
        adapter._voice_clients[111] = vc
        adapter._voice_text_channels[111] = 222
        adapter._voice_mixers[111] = mixer
        adapter._cancel_voice_timeout = MagicMock()
        adapter._reset_voice_timeout = MagicMock()
        handle = await adapter.begin_streaming_tts("222", AudioFormat(24000, 1, 2))

        await adapter.finish_streaming_tts(handle)

        mixer.child.finish.assert_called_once_with()
        assert adapter._reset_voice_timeout.call_count == 0
        assert handle.drain_task is not None

        mixer.child.drained = True
        mixer.speech_active = False
        await asyncio.wait_for(handle.drain_task, timeout=0.5)
        adapter._reset_voice_timeout.assert_called_once_with(111)

        await adapter.abort_streaming_tts(handle)
        await adapter.abort_streaming_tts(handle)
        await adapter.write_streaming_tts(handle, b"\x01\x00")
        mixer.child.abort.assert_called_once_with()
        mixer.child.write.assert_not_called()


class TestVoiceMixerActive:


    def test_false_when_attr_missing(self):
        # Defensive getattr path (object.__new__ helper that forgot the attr).
        from plugins.platforms.discord.adapter import DiscordAdapter
        from gateway.config import Platform
        bare = object.__new__(DiscordAdapter)
        bare.platform = Platform.DISCORD
        assert bare.voice_mixer_active(111) is False


class TestPlayInVoiceChannelMixerPath:
    @pytest.mark.asyncio
    async def test_routes_through_mixer_when_present(self):
        adapter = _make_adapter()
        vc = MagicMock()
        vc.is_connected.return_value = True
        adapter._voice_clients[111] = vc

        # speech_active returns True once (so play_speech is observed) then
        # False so the wait loop exits promptly.
        class _Mixer:
            def __init__(self):
                self._polls = 0
                self.play_speech = MagicMock()

            @property
            def speech_active(self):
                self._polls += 1
                return self._polls <= 1

        mixer = _Mixer()
        adapter._voice_mixers[111] = mixer
        adapter._reset_voice_timeout = MagicMock()

        fake_pcm = b"\x00" * vm.FRAME_SIZE
        with patch.object(vm, "decode_to_pcm", return_value=fake_pcm):
            ok = await adapter.play_in_voice_channel(111, "/tmp/x.mp3")
        assert ok is True
        mixer.play_speech.assert_called_once()
        adapter._reset_voice_timeout.assert_called_once_with(111)
        # Legacy path must NOT have been used.
        vc.play.assert_not_called()


class TestLeadSilence:
    """Warm-up lead silence prepended to speech so the first word isn't clipped
    (issue #66827)."""

    def test_bytes_empty_when_unset(self):
        adapter = _make_adapter()  # default cfg has no lead_silence_ms
        assert adapter._lead_silence_bytes() == b""


    def test_bytes_length_matches_ms(self):
        adapter = _make_adapter({"lead_silence_ms": 200})
        lead = adapter._lead_silence_bytes()
        assert lead == b"\x00" * (vm.BYTES_PER_MS * 200)
        assert len(lead) == 200 * 192  # 48kHz stereo s16 -> 192 bytes/ms


class TestPlayAckInVoice:
    @pytest.mark.asyncio
    async def test_noop_when_ack_disabled(self):
        adapter = _make_adapter({"ack_enabled": False})
        adapter._voice_mixers[111] = MagicMock()
        assert await adapter.play_ack_in_voice(111) is False


