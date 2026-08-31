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


class _VoiceClientState:
    """Concrete voice-client state double for resolver lifecycle tests."""

    def __init__(self, source: object | None, *, connected=True, playing=True):
        self.source = source
        self.connected = connected
        self.playing = playing

    def is_connected(self):
        return self.connected

    def is_playing(self):
        return self.playing


class TestDiscordStreamingTTS:
    @pytest.mark.asyncio
    async def test_connected_bound_mixer_supports_and_begins_stream(self):
        from gateway.platforms.base import AudioFormat

        adapter = _make_adapter()
        mixer = vm.VoiceMixer()
        vc = _VoiceClientState(mixer)
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
    async def test_duck_typed_non_voice_mixer_declines_streaming(self):
        from gateway.platforms.base import AudioFormat

        class DuckTypedMixer:
            begin_streaming_speech = MagicMock()

        adapter = _make_adapter()
        duck_typed_mixer = DuckTypedMixer()
        vc = _VoiceClientState(duck_typed_mixer)
        adapter._voice_clients[111] = vc
        adapter._voice_text_channels[111] = 222
        adapter._voice_mixers[111] = duck_typed_mixer

        audio_format = AudioFormat(24000, 1, 2)
        assert adapter.supports_streaming_tts("222", audio_format) is False
        assert await adapter.begin_streaming_tts("222", audio_format) is None
        duck_typed_mixer.begin_streaming_speech.assert_not_called()

    @pytest.mark.asyncio
    async def test_connected_nonplaying_mixer_declines_streaming(self):
        from gateway.platforms.base import AudioFormat

        adapter = _make_adapter()
        mixer = vm.VoiceMixer()
        vc = _VoiceClientState(mixer, playing=False)
        adapter._voice_clients[111] = vc
        adapter._voice_text_channels[111] = 222
        adapter._voice_mixers[111] = mixer

        audio_format = AudioFormat(24000, 1, 2)
        assert adapter.supports_streaming_tts("222", audio_format) is False
        assert await adapter.begin_streaming_tts("222", audio_format) is None

    @pytest.mark.asyncio
    async def test_split_24k_mono_pcm_writes_as_fifo_discord_pcm(self):
        from gateway.platforms.base import AudioFormat

        adapter = _make_adapter()
        mixer = vm.VoiceMixer()
        vc = _VoiceClientState(mixer)
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
    async def test_native_48k_stereo_pcm_reaches_mixer_in_fifo_order(self):
        from gateway.platforms.base import AudioFormat
        from plugins.platforms.discord.adapter import _DiscordStreamingTTSHandle

        adapter = _make_adapter()
        mixer = vm.VoiceMixer()
        vc = _VoiceClientState(mixer)
        adapter._voice_clients[111] = vc
        adapter._voice_text_channels[111] = 222
        adapter._voice_mixers[111] = mixer
        adapter._cancel_voice_timeout = MagicMock()
        source = bytes(range(256)) * 15  # exactly one 48kHz stereo 20ms frame

        handle = await adapter.begin_streaming_tts("222", AudioFormat(48000, 2, 2))
        assert isinstance(handle, _DiscordStreamingTTSHandle)
        for chunk in (source[:1], source[1:138], source[138:]):
            await adapter.write_streaming_tts(handle, chunk)
        await adapter.finish_streaming_tts(handle)

        handle.child.fade_frames = 0
        frame = mixer.read()
        assert len(frame) == len(source)
        np.testing.assert_array_equal(
            np.frombuffer(frame, dtype=np.int16), np.frombuffer(source, dtype=np.int16)
        )

    @pytest.mark.asyncio
    async def test_stale_after_begin_fails_consumer_before_audio_for_fallback(self):
        from gateway.streaming_tts_consumer import StreamingTTSConsumer
        from plugins.platforms.discord.adapter import _DiscordStreamingTTSHandle

        class SingleChunkStreamer:
            sample_rate = 24000
            channels = 1
            sample_width = 2

            def stream(self, text):
                del text
                yield b"\x01\x00" * 32

        adapter = _make_adapter()
        mixer = vm.VoiceMixer()
        vc = _VoiceClientState(mixer)
        adapter._voice_clients[111] = vc
        adapter._voice_text_channels[111] = 222
        adapter._voice_mixers[111] = mixer
        adapter._cancel_voice_timeout = MagicMock()

        with patch("tools.tts_streaming.resolve_streaming_provider", return_value=SingleChunkStreamer()):
            consumer = StreamingTTSConsumer(adapter, "222", {}, asyncio.get_running_loop())
            consumer.start()
            for _ in range(100):
                if consumer.started:
                    break
                await asyncio.sleep(0.01)
            assert consumer.started is True
            assert isinstance(consumer._handle, _DiscordStreamingTTSHandle)
            child = consumer._handle.child

            # The existing voice connection replaces/unlinks the mixer between
            # begin and the first PCM write.
            adapter._voice_mixers.pop(111)
            consumer.on_delta("A complete answer.")
            consumer.finish()
            completed = await consumer.wait_complete(timeout=1.0)

        # The consumer's pre-audio failure state keeps whole-file fallback
        # eligible; the obsolete child receives neither PCM nor activation.
        assert completed is False
        assert consumer.completed is False
        assert consumer.audible is False
        assert consumer.suppress_whole_file is False
        assert child.drained is True
        assert child._aborted is True
        assert child._activated is False
        assert mixer.speech_active is False

    @pytest.mark.asyncio
    async def test_aborted_worker_write_before_child_lock_keeps_whole_file_fallback(self):
        """An aborted child that accepts no PCM must fail before audio."""
        from gateway.streaming_tts_consumer import StreamingTTSConsumer
        from plugins.platforms.discord.adapter import _DiscordStreamingTTSHandle

        class SingleChunkStreamer:
            sample_rate = 24000
            channels = 1
            sample_width = 2

            def stream(self, text):
                del text
                yield b"\xe8\x03" * (vm.FRAME_SIZE // 8)

        adapter = _make_adapter()
        mixer = vm.VoiceMixer()
        vc = _VoiceClientState(mixer)
        adapter._voice_clients[111] = vc
        adapter._voice_text_channels[111] = 222
        adapter._voice_mixers[111] = mixer
        adapter._cancel_voice_timeout = MagicMock()
        write_entered = threading.Event()
        release_write = threading.Event()

        with patch("tools.tts_streaming.resolve_streaming_provider", return_value=SingleChunkStreamer()):
            consumer = StreamingTTSConsumer(adapter, "222", {}, asyncio.get_running_loop())
            consumer.start()
            for _ in range(100):
                if consumer.started:
                    break
                await asyncio.sleep(0.01)
            assert consumer.started is True
            assert isinstance(consumer._handle, _DiscordStreamingTTSHandle)
            child = consumer._handle.child
            original_write = vm.StreamingMixerChild.write

            def write_after_abort_window(self, pcm):
                if self is child:
                    write_entered.set()
                    assert release_write.wait(timeout=1)
                return original_write(self, pcm)

            with patch.object(vm.StreamingMixerChild, "write", write_after_abort_window):
                consumer.on_delta("A complete answer.")
                consumer.finish()
                assert await asyncio.to_thread(write_entered.wait, 1)
                await adapter.abort_streaming_tts(consumer._handle)
                release_write.set()
                completed = await consumer.wait_complete(timeout=1.0)

        assert completed is False
        assert consumer.completed is False
        assert consumer.audible is False
        assert consumer.suppress_whole_file is False
        assert child.drained is True
        assert child._aborted is True
        assert child._activated is False
        assert child._buffer == bytearray()
        assert mixer.speech_active is False

    @pytest.mark.asyncio
    async def test_mixer_replaced_after_worker_write_suppresses_whole_file_replay(self):
        """PCM consumed before post-write staleness still suppresses file replay."""
        from gateway.streaming_tts_consumer import StreamingTTSConsumer
        from plugins.platforms.discord.adapter import _DiscordStreamingTTSHandle

        class SingleChunkStreamer:
            sample_rate = 24000
            channels = 1
            sample_width = 2

            def stream(self, text):
                del text
                # 480 mono samples upsample to one complete Discord frame.
                yield b"\xe8\x03" * (vm.FRAME_SIZE // 8)

        adapter = _make_adapter()
        mixer = vm.VoiceMixer()
        vc = _VoiceClientState(mixer)
        adapter._voice_clients[111] = vc
        adapter._voice_text_channels[111] = 222
        adapter._voice_mixers[111] = mixer
        adapter._cancel_voice_timeout = MagicMock()
        pcm_consumed = threading.Event()

        with patch("tools.tts_streaming.resolve_streaming_provider", return_value=SingleChunkStreamer()):
            consumer = StreamingTTSConsumer(adapter, "222", {}, asyncio.get_running_loop())
            consumer.start()
            for _ in range(100):
                if consumer.started:
                    break
                await asyncio.sleep(0.01)
            assert consumer.started is True
            assert isinstance(consumer._handle, _DiscordStreamingTTSHandle)
            child = consumer._handle.child
            original_write = vm.StreamingMixerChild.write

            def write_consume_then_replace(self, pcm):
                result = original_write(self, pcm)
                if self is child:
                    # The actual child write has queued a full frame and the
                    # mixer consumes it before the adapter's stale recheck.
                    assert mixer.read() != vm.SILENCE_FRAME
                    pcm_consumed.set()
                    replacement = vm.VoiceMixer()
                    adapter._voice_mixers[111] = replacement
                    vc.source = replacement
                return result

            with patch.object(vm.StreamingMixerChild, "write", write_consume_then_replace):
                consumer.on_delta("A complete answer.")
                consumer.finish()
                completed = await consumer.wait_complete(timeout=1.0)

        assert pcm_consumed.is_set()
        assert completed is False
        assert consumer.completed is False
        assert consumer.audible is True
        assert consumer.suppress_whole_file is True
        assert child.drained is True
        assert child._aborted is True
        assert child._activated is True

    @pytest.mark.asyncio
    async def test_unavailable_or_stale_voice_state_declines_streaming(self):
        from gateway.platforms.base import AudioFormat

        audio_format = AudioFormat(24000, 1, 2)
        adapter = _make_adapter()
        assert adapter.supports_streaming_tts("222", audio_format) is False
        assert await adapter.begin_streaming_tts("222", audio_format) is None

        vc = _VoiceClientState(None, connected=False)
        adapter._voice_clients[111] = vc
        adapter._voice_text_channels[111] = 222
        assert adapter.supports_streaming_tts("222", audio_format) is False
        assert await adapter.begin_streaming_tts("222", audio_format) is None

        vc.connected = True
        with patch.object(adapter, "_discord_streaming_numpy_available", return_value=False):
            assert adapter.supports_streaming_tts("222", audio_format) is False
            assert await adapter.begin_streaming_tts("222", audio_format) is None

        class RemovingVoiceMixer(vm.VoiceMixer):
            def begin_streaming_speech(self):
                child = super().begin_streaming_speech()
                adapter._voice_mixers.pop(111)
                return child

        mixer = RemovingVoiceMixer()
        vc.source = mixer
        adapter._voice_mixers[111] = mixer
        assert await adapter.begin_streaming_tts("222", audio_format) is None
        assert mixer._streaming_speech[0].drained is True

    @pytest.mark.asyncio
    async def test_finish_waits_for_drain_and_abort_drops_late_writes(self):
        from gateway.platforms.base import AudioFormat
        from plugins.platforms.discord.adapter import _DiscordStreamingTTSHandle

        adapter = _make_adapter()
        mixer = vm.VoiceMixer()
        vc = _VoiceClientState(mixer)
        adapter._voice_clients[111] = vc
        adapter._voice_text_channels[111] = 222
        adapter._voice_mixers[111] = mixer
        adapter._cancel_voice_timeout = MagicMock()
        adapter._reset_voice_timeout = MagicMock()
        handle = await adapter.begin_streaming_tts("222", AudioFormat(24000, 1, 2))
        assert isinstance(handle, _DiscordStreamingTTSHandle)

        await adapter.write_streaming_tts(handle, b"\x01\x00" * (vm.FRAME_SIZE // 2))
        await adapter.finish_streaming_tts(handle)

        assert adapter._reset_voice_timeout.call_count == 0
        assert handle.drain_task is not None

        for _ in range(4):
            mixer.read()
        assert handle.child.drained is True
        assert mixer.speech_active is False
        assert adapter._resolve_streaming_voice_mixer("222") == (111, mixer)
        await asyncio.wait_for(handle.drain_task, timeout=0.5)
        adapter._reset_voice_timeout.assert_called_once_with(111)

        await adapter.abort_streaming_tts(handle)
        await adapter.abort_streaming_tts(handle)
        await adapter.write_streaming_tts(handle, b"\x01\x00")
        assert handle.aborted is True
        assert handle.child.drained is True


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


