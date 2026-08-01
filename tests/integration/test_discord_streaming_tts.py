import asyncio
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.asyncio

discord = pytest.importorskip("discord", reason="discord.py required for Discord streaming TTS tests")

from gateway.config import PlatformConfig
from gateway.platforms.base import AudioFormat
from plugins.platforms.discord.adapter import (
    DiscordAdapter,
    _DiscordStreamingPCMSource,
    _DiscordStreamingTTSHandle,
)


class _FakeVoiceClient:
    def __init__(self):
        self._connected = True
        self._playing = False
        self.source = None
        self.after = None
        self.play_calls = 0
        self.stop_calls = 0

    def is_connected(self):
        return self._connected

    def is_playing(self):
        return self._playing

    def play(self, source, after=None):
        self.play_calls += 1
        self._playing = True
        self.source = source
        self.after = after

    def stop(self):
        self.stop_calls += 1
        was_playing = self._playing
        self._playing = False
        if was_playing and self.after is not None:
            self.after(None)


class _FakeReceiver:
    def __init__(self):
        self.paused = 0
        self.resumed = 0

    def pause(self):
        self.paused += 1

    def resume(self):
        self.resumed += 1


def _make_adapter():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="integration-test-token"))
    adapter._client = SimpleNamespace(user=SimpleNamespace(id=999))
    adapter._voice_fx_cfg["lead_silence_ms"] = 0
    return adapter


async def test_streaming_pcm_source_converts_24k_mono_to_48k_stereo():
    source = _DiscordStreamingPCMSource(AudioFormat(sample_rate=24000, channels=1, sample_width=2))
    source.append_pcm((b"\x01\x00") * 480)
    frame = source.read()
    assert len(frame) == _DiscordStreamingPCMSource.FRAME_SIZE
    source.finish()


async def test_discord_begin_streaming_tts_starts_voice_playback_and_resumes_receiver_on_stop():
    adapter = _make_adapter()
    vc = _FakeVoiceClient()
    receiver = _FakeReceiver()
    adapter._voice_text_channels = {123: 456}
    adapter._voice_clients = {123: vc}
    setattr(adapter, "_voice_receivers", {123: receiver})

    handle = await adapter.begin_streaming_tts(
        "456",
        AudioFormat(sample_rate=24000, channels=1, sample_width=2),
    )

    assert isinstance(handle, _DiscordStreamingTTSHandle)
    assert vc.play_calls == 1
    assert receiver.paused == 1
    assert vc.source is not None

    await adapter.write_streaming_tts(handle, (b"\x02\x00") * 480)
    frame = vc.source.read()
    assert len(frame) == _DiscordStreamingPCMSource.FRAME_SIZE

    await adapter.finish_streaming_tts(handle)
    vc.stop()
    await asyncio.sleep(0.05)
    assert receiver.resumed == 1


async def test_discord_supports_streaming_tts_only_for_voice_linked_chat():
    adapter = _make_adapter()
    adapter._voice_text_channels = {321: 654}
    adapter._voice_clients = {321: _FakeVoiceClient()}

    assert adapter.supports_streaming_tts(
        "654", AudioFormat(sample_rate=24000, channels=1, sample_width=2)
    )
    assert not adapter.supports_streaming_tts(
        "999", AudioFormat(sample_rate=24000, channels=1, sample_width=2)
    )
    assert not adapter.supports_streaming_tts(
        "654", AudioFormat(sample_rate=22050, channels=1, sample_width=2)
    )
