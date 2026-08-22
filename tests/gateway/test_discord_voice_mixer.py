"""Tests for the Discord continuous voice mixer (ambient + ducked speech)
and the verbal-ack-before-tool-calls hook.

The mixer (plugins/platforms/discord/voice_mixer.py) is pure-PCM and has no
discord.py dependency, so its core is tested directly.  The adapter
integration (install on join, play routing, ack) is tested with the standard
``object.__new__(DiscordAdapter)`` helper used elsewhere in the voice suite.
"""

import os
import struct
import sys
import time
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


def _build_rtp_packet(ssrc, seq=1):
    header = struct.pack(">BBHII", 0x80, 0x78, seq, 960 * seq, ssrc)
    return header + (b"\x00" * 20) + b"\x00\x00\x00\x01"


def _make_receiver(
    *,
    allowed_user_ids=None,
    speech_start_callback=None,
    speech_start_allowed=None,
):
    from plugins.platforms.discord.adapter import VoiceReceiver

    vc = MagicMock()
    vc._connection.secret_key = [0] * 32
    vc._connection.dave_session = None
    vc._connection.ssrc = 9999
    vc._connection.add_socket_listener = MagicMock()
    vc._connection.remove_socket_listener = MagicMock()
    vc._connection.hook = None
    receiver = VoiceReceiver(
        vc,
        allowed_user_ids=allowed_user_ids,
        speech_start_callback=speech_start_callback,
        speech_start_allowed=speech_start_allowed,
    )
    receiver.start()
    return receiver


def _send_decoded_packet(receiver, ssrc, *, pcm=b"\x00" * 3840, seq=1):
    pytest.importorskip("nacl")
    decoder = receiver._decoders.setdefault(ssrc, MagicMock())
    decoder.decode.return_value = pcm
    with patch("nacl.secret.Aead") as aead:
        aead.return_value.decrypt.return_value = b"\xf8\xff\xfe"
        receiver._on_packet(_build_rtp_packet(ssrc, seq))


class TestVoiceReceiverSpeechStart:
    def test_authorized_mapped_user_only(self):
        callback = MagicMock()
        receiver = _make_receiver(
            allowed_user_ids={"42"},
            speech_start_callback=callback,
        )
        receiver.map_ssrc(100, 42)
        receiver.map_ssrc(200, 7)

        _send_decoded_packet(receiver, 100)
        _send_decoded_packet(receiver, 200)
        _send_decoded_packet(receiver, 300)

        callback.assert_called_once_with(42)

    @pytest.mark.parametrize(
        ("allowed_user_ids", "canonical_result", "expected_calls"),
        [(set(), False, 0), ({"7"}, True, 1)],
    )
    def test_canonical_authorization_overrides_raw_user_ids(
        self,
        allowed_user_ids,
        canonical_result,
        expected_calls,
    ):
        callback = MagicMock()
        allowed = MagicMock(return_value=canonical_result)
        receiver = _make_receiver(
            allowed_user_ids=allowed_user_ids,
            speech_start_callback=callback,
            speech_start_allowed=allowed,
        )
        receiver.map_ssrc(100, 42)

        _send_decoded_packet(receiver, 100)

        allowed.assert_called_once_with(42)
        assert callback.call_count == expected_calls

    @pytest.mark.parametrize(
        "reset_path",
        ["silence_emit", "silence_discard", "flush", "stop"],
    )
    def test_fires_once_per_utterance_and_resets(self, reset_path):
        callback = MagicMock()
        receiver = _make_receiver(
            allowed_user_ids={"42"},
            speech_start_callback=callback,
        )
        receiver.map_ssrc(100, 42)
        pcm = b"\x00" * (96000 if reset_path == "silence_emit" else 3840)

        _send_decoded_packet(receiver, 100, pcm=pcm, seq=1)
        _send_decoded_packet(receiver, 100, pcm=pcm, seq=2)
        callback.assert_called_once_with(42)

        if reset_path.startswith("silence_"):
            receiver._last_packet_time[100] = time.monotonic() - 4.0
            receiver.check_silence()
        elif reset_path == "flush":
            receiver.flush_pending()
        else:
            receiver.stop()
            receiver.start()
            receiver.map_ssrc(100, 42)

        _send_decoded_packet(receiver, 100, pcm=pcm, seq=3)
        assert callback.call_count == 2
        assert all(args == (42,) for args, _kwargs in callback.call_args_list)

    def test_authorization_runs_outside_receiver_lock(self):
        callback = MagicMock()
        lock_was_available = []
        receiver = None

        def allowed(_user_id):
            acquired = receiver._lock.acquire(blocking=False)
            lock_was_available.append(acquired)
            if acquired:
                receiver._lock.release()
            return True

        receiver = _make_receiver(
            allowed_user_ids={"42"},
            speech_start_callback=callback,
            speech_start_allowed=allowed,
        )
        receiver.map_ssrc(100, 42)

        _send_decoded_packet(receiver, 100)

        assert lock_was_available == [True]
        callback.assert_called_once_with(42)

    def test_callback_exception_does_not_escape_socket_reader(self):
        callback = MagicMock(side_effect=RuntimeError("boom"))
        receiver = _make_receiver(
            allowed_user_ids={"42"},
            speech_start_callback=callback,
        )
        receiver.map_ssrc(100, 42)

        _send_decoded_packet(receiver, 100, seq=1)
        _send_decoded_packet(receiver, 100, seq=2)

        callback.assert_called_once_with(42)
        assert len(receiver._buffers[100]) == 7680


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


class TestVoiceMixerActive:


    def test_false_when_attr_missing(self):
        # Defensive getattr path (object.__new__ helper that forgot the attr).
        from plugins.platforms.discord.adapter import DiscordAdapter
        from gateway.config import Platform
        bare = object.__new__(DiscordAdapter)
        bare.platform = Platform.DISCORD
        assert bare.voice_mixer_active(111) is False


class TestVoiceBargeIn:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("enabled", [True, False])
    async def test_receiver_callback_is_wired_only_when_enabled(self, enabled):
        from plugins.platforms.discord import adapter as discord_adapter
        from gateway.config import PlatformConfig

        with patch("hermes_cli.config.read_raw_config", return_value={
            "voice": {"barge_in": enabled},
        }):
            adapter = discord_adapter.DiscordAdapter(
                PlatformConfig(enabled=True, token="fake-token")
            )
        adapter._client = MagicMock()
        adapter._allowed_user_ids = {"42"}
        adapter._reset_voice_timeout = MagicMock()
        channel = MagicMock()
        channel.guild.id = 111
        channel.connect = AsyncMock(return_value=MagicMock())

        def discard_listen_loop(coro):
            coro.close()
            return MagicMock()

        with patch.object(discord_adapter, "DISCORD_AVAILABLE", True), \
                patch.object(discord_adapter, "VoiceReceiver") as receiver_cls, \
                patch.object(
                    discord_adapter.asyncio,
                    "ensure_future",
                    side_effect=discard_listen_loop,
                ):
            await adapter.join_voice_channel(channel)

        callback = receiver_cls.call_args.kwargs.get("speech_start_callback")
        allowed = receiver_cls.call_args.kwargs.get("speech_start_allowed")
        assert callable(callback) is enabled
        assert callable(allowed) is enabled
        if enabled:
            guild = adapter._client.get_guild.return_value
            adapter._is_allowed_user = MagicMock(return_value=False)
            assert allowed(42) is False
            adapter._is_allowed_user.assert_called_once_with(
                "42", guild=guild, is_dm=False
            )

    def test_active_mixer_speech_is_interrupted_before_legacy_playback(self):
        adapter = _make_adapter()
        mixer = MagicMock()
        mixer.speech_active = True
        adapter._voice_mixers[111] = mixer
        vc = MagicMock()
        vc.is_playing.return_value = True
        adapter._voice_clients[111] = vc

        adapter._on_voice_speech_start(111, 42)

        mixer.stop_speech.assert_called_once_with()
        vc.stop.assert_not_called()

    def test_active_legacy_playback_is_interrupted_without_mixer_speech(self):
        adapter = _make_adapter()
        vc = MagicMock()
        vc.is_playing.return_value = True
        adapter._voice_clients[111] = vc

        adapter._on_voice_speech_start(111, 42)

        vc.stop.assert_called_once_with()

    @pytest.mark.parametrize("with_idle_mixer", [False, True])
    def test_noop_without_active_speech(self, with_idle_mixer):
        adapter = _make_adapter()
        vc = MagicMock()
        vc.is_playing.return_value = False
        adapter._voice_clients[111] = vc
        if with_idle_mixer:
            mixer = MagicMock()
            mixer.speech_active = False
            adapter._voice_mixers[111] = mixer

        adapter._on_voice_speech_start(111, 42)

        vc.stop.assert_not_called()
        if with_idle_mixer:
            mixer.stop_speech.assert_not_called()

    @pytest.mark.asyncio
    async def test_legacy_playback_keeps_capture_live_when_enabled(self):
        adapter = _make_adapter()
        adapter._voice_barge_in_enabled = True
        adapter._playback_timeout_for_audio = AsyncMock(return_value=30.0)
        adapter._cancel_voice_timeout = MagicMock()
        adapter._reset_voice_timeout = MagicMock()
        receiver = MagicMock()
        adapter._voice_receivers[111] = receiver
        vc = MagicMock()
        vc.is_connected.return_value = True
        vc.is_playing.return_value = False
        vc.play.side_effect = lambda _source, after: after(None)
        adapter._voice_clients[111] = vc

        with patch("plugins.platforms.discord.adapter.discord") as discord_mock:
            discord_mock.FFmpegPCMAudio.return_value = MagicMock()
            discord_mock.PCMVolumeTransformer.return_value = MagicMock()
            assert await adapter.play_in_voice_channel(111, "/tmp/speech.mp3") is True

        receiver.pause.assert_not_called()
        receiver.resume.assert_not_called()


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
