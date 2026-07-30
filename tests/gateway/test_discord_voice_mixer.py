"""Tests for the Discord continuous voice mixer (ambient + ducked speech)
and the verbal-ack-before-tool-calls hook.

The mixer (plugins/platforms/discord/voice_mixer.py) is pure-PCM and has no
discord.py dependency, so its core is tested directly.  The adapter
integration (install on join, play routing, ack) is tested with the standard
``object.__new__(DiscordAdapter)`` helper used elsewhere in the voice suite.
"""

import os
import sys
from types import SimpleNamespace
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
    adapter._allowed_user_ids = set()
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


def _voice_channel(*, public: bool, channel_id: int = 222, guild_id: int = 111):
    default_role = SimpleNamespace(id=guild_id)
    guild = SimpleNamespace(id=guild_id, default_role=default_role)
    channel = SimpleNamespace(
        id=channel_id,
        guild=guild,
        permissions_for=lambda role: SimpleNamespace(
            view_channel=public and role is default_role,
            connect=public and role is default_role,
        ),
    )
    channel.connect = AsyncMock()
    return channel


class TestVoiceMixerActive:


    def test_false_when_attr_missing(self):
        # Defensive getattr path (object.__new__ helper that forgot the attr).
        from plugins.platforms.discord.adapter import DiscordAdapter
        from gateway.config import Platform
        bare = object.__new__(DiscordAdapter)
        bare.platform = Platform.DISCORD
        assert bare.voice_mixer_active(111) is False


class TestVoicePublicWriterBoundary:
    @pytest.mark.asyncio
    async def test_join_rejects_target_without_public_proof(self):
        adapter = _make_adapter()
        channel = _voice_channel(public=False)

        with patch(
            "plugins.platforms.discord.adapter._discord_public_only_policy_required",
            return_value=True,
        ):
            ok = await adapter.join_voice_channel(channel)

        assert ok is False
        channel.connect.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_join_rejects_visible_but_closed_audience_voice_target(self):
        adapter = _make_adapter()
        channel = _voice_channel(public=True)
        channel.permissions_for = lambda _role: SimpleNamespace(
            view_channel=True,
            connect=False,
        )

        with patch(
            "plugins.platforms.discord.adapter._discord_public_only_policy_required",
            return_value=True,
        ):
            ok = await adapter.join_voice_channel(channel)

        assert ok is False
        channel.connect.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_install_mixer_rejects_target_without_public_proof(self):
        adapter = _make_adapter()
        voice_client = MagicMock()
        voice_client.channel = _voice_channel(public=False)

        with patch(
            "plugins.platforms.discord.adapter._discord_public_only_policy_required",
            return_value=True,
        ), pytest.raises(PermissionError, match="not publicly visible"):
            await adapter._install_voice_mixer(111, voice_client)

        voice_client.play.assert_not_called()

    @pytest.mark.asyncio
    async def test_install_mixer_rechecks_after_ambient_build_before_play(self):
        adapter = _make_adapter()
        state = {"public": True}
        channel = _voice_channel(public=True)
        channel.permissions_for = lambda _role: SimpleNamespace(
            view_channel=state["public"],
            connect=state["public"],
        )
        voice_client = MagicMock()
        voice_client.channel = channel

        async def _build_then_revoke(_fn):
            state["public"] = False
            return b"ambient"

        with patch(
            "plugins.platforms.discord.adapter._discord_public_only_policy_required",
            return_value=True,
        ), patch(
            "plugins.platforms.discord.adapter.asyncio.to_thread",
            new=_build_then_revoke,
        ), pytest.raises(PermissionError, match="not publicly visible"):
            await adapter._install_voice_mixer(111, voice_client)

        voice_client.play.assert_not_called()

    @pytest.mark.asyncio
    async def test_ack_rejects_target_without_public_proof(self):
        adapter = _make_adapter()
        voice_client = MagicMock()
        voice_client.channel = _voice_channel(public=False)
        adapter._voice_clients[111] = voice_client
        adapter._voice_mixers[111] = MagicMock()

        with patch(
            "plugins.platforms.discord.adapter._discord_public_only_policy_required",
            return_value=True,
        ), patch("tools.tts_tool.text_to_speech_tool") as tts:
            ok = await adapter.play_ack_in_voice(111)

        assert ok is False
        tts.assert_not_called()

    @pytest.mark.asyncio
    async def test_playback_rechecks_public_proof_before_egress(self):
        adapter = _make_adapter()
        voice_client = MagicMock()
        voice_client.is_connected.return_value = True
        voice_client.channel = _voice_channel(public=False)
        adapter._voice_clients[111] = voice_client
        adapter._voice_mixers[111] = MagicMock()

        with patch(
            "plugins.platforms.discord.adapter._discord_public_only_policy_required",
            return_value=True,
        ), patch.object(vm, "decode_to_pcm") as decode:
            ok = await adapter.play_in_voice_channel(111, "/tmp/x.mp3")

        assert ok is False
        decode.assert_not_called()
        voice_client.play.assert_not_called()

    @pytest.mark.asyncio
    async def test_playback_revoked_during_decode_never_reaches_mixer(self):
        adapter = _make_adapter()
        state = {"public": True}
        channel = _voice_channel(public=True)
        channel.permissions_for = lambda _role: SimpleNamespace(
            view_channel=state["public"],
            connect=state["public"],
        )
        voice_client = MagicMock()
        voice_client.channel = channel
        voice_client.is_connected.return_value = True
        adapter._voice_clients[111] = voice_client
        mixer = MagicMock()
        adapter._voice_mixers[111] = mixer

        async def _decode_then_revoke(_fn, _path):
            state["public"] = False
            return b"pcm"

        with patch(
            "plugins.platforms.discord.adapter._discord_public_only_policy_required",
            return_value=True,
        ), patch(
            "plugins.platforms.discord.adapter.asyncio.to_thread",
            new=_decode_then_revoke,
        ):
            ok = await adapter.play_in_voice_channel(111, "/tmp/x.mp3")

        assert ok is False
        mixer.play_speech.assert_not_called()
        voice_client.play.assert_not_called()

    @pytest.mark.asyncio
    async def test_post_connect_actual_private_target_is_disconnected(self):
        adapter = _make_adapter()
        requested = _voice_channel(public=True)
        voice_client = MagicMock()
        voice_client.channel = _voice_channel(public=False)
        voice_client.is_playing.return_value = False
        voice_client.disconnect = AsyncMock()
        requested.connect.return_value = voice_client

        with patch(
            "plugins.platforms.discord.adapter._discord_public_only_policy_required",
            return_value=True,
        ), patch("plugins.platforms.discord.adapter.VoiceReceiver") as receiver:
            ok = await adapter.join_voice_channel(requested)

        assert ok is False
        voice_client.disconnect.assert_awaited_once()
        receiver.assert_not_called()
        assert 111 not in adapter._voice_clients
        assert 111 not in adapter._voice_listen_tasks

    @pytest.mark.asyncio
    async def test_server_move_to_private_target_tears_down_existing_voice(self):
        adapter = _make_adapter()
        requested = _voice_channel(public=True, channel_id=333)
        private_actual = _voice_channel(public=False, channel_id=444)
        voice_client = MagicMock()
        voice_client.channel = _voice_channel(public=True, channel_id=222)
        voice_client.is_connected.return_value = True
        voice_client.is_playing.return_value = False
        voice_client.disconnect = AsyncMock()

        async def _move(_channel):
            voice_client.channel = private_actual

        voice_client.move_to = AsyncMock(side_effect=_move)
        adapter._voice_clients[111] = voice_client

        with patch(
            "plugins.platforms.discord.adapter._discord_public_only_policy_required",
            return_value=True,
        ):
            ok = await adapter.join_voice_channel(requested)

        assert ok is False
        voice_client.disconnect.assert_awaited_once()
        assert 111 not in adapter._voice_clients

    @pytest.mark.asyncio
    async def test_mixer_permission_failure_tears_down_receiver_and_client(self):
        adapter = _make_adapter()
        adapter._reset_voice_timeout = MagicMock()
        requested = _voice_channel(public=True)
        voice_client = MagicMock()
        voice_client.channel = requested
        voice_client.is_connected.return_value = True
        voice_client.is_playing.return_value = False
        voice_client.disconnect = AsyncMock()
        requested.connect.return_value = voice_client
        receiver = MagicMock()
        pending_task = MagicMock()

        def _capture_task(coro):
            coro.close()
            return pending_task

        with patch(
            "plugins.platforms.discord.adapter._discord_public_only_policy_required",
            return_value=True,
        ), patch(
            "plugins.platforms.discord.adapter.VoiceReceiver",
            return_value=receiver,
        ), patch(
            "plugins.platforms.discord.adapter.asyncio.ensure_future",
            side_effect=_capture_task,
        ), patch.object(
            adapter,
            "_install_voice_mixer",
            AsyncMock(side_effect=PermissionError("public proof revoked")),
        ):
            ok = await adapter.join_voice_channel(requested)

        assert ok is False
        receiver.start.assert_called_once()
        receiver.stop.assert_called_once()
        pending_task.cancel.assert_called_once()
        voice_client.disconnect.assert_awaited_once()
        assert 111 not in adapter._voice_clients
        assert 111 not in adapter._voice_receivers
        assert 111 not in adapter._voice_listen_tasks

    @pytest.mark.asyncio
    async def test_authoritative_channel_revocation_disconnects_stale_public_client(self):
        adapter = _make_adapter()
        old_public = _voice_channel(public=True)
        updated_private = _voice_channel(public=False)
        voice_client = MagicMock()
        voice_client.channel = old_public
        adapter._voice_clients[111] = voice_client
        adapter.leave_voice_channel = AsyncMock()

        with patch(
            "plugins.platforms.discord.adapter._discord_public_only_policy_required",
            return_value=True,
        ):
            await adapter._handle_voice_channel_policy_update(updated_private)

        adapter.leave_voice_channel.assert_awaited_once_with(111)

    @pytest.mark.asyncio
    async def test_everyone_role_revocation_disconnects_active_voice(self):
        adapter = _make_adapter()
        channel = _voice_channel(public=True)
        voice_client = MagicMock()
        voice_client.channel = channel
        adapter._voice_clients[111] = voice_client
        adapter.leave_voice_channel = AsyncMock()
        revoked_role = SimpleNamespace(id=111, guild=channel.guild)
        channel.permissions_for = lambda role: SimpleNamespace(
            view_channel=role is channel.guild.default_role,
            connect=role is channel.guild.default_role,
        )

        with patch(
            "plugins.platforms.discord.adapter._discord_public_only_policy_required",
            return_value=True,
        ):
            await adapter._handle_voice_role_policy_update(revoked_role)

        adapter.leave_voice_channel.assert_awaited_once_with(111)

    @pytest.mark.asyncio
    async def test_voice_input_rejects_revoked_target_before_transcription(self):
        adapter = _make_adapter()
        voice_client = MagicMock()
        voice_client.channel = _voice_channel(public=False)
        adapter._voice_clients[111] = voice_client
        adapter._voice_input_callback = AsyncMock()

        with patch(
            "plugins.platforms.discord.adapter._discord_public_only_policy_required",
            return_value=True,
        ), patch(
            "plugins.platforms.discord.adapter.VoiceReceiver.pcm_to_wav",
        ) as pcm_to_wav:
            await adapter._process_voice_input(111, 42, b"pcm")

        pcm_to_wav.assert_not_called()
        adapter._voice_input_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_voice_transcript_is_discarded_if_target_revoked_during_stt(self):
        adapter = _make_adapter()
        state = {"public": True}
        channel = _voice_channel(public=True)
        channel.permissions_for = lambda _role: SimpleNamespace(
            view_channel=state["public"],
            connect=state["public"],
        )
        voice_client = MagicMock()
        voice_client.channel = channel
        adapter._voice_clients[111] = voice_client
        adapter._voice_input_callback = AsyncMock()

        def _transcribe_then_revoke(_path):
            state["public"] = False
            return {"success": True, "transcript": "private after STT"}

        with patch(
            "plugins.platforms.discord.adapter._discord_public_only_policy_required",
            return_value=True,
        ), patch(
            "plugins.platforms.discord.adapter.VoiceReceiver.pcm_to_wav",
        ), patch(
            "tools.transcription_tools.transcribe_audio",
            side_effect=_transcribe_then_revoke,
        ):
            await adapter._process_voice_input(111, 42, b"pcm")

        adapter._voice_input_callback.assert_not_awaited()

    def test_voice_context_rejects_closed_or_private_target(self):
        adapter = _make_adapter()
        voice_client = MagicMock()
        voice_client.is_connected.return_value = True
        voice_client.channel = _voice_channel(public=False)
        adapter._voice_clients[111] = voice_client

        with patch(
            "plugins.platforms.discord.adapter._discord_public_only_policy_required",
            return_value=True,
        ):
            assert adapter.get_voice_channel_info(111) is None
            assert adapter.get_voice_channel_context(111) == ""


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

    @pytest.mark.asyncio
    async def test_legacy_wait_yields_until_previous_playback_finishes(self):
        adapter = _make_adapter()
        vc = MagicMock()
        vc.is_connected.return_value = True
        vc.is_playing.side_effect = [True, False]
        adapter._voice_clients[111] = vc
        adapter._reset_voice_timeout = MagicMock()
        sleep = AsyncMock()

        async def _fast(coro, *args, **kwargs):
            if hasattr(coro, "close"):
                coro.close()
            return None

        with patch(
            "plugins.platforms.discord.adapter.asyncio.sleep",
            sleep,
        ), patch(
            "plugins.platforms.discord.adapter.asyncio.wait_for",
            _fast,
        ), patch("plugins.platforms.discord.adapter.discord") as mock_discord:
            mock_discord.FFmpegPCMAudio.return_value = MagicMock()
            mock_discord.PCMVolumeTransformer.return_value = MagicMock()
            ok = await adapter.play_in_voice_channel(111, "/tmp/x.mp3")

        assert ok is True
        sleep.assert_awaited_once_with(0.1)
        vc.play.assert_called_once()


class TestPlayAckInVoice:
    @pytest.mark.asyncio
    async def test_noop_when_ack_disabled(self):
        adapter = _make_adapter({"ack_enabled": False})
        adapter._voice_mixers[111] = MagicMock()
        assert await adapter.play_ack_in_voice(111) is False


