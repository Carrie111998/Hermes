"""Deterministic contracts for conservative Discord VC barge-in."""

import asyncio
import importlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


KOREAN_PHRASES = ("세린아 멈춰", "세린아 잠깐")


def _patch_mixer_decode():
    """Patch the mixer module using the same import fallback as the adapter."""
    try:
        module = importlib.import_module("voice_mixer")
    except ImportError:
        module = importlib.import_module("plugins.platforms.discord.voice_mixer")
    return patch.object(module, "decode_to_pcm", return_value=b"pcm")


class _Receiver:
    def __init__(self):
        self.playback_token = None
        self.pause_calls = 0
        self.resume_calls = 0
        self._paused = False
        self.stopped = False

    def begin_playback_capture(self, token):
        self.playback_token = token
        self._paused = False

    def end_playback_capture(self, token):
        if self.playback_token == token:
            self.playback_token = None

    def pause(self):
        self.pause_calls += 1
        self._paused = True

    def resume(self):
        self.resume_calls += 1
        self._paused = False

    def flush_pending(self, *, with_context=False):
        return []

    def stop(self):
        self.stopped = True


class _Mixer:
    def __init__(self):
        self.active = False
        self.play_speech = MagicMock(side_effect=self._play)
        self.stop_speech = MagicMock(side_effect=self._stop)

    def _play(self, *_args, **_kwargs):
        self.active = True

    def _stop(self):
        self.active = False

    @property
    def speech_active(self):
        return self.active


def _make_adapter(
    *,
    enabled=True,
    monitor_only=False,
    phrases=KOREAN_PHRASES,
    ack_enabled=False,
    stop_ack_phrases=(),
    follow_up_ack_phrases=(),
):
    from gateway.config import Platform, PlatformConfig
    from plugins.platforms.discord.adapter import DiscordAdapter

    adapter = object.__new__(DiscordAdapter)
    adapter.platform = Platform.DISCORD
    adapter.config = PlatformConfig(enabled=True, token="fake-token", extra={})
    adapter._client = MagicMock()
    adapter._voice_clients = {}
    adapter._voice_locks = {}
    adapter._voice_playback_locks = {}
    adapter._voice_text_channels = {}
    adapter._voice_sources = {}
    adapter._voice_timeout_tasks = {}
    adapter._voice_receivers = {}
    adapter._voice_listen_tasks = {}
    adapter._voice_mixers = {}
    adapter._voice_input_callback = AsyncMock()
    adapter._on_voice_disconnect = None
    adapter._allowed_user_ids = set()
    adapter._voice_fx_cfg = {"speech_gain": 1.0, "lead_silence_ms": 0}
    adapter._voice_barge_in_cfg = {
        "enabled": enabled,
        "monitor_only": monitor_only,
        "phrases": tuple(phrases),
        "min_trailing_characters": 2,
        "ack_enabled": ack_enabled,
        "stop_ack_phrases": tuple(stop_ack_phrases),
        "follow_up_ack_phrases": tuple(follow_up_ack_phrases),
    }
    adapter._voice_playback_states = {}
    adapter._voice_playback_serial = 0
    adapter._voice_barge_in_claims = set()
    adapter._voice_barge_in_ack_indices = {"stop": 0, "follow_up": 0}
    adapter._playback_timeout_for_audio = AsyncMock(return_value=30.0)
    adapter._cancel_voice_timeout = MagicMock()
    adapter._reset_voice_timeout = MagicMock()
    adapter._is_allowed_user = MagicMock(return_value=True)
    return adapter


async def _process_transcript(adapter, transcript, *, token=None):
    with (
        patch("plugins.platforms.discord.adapter.VoiceReceiver.pcm_to_wav"),
        patch(
            "tools.transcription_tools.transcribe_audio",
            return_value={"success": True, "transcript": transcript},
        ),
        patch("tools.voice_mode.is_whisper_hallucination", return_value=False),
    ):
        await adapter._process_voice_input(
            111,
            42,
            b"pcm",
            playback_token=token,
        )


def test_phrase_matcher_accepts_stop_only_and_trailing_command():
    from plugins.platforms.discord.adapter import _match_voice_barge_in_phrase

    assert _match_voice_barge_in_phrase("세린아 멈춰!", KOREAN_PHRASES) == (True, "")
    assert _match_voice_barge_in_phrase(
        "세린아 멈춰, 다음 질문에 답해줘", KOREAN_PHRASES
    ) == (True, "다음 질문에 답해줘")


def test_phrase_matcher_rejects_embedded_phrase_and_noise():
    from plugins.platforms.discord.adapter import _match_voice_barge_in_phrase

    assert _match_voice_barge_in_phrase("이제 멈춰도 돼", KOREAN_PHRASES) == (False, "")
    assert _match_voice_barge_in_phrase("세린아", KOREAN_PHRASES) == (False, "")
    assert _match_voice_barge_in_phrase("어...", KOREAN_PHRASES) == (False, "")


@pytest.mark.parametrize("own_name", ("하나야", "유나야", "미나야", "라나야", "세나야"))
def test_phrase_matcher_accepts_only_configured_agent_own_name(own_name):
    from plugins.platforms.discord.adapter import _match_voice_barge_in_phrase

    configured = (own_name,)
    assert _match_voice_barge_in_phrase(own_name, configured) == (True, "")
    for other_name in {"하나야", "유나야", "미나야", "라나야", "세나야"} - {
        own_name
    }:
        assert _match_voice_barge_in_phrase(other_name, configured) == (False, "")


@pytest.mark.asyncio
async def test_own_name_ack_then_fresh_utterance_uses_normal_voice_input_path():
    adapter = _make_adapter(
        phrases=("하나야",),
        ack_enabled=True,
        stop_ack_phrases=("네.",),
    )
    adapter.play_ack_in_voice = AsyncMock(return_value=True)
    playback = adapter._begin_voice_playback(111)

    await _process_transcript(adapter, "하나야", token=playback.token)

    assert playback.interrupted.is_set()
    adapter.play_ack_in_voice.assert_awaited_once_with(111, "네.")
    adapter._voice_input_callback.assert_not_awaited()

    # Playback cleanup ends the tagged epoch. The next separately spoken input
    # is untagged and follows the ordinary voice command path.
    adapter._voice_playback_states.pop(111)
    await _process_transcript(adapter, "내일 날씨 알려줘")

    adapter._voice_input_callback.assert_awaited_once_with(
        guild_id=111,
        user_id=42,
        transcript="내일 날씨 알려줘",
    )


def test_config_is_opt_in_and_keeps_only_nonempty_string_phrases():
    from gateway.config import PlatformConfig
    from plugins.platforms.discord.adapter import DiscordAdapter

    with patch("hermes_cli.config.read_raw_config", return_value={}):
        default_adapter = DiscordAdapter(PlatformConfig(enabled=True, token="x"))
    assert default_adapter._voice_barge_in_cfg == {
        "enabled": False,
        "monitor_only": False,
        "phrases": (),
        "min_trailing_characters": 2,
        "ack_enabled": False,
        "stop_ack_phrases": (),
        "follow_up_ack_phrases": (),
    }
    assert default_adapter._voice_streaming_kws_cfg.enabled is False
    assert default_adapter._voice_streaming_kws_cfg.shadow_only is True

    with patch(
        "hermes_cli.config.read_raw_config",
        return_value={
            "discord": {
                "voice_barge_in": {
                    "enabled": True,
                    "monitor_only": False,
                    "phrases": [" 세린아 멈춰 ", "", 123, "세린아 잠깐"],
                    "min_trailing_characters": 3,
                    "ack_enabled": "yes",
                    "stop_ack_phrases": [
                        " 네, 멈출게요. ",
                        "",
                        123,
                        "네, 멈출게요.",
                    ],
                    "follow_up_ack_phrases": [
                        " 말씀하세요. ",
                        None,
                        "이어갈게요.",
                    ],
                    "streaming_kws": {
                        "enabled": "yes",
                        "shadow_only": "true",
                        "provider": "faster_whisper",
                        "hotword_bias": True,
                        "contrast_wake_names": ["유나야", "라나야"],
                        "num_threads": 2,
                        "queue_frames": 128,
                    },
                }
            }
        },
    ):
        configured = DiscordAdapter(PlatformConfig(enabled=True, token="x"))
    assert configured._voice_barge_in_cfg == {
        "enabled": True,
        "monitor_only": False,
        "phrases": KOREAN_PHRASES,
        "min_trailing_characters": 3,
        "ack_enabled": True,
        "stop_ack_phrases": ("네, 멈출게요.",),
        "follow_up_ack_phrases": ("말씀하세요.", "이어갈게요."),
    }
    assert configured._voice_streaming_kws_cfg.enabled is True
    assert configured._voice_streaming_kws_cfg.shadow_only is True
    assert configured._voice_streaming_kws_cfg.provider == "faster_whisper"
    assert configured._voice_streaming_kws_cfg.hotword_bias is True
    assert configured._voice_streaming_kws_cfg.contrast_wake_names == (
        "유나야",
        "라나야",
    )
    assert configured._voice_streaming_kws_cfg.num_threads == 2
    assert configured._voice_streaming_kws_cfg.queue_frames == 128


def test_monitor_only_normalizes_contradictory_live_and_ack_flags_off():
    from gateway.config import PlatformConfig
    from plugins.platforms.discord.adapter import DiscordAdapter

    with patch(
        "hermes_cli.config.read_raw_config",
        return_value={
            "discord": {
                "voice_barge_in": {
                    "enabled": True,
                    "monitor_only": True,
                    "phrases": ["하나야 멈춰"],
                    "ack_enabled": True,
                }
            }
        },
    ):
        adapter = DiscordAdapter(PlatformConfig(enabled=True, token="x"))

    assert adapter._voice_barge_in_cfg["enabled"] is False
    assert adapter._voice_barge_in_cfg["monitor_only"] is True
    assert adapter._voice_barge_in_cfg["ack_enabled"] is False
    assert adapter._voice_barge_in_enabled() is False
    assert adapter._voice_barge_in_monitor_only() is True
    assert adapter._voice_barge_in_capture_enabled() is True


def test_playback_capture_reactivates_a_started_receiver_left_paused():
    """The real receiver must pass RTP after legacy pause state, unlike mocks."""
    from plugins.platforms.discord.adapter import VoiceReceiver

    vc = MagicMock()
    vc._connection.secret_key = [0] * 32
    vc._connection.dave_session = None
    vc._connection.ssrc = 9999
    vc._connection.hook = None

    receiver = VoiceReceiver(vc)
    receiver.start()
    receiver.pause()
    assert receiver._running is True
    assert receiver._paused is True

    receiver.begin_playback_capture(7)
    receiver._on_packet(b"")

    assert receiver._playback_capture_token == 7
    assert receiver._paused is False
    assert receiver._packet_debug_count == 1


@pytest.mark.asyncio
async def test_ack_phrases_round_robin_deterministically_and_independently_by_kind():
    adapter = _make_adapter(
        ack_enabled=True,
        stop_ack_phrases=("stop one", "stop two"),
        follow_up_ack_phrases=("follow one", "follow two"),
    )
    adapter.play_ack_in_voice = AsyncMock(return_value=True)

    for kind in ("stop", "follow_up", "stop", "stop", "follow_up"):
        assert await adapter._play_voice_barge_in_ack(111, kind) is True

    assert [call.args for call in adapter.play_ack_in_voice.await_args_list] == [
        (111, "stop one"),
        (111, "follow one"),
        (111, "stop two"),
        (111, "stop one"),
        (111, "follow two"),
    ]


@pytest.mark.asyncio
async def test_ack_config_is_independently_opt_in():
    adapter = _make_adapter(
        ack_enabled=False,
        stop_ack_phrases=("stop one",),
        follow_up_ack_phrases=("follow one",),
    )
    adapter.play_ack_in_voice = AsyncMock(return_value=True)

    assert await adapter._play_voice_barge_in_ack(111, "stop") is False
    assert await adapter._play_voice_barge_in_ack(111, "follow_up") is False
    adapter.play_ack_in_voice.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_stop_only_interrupts_playback_without_model_event():
    adapter = _make_adapter(
        ack_enabled=True,
        stop_ack_phrases=("네, 멈출게요.",),
    )
    adapter.play_ack_in_voice = AsyncMock(return_value=True)
    receiver = _Receiver()
    adapter._voice_receivers[111] = receiver

    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = False
    started = asyncio.Event()

    def _play(_source, **_kwargs):
        vc.is_playing.return_value = True
        started.set()

    def _stop():
        vc.is_playing.return_value = False

    vc.play.side_effect = _play
    vc.stop.side_effect = _stop
    vc.disconnect = AsyncMock()
    adapter._voice_clients[111] = vc

    with patch("plugins.platforms.discord.adapter.discord") as discord_mock:
        discord_mock.FFmpegPCMAudio.return_value = MagicMock()
        discord_mock.PCMVolumeTransformer.return_value = MagicMock()
        play_task = asyncio.create_task(adapter.play_in_voice_channel(111, "/tmp/x.mp3"))
        await asyncio.wait_for(started.wait(), timeout=1)
        token = receiver.playback_token
        assert token is not None
        assert receiver._paused is False

        await _process_transcript(adapter, "세린아 멈춰", token=token)
        assert await asyncio.wait_for(play_task, timeout=1) is True

    vc.stop.assert_called_once()
    adapter.play_ack_in_voice.assert_awaited_once_with(111, "네, 멈출게요.")
    adapter._voice_input_callback.assert_not_awaited()
    assert receiver.pause_calls == 0
    assert receiver.playback_token is None
    assert adapter._voice_playback_states == {}


@pytest.mark.asyncio
async def test_mixer_stop_with_trailing_routes_one_clean_input():
    adapter = _make_adapter(
        ack_enabled=True,
        follow_up_ack_phrases=("네, 말씀하세요.",),
    )
    events = []

    async def _ack(*_args):
        events.append("ack")
        return True

    async def _route(**_kwargs):
        events.append("route")

    adapter.play_ack_in_voice = AsyncMock(side_effect=_ack)
    adapter._voice_input_callback = AsyncMock(side_effect=_route)
    receiver = _Receiver()
    mixer = _Mixer()
    vc = MagicMock()
    vc.is_connected.return_value = True
    adapter._voice_receivers[111] = receiver
    adapter._voice_mixers[111] = mixer
    adapter._voice_clients[111] = vc

    with _patch_mixer_decode():
        play_task = asyncio.create_task(adapter.play_in_voice_channel(111, "/tmp/x.mp3"))
        for _ in range(20):
            if receiver.playback_token is not None and mixer.active:
                break
            await asyncio.sleep(0)
        token = receiver.playback_token
        assert token is not None and mixer.active
        assert receiver._paused is False

        await _process_transcript(
            adapter,
            "세린아 잠깐, 다음 질문에 답해줘",
            token=token,
        )
        assert await asyncio.wait_for(play_task, timeout=1) is True

    mixer.stop_speech.assert_called_once()
    adapter.play_ack_in_voice.assert_awaited_once_with(111, "네, 말씀하세요.")
    adapter._voice_input_callback.assert_awaited_once_with(
        guild_id=111,
        user_id=42,
        transcript="다음 질문에 답해줘",
    )
    assert events == ["ack", "route"]
    vc.stop.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("ack_result", [False, RuntimeError("TTS failed")])
async def test_follow_up_ack_failure_fails_open_for_clean_tail(ack_result):
    adapter = _make_adapter(
        ack_enabled=True,
        follow_up_ack_phrases=("네, 말씀하세요.",),
    )
    mixer = _Mixer()
    mixer.active = True
    adapter._voice_mixers[111] = mixer
    state = adapter._begin_voice_playback(111)
    if isinstance(ack_result, Exception):
        ack_mock = AsyncMock(side_effect=ack_result)
    else:
        ack_mock = AsyncMock(return_value=ack_result)
    adapter.play_ack_in_voice = ack_mock

    await _process_transcript(
        adapter,
        "세린아 잠깐, 다음 질문에 답해줘",
        token=state.token,
    )

    assert state.interrupted.is_set()
    ack_mock.assert_awaited_once_with(111, "네, 말씀하세요.")
    adapter._voice_input_callback.assert_awaited_once_with(
        guild_id=111,
        user_id=42,
        transcript="다음 질문에 답해줘",
    )


@pytest.mark.asyncio
async def test_playback_echo_without_phrase_never_reaches_model():
    adapter = _make_adapter()
    state = adapter._begin_voice_playback(111)

    await _process_transcript(
        adapter,
        "이 답변은 스피커에서 다시 들어온 메아리입니다",
        token=state.token,
    )

    adapter._voice_input_callback.assert_not_awaited()
    assert not state.interrupted.is_set()


@pytest.mark.asyncio
async def test_playback_gate_logs_decision_without_transcript_content(caplog):
    adapter = _make_adapter()
    state = adapter._begin_voice_playback(111)
    transcript = "민감한 내용이지만 호출어는 없는 재생 중 발화"
    caplog.set_level("INFO")

    await _process_transcript(adapter, transcript, token=state.token)

    assert "Discord barge-in STT decision" in caplog.text
    assert f"playback={state.token}" in caplog.text
    assert "matched=False" in caplog.text
    assert f"transcript_chars={len(transcript)}" in caplog.text
    assert transcript not in caplog.text


@pytest.mark.asyncio
async def test_short_trailing_fragment_stops_but_is_not_forwarded():
    adapter = _make_adapter()
    mixer = _Mixer()
    mixer.active = True
    adapter._voice_mixers[111] = mixer
    state = adapter._begin_voice_playback(111)

    await _process_transcript(adapter, "세린아 멈춰, 어", token=state.token)

    assert state.interrupted.is_set()
    mixer.stop_speech.assert_called_once()
    adapter._voice_input_callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_barge_for_same_playback_routes_trailing_once():
    adapter = _make_adapter(
        ack_enabled=True,
        follow_up_ack_phrases=("네, 말씀하세요.",),
    )
    adapter.play_ack_in_voice = AsyncMock(return_value=True)
    mixer = _Mixer()
    mixer.active = True
    adapter._voice_mixers[111] = mixer
    state = adapter._begin_voice_playback(111)

    transcript = "세린아 멈춰, 날씨 알려줘"
    await _process_transcript(adapter, transcript, token=state.token)
    await _process_transcript(adapter, transcript, token=state.token)

    mixer.stop_speech.assert_called_once()
    adapter.play_ack_in_voice.assert_awaited_once_with(111, "네, 말씀하세요.")
    adapter._voice_input_callback.assert_awaited_once_with(
        guild_id=111,
        user_id=42,
        transcript="날씨 알려줘",
    )


@pytest.mark.asyncio
async def test_stale_playback_token_cannot_interrupt_or_route_into_newer_playback():
    adapter = _make_adapter()
    vc = MagicMock()
    vc.is_playing.return_value = True
    adapter._voice_clients[111] = vc

    stale = adapter._begin_voice_playback(111)
    current = adapter._begin_voice_playback(111)
    assert stale.token != current.token

    await _process_transcript(
        adapter,
        "세린아 잠깐, 이전 재생에서 늦게 도착한 명령",
        token=stale.token,
    )

    assert current.interrupted.is_set() is False
    vc.stop.assert_not_called()
    adapter._voice_input_callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_playback_token_cannot_ack_or_route_after_state_removal():
    adapter = _make_adapter(
        ack_enabled=True,
        follow_up_ack_phrases=("네, 말씀하세요.",),
    )
    adapter.play_ack_in_voice = AsyncMock(return_value=True)
    expired = adapter._begin_voice_playback(111)
    adapter._voice_playback_states.pop(111)

    await _process_transcript(
        adapter,
        "세린아 잠깐, 재생 종료 뒤 늦게 도착한 명령",
        token=expired.token,
    )

    adapter.play_ack_in_voice.assert_not_awaited()
    adapter._voice_input_callback.assert_not_awaited()
    assert adapter._voice_barge_in_claims == set()


@pytest.mark.asyncio
async def test_disconnect_interrupts_waiter_and_cleans_playback_state():
    adapter = _make_adapter()
    receiver = _Receiver()
    adapter._voice_receivers[111] = receiver

    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = False
    started = asyncio.Event()

    def _play(_source, **_kwargs):
        vc.is_playing.return_value = True
        started.set()

    def _stop():
        vc.is_playing.return_value = False

    vc.play.side_effect = _play
    vc.stop.side_effect = _stop
    vc.disconnect = AsyncMock()
    adapter._voice_clients[111] = vc

    with patch("plugins.platforms.discord.adapter.discord") as discord_mock:
        discord_mock.FFmpegPCMAudio.return_value = MagicMock()
        discord_mock.PCMVolumeTransformer.return_value = MagicMock()
        play_task = asyncio.create_task(adapter.play_in_voice_channel(111, "/tmp/x.mp3"))
        await asyncio.wait_for(started.wait(), timeout=1)
        await adapter.leave_voice_channel(111)
        assert await asyncio.wait_for(play_task, timeout=1) is True

    assert adapter._voice_playback_states == {}
    assert receiver.stopped is True
    assert receiver.playback_token is None
    vc.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_disabled_default_pauses_receiver_during_mixer_speech():
    adapter = _make_adapter(enabled=False)
    receiver = _Receiver()
    mixer = _Mixer()
    vc = MagicMock()
    vc.is_connected.return_value = True
    adapter._voice_receivers[111] = receiver
    adapter._voice_mixers[111] = mixer
    adapter._voice_clients[111] = vc

    with _patch_mixer_decode():
        play_task = asyncio.create_task(adapter.play_in_voice_channel(111, "/tmp/x.mp3"))
        for _ in range(20):
            if mixer.active:
                break
            await asyncio.sleep(0)
        assert mixer.active
        assert receiver.pause_calls == 1
        assert receiver._paused is True
        assert receiver.playback_token is None
        mixer.stop_speech()
        assert await asyncio.wait_for(play_task, timeout=1) is True

    assert receiver.resume_calls == 1
    assert receiver._paused is False
    adapter._voice_input_callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_monitor_only_captures_without_interrupt_ack_or_model_event(caplog):
    adapter = _make_adapter(enabled=False, monitor_only=True, ack_enabled=True)
    receiver = _Receiver()
    mixer = _Mixer()
    vc = MagicMock()
    vc.is_connected.return_value = True
    adapter._voice_receivers[111] = receiver
    adapter._voice_mixers[111] = mixer
    adapter._voice_clients[111] = vc
    adapter.play_ack_in_voice = AsyncMock(return_value=True)
    caplog.set_level("INFO")

    with _patch_mixer_decode():
        play_task = asyncio.create_task(adapter.play_in_voice_channel(111, "/tmp/x.mp3"))
        for _ in range(20):
            if receiver.playback_token is not None and mixer.active:
                break
            await asyncio.sleep(0)
        token = receiver.playback_token
        assert token is not None

        await _process_transcript(
            adapter,
            "세린아 멈춰, 날씨 알려줘",
            token=token,
        )

        assert mixer.active is True
        mixer.stop_speech()
        assert await asyncio.wait_for(play_task, timeout=1) is True

    adapter.play_ack_in_voice.assert_not_awaited()
    adapter._voice_input_callback.assert_not_awaited()
    assert "Discord barge-in monitor-only" in caplog.text
    assert "matched=True" in caplog.text
    assert "날씨 알려줘" not in caplog.text


@pytest.mark.asyncio
async def test_monitor_only_fail_closed_when_runtime_dict_enables_both_modes():
    adapter = _make_adapter(enabled=True, monitor_only=True, ack_enabled=True)
    mixer = _Mixer()
    mixer.active = True
    adapter._voice_mixers[111] = mixer
    state = adapter._begin_voice_playback(111)
    adapter.play_ack_in_voice = AsyncMock(return_value=True)

    await _process_transcript(
        adapter,
        "세린아 멈춰, 날씨 알려줘",
        token=state.token,
    )

    assert state.interrupted.is_set() is False
    mixer.stop_speech.assert_not_called()
    adapter.play_ack_in_voice.assert_not_awaited()
    adapter._voice_input_callback.assert_not_awaited()


def test_streaming_kws_alone_enables_playback_capture():
    from plugins.platforms.discord.streaming_kws import StreamingKwsConfig

    adapter = _make_adapter(enabled=False, monitor_only=False)
    adapter._voice_streaming_kws_cfg = StreamingKwsConfig(
        enabled=True,
        shadow_only=True,
    )

    assert adapter._voice_barge_in_enabled() is False
    assert adapter._voice_barge_in_monitor_only() is False
    assert adapter._voice_streaming_kws_enabled() is True
    assert adapter._voice_barge_in_capture_enabled() is True


def test_streaming_kws_shadow_detection_has_no_side_effects():
    from plugins.platforms.discord.streaming_kws import StreamingKwsConfig

    adapter = _make_adapter(enabled=False, monitor_only=False)
    adapter._voice_streaming_kws_cfg = StreamingKwsConfig(
        enabled=True,
        shadow_only=True,
    )
    adapter._client.get_guild.return_value = MagicMock()
    adapter._is_allowed_user = MagicMock(return_value=True)
    mixer = _Mixer()
    mixer.active = True
    adapter._voice_mixers[111] = mixer
    state = adapter._begin_voice_playback(111)

    adapter._handle_voice_streaming_kws_detection(
        {
            "guild_id": 111,
            "token": state.token,
            "user_id": 42,
            "keyword_index": 0,
            "latency_ms": 620,
            "audio_ms": 880,
            "queue_delay_ms": 4,
        }
    )

    assert state.interrupted.is_set() is False
    mixer.stop_speech.assert_not_called()
    adapter._voice_input_callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_streaming_kws_shadow_endpoint_skips_wav_and_batch_stt():
    from plugins.platforms.discord.streaming_kws import StreamingKwsConfig

    adapter = _make_adapter(enabled=False, monitor_only=False)
    adapter._voice_streaming_kws_cfg = StreamingKwsConfig(
        enabled=True,
        shadow_only=True,
    )
    state = adapter._begin_voice_playback(111)

    with (
        patch("plugins.platforms.discord.adapter.tempfile.NamedTemporaryFile") as temp,
        patch("plugins.platforms.discord.adapter.VoiceReceiver.pcm_to_wav") as pcm_to_wav,
        patch("tools.transcription_tools.transcribe_audio") as transcribe,
    ):
        await adapter._process_voice_input(
            111,
            42,
            b"private pcm",
            playback_token=state.token,
        )

    temp.assert_not_called()
    pcm_to_wav.assert_not_called()
    transcribe.assert_not_called()
    adapter._voice_input_callback.assert_not_awaited()


def test_parent_monitor_only_forces_nested_streaming_live_to_shadow():
    from plugins.platforms.discord.streaming_kws import StreamingKwsConfig

    adapter = _make_adapter(enabled=False, monitor_only=True)
    adapter._voice_streaming_kws_cfg = StreamingKwsConfig(
        enabled=True,
        shadow_only=False,
    )
    adapter._client.get_guild.return_value = MagicMock()
    adapter._is_allowed_user = MagicMock(return_value=True)
    mixer = _Mixer()
    mixer.active = True
    adapter._voice_mixers[111] = mixer
    state = adapter._begin_voice_playback(111)

    adapter._handle_voice_streaming_kws_detection(
        {"guild_id": 111, "token": state.token, "user_id": 42}
    )

    assert state.interrupted.is_set() is False
    mixer.stop_speech.assert_not_called()
    assert (111, state.token) not in getattr(
        adapter,
        "_voice_streaming_kws_live_tokens",
        {},
    )


def test_streaming_kws_live_detection_interrupts_once_without_claiming_follow_up():
    from plugins.platforms.discord.streaming_kws import StreamingKwsConfig

    adapter = _make_adapter(enabled=False, monitor_only=False)
    adapter._voice_streaming_kws_cfg = StreamingKwsConfig(
        enabled=True,
        shadow_only=False,
    )
    adapter._client.get_guild.return_value = MagicMock()
    adapter._is_allowed_user = MagicMock(return_value=True)
    mixer = _Mixer()
    mixer.active = True
    adapter._voice_mixers[111] = mixer
    state = adapter._begin_voice_playback(111)
    event = {
        "guild_id": 111,
        "token": state.token,
        "user_id": 42,
        "keyword_index": 0,
        "latency_ms": 620,
        "audio_ms": 880,
        "queue_delay_ms": 4,
    }

    adapter._handle_voice_streaming_kws_detection(event)
    adapter._handle_voice_streaming_kws_detection(event)

    assert state.interrupted.is_set()
    mixer.stop_speech.assert_called_once()
    assert adapter._voice_barge_in_claims == set()
    assert (111, state.token) in adapter._voice_streaming_kws_live_tokens
    adapter._voice_input_callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_streaming_kws_live_token_routes_only_trailing_follow_up():
    from plugins.platforms.discord.streaming_kws import StreamingKwsConfig

    adapter = _make_adapter(enabled=False, monitor_only=False)
    adapter._voice_streaming_kws_cfg = StreamingKwsConfig(
        enabled=True,
        shadow_only=False,
    )
    adapter._client.get_guild.return_value = MagicMock()
    adapter._is_allowed_user = MagicMock(return_value=True)
    mixer = _Mixer()
    mixer.active = True
    adapter._voice_mixers[111] = mixer
    state = adapter._begin_voice_playback(111)
    adapter._handle_voice_streaming_kws_detection(
        {"guild_id": 111, "token": state.token, "user_id": 42}
    )

    await _process_transcript(
        adapter,
        "세린아 멈춰, 날씨 알려줘",
        token=state.token,
    )

    adapter._voice_input_callback.assert_awaited_once_with(
        guild_id=111,
        user_id=42,
        transcript="날씨 알려줘",
    )
    assert (111, state.token) not in adapter._voice_streaming_kws_live_tokens


def test_streaming_kws_rejects_unauthorized_and_stale_detection():
    from plugins.platforms.discord.streaming_kws import StreamingKwsConfig

    adapter = _make_adapter(enabled=False, monitor_only=False)
    adapter._voice_streaming_kws_cfg = StreamingKwsConfig(
        enabled=True,
        shadow_only=False,
    )
    adapter._client.get_guild.return_value = MagicMock()
    adapter._is_allowed_user = MagicMock(return_value=False)
    mixer = _Mixer()
    mixer.active = True
    adapter._voice_mixers[111] = mixer
    state = adapter._begin_voice_playback(111)

    adapter._handle_voice_streaming_kws_detection(
        {"guild_id": 111, "token": state.token, "user_id": 42}
    )
    adapter._is_allowed_user.return_value = True
    adapter._handle_voice_streaming_kws_detection(
        {"guild_id": 111, "token": state.token + 1, "user_id": 42}
    )

    assert state.interrupted.is_set() is False
    mixer.stop_speech.assert_not_called()


@pytest.mark.asyncio
async def test_monitor_only_records_stale_playback_match_before_return(caplog):
    adapter = _make_adapter(enabled=False, monitor_only=True)
    old_state = adapter._begin_voice_playback(111)
    current_state = adapter._begin_voice_playback(111)
    caplog.set_level("INFO")

    await _process_transcript(
        adapter,
        "세린아 멈춰, 날씨 알려줘",
        token=old_state.token,
    )

    assert current_state.interrupted.is_set() is False
    adapter._voice_input_callback.assert_not_awaited()
    assert f"playback={old_state.token}" in caplog.text
    assert "matched=True" in caplog.text
    assert "Discord barge-in monitor-only" in caplog.text


@pytest.mark.asyncio
async def test_monitor_only_stt_exception_logs_bounded_outcome(caplog):
    adapter = _make_adapter(enabled=False, monitor_only=True)
    state = adapter._begin_voice_playback(111)
    caplog.set_level("INFO")

    with (
        patch("plugins.platforms.discord.adapter.VoiceReceiver.pcm_to_wav"),
        patch(
            "tools.transcription_tools.transcribe_audio",
            side_effect=RuntimeError("sensitive provider detail"),
        ),
        patch("tools.voice_mode.is_whisper_hallucination", return_value=False),
    ):
        await adapter._process_voice_input(
            111,
            42,
            b"pcm",
            playback_token=state.token,
        )

    decision_logs = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("Discord barge-in STT decision")
    ]
    assert len(decision_logs) == 1
    assert "outcome=exception" in decision_logs[0]
    assert "stage=stt" in decision_logs[0]
    assert "type=RuntimeError" in decision_logs[0]
    assert "sensitive provider detail" not in decision_logs[0]
    adapter._voice_input_callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_queued_playback_does_not_start_after_disconnect():
    adapter = _make_adapter()
    vc = MagicMock()
    vc.is_connected.return_value = True
    adapter._voice_clients[111] = vc

    lock = asyncio.Lock()
    await lock.acquire()
    adapter._voice_playback_locks[111] = lock
    play_task = asyncio.create_task(adapter.play_in_voice_channel(111, "/tmp/x.mp3"))
    await asyncio.sleep(0)

    adapter._voice_clients.pop(111)
    vc.is_connected.return_value = False
    lock.release()

    assert await asyncio.wait_for(play_task, timeout=1) is False
    vc.play.assert_not_called()
    adapter._playback_timeout_for_audio.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_ack_uses_shared_playback_path_without_tool_ack_or_mixer():
    adapter = _make_adapter()
    adapter._voice_fx_cfg["ack_enabled"] = False
    adapter.play_in_voice_channel = AsyncMock(return_value=True)

    def _tts(*, text, output_path):
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"audio")
        return json.dumps({"success": True, "file_path": output_path})

    with patch("tools.tts_tool.text_to_speech_tool", side_effect=_tts):
        assert await adapter.play_ack_in_voice(111, "잠깐 볼게요") is True

    adapter.play_in_voice_channel.assert_awaited_once()
    guild_id, audio_path = adapter.play_in_voice_channel.await_args.args
    assert guild_id == 111
    assert audio_path.endswith(".mp3")


@pytest.mark.asyncio
async def test_normal_nonplayback_voice_behavior_is_preserved():
    adapter = _make_adapter()

    await _process_transcript(adapter, "평소 음성 질문", token=None)

    adapter._voice_input_callback.assert_awaited_once_with(
        guild_id=111,
        user_id=42,
        transcript="평소 음성 질문",
    )
