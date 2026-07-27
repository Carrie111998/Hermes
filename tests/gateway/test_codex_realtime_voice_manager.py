from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from gateway.codex_realtime_voice import CodexRealtimeVoiceManager


def test_discord_yaml_bridge_preserves_realtime_voice_config():
    from plugins.platforms.discord.adapter import _apply_yaml_config

    raw = {
        "codex_realtime_voice": {
            "enabled": True,
            "user_id": "42",
            "fallback_to_classic": True,
        }
    }
    seeded = _apply_yaml_config({"discord": raw}, raw)

    assert seeded is not None
    assert seeded["codex_realtime_voice"] == raw["codex_realtime_voice"]


class FakeSession:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.active = False
        self.pcm: list[bytes] = []
        self.spoken: list[tuple[str, int | None]] = []
        self.stopped = False

    async def start(self, *, voice=None):
        self.active = True
        self.voice = voice
        return SimpleNamespace(protocol_version="v3", voices=("cedar",))

    def push_discord_pcm(self, pcm: bytes) -> bool:
        if not self.active:
            return False
        self.pcm.append(pcm)
        return True

    async def append_speech(
        self, text: str, *, transcript_generation: int | None = None
    ) -> bool:
        if not self.active:
            return False
        self.spoken.append((text, transcript_generation))
        return True

    async def stop(self):
        self.active = False
        self.stopped = True


class FakeAdapter:
    def __init__(self, config: dict) -> None:
        self.config = SimpleNamespace(extra={"codex_realtime_voice": config})
        self.output_pcm: list[tuple[int, bytes]] = []
        self.mixer_started: list[int] = []
        self.stream_ended: list[int] = []
        self.output_ready = True
        self._profile_name = None

    async def ensure_realtime_voice_output(self, guild_id: int) -> bool:
        self.mixer_started.append(guild_id)
        return self.output_ready

    def push_realtime_voice_pcm(self, guild_id: int, pcm: bytes) -> bool:
        self.output_pcm.append((guild_id, pcm))
        return True

    def end_realtime_voice_output(self, guild_id: int) -> None:
        self.stream_ended.append(guild_id)


@pytest.mark.asyncio
async def test_disabled_config_preserves_classic_voice_without_session():
    created: list[FakeSession] = []
    manager = CodexRealtimeVoiceManager(
        session_factory=lambda **kwargs: (
            created.append(FakeSession(**kwargs)) or created[-1]
        ),
        dependency_ensurer=lambda: None,
    )
    adapter = FakeAdapter({"enabled": False})

    result = await manager.start_for_voice_channel(
        adapter=adapter,
        guild_id=7,
        user_id=42,
        on_transcript=lambda *_: None,
    )

    assert result.enabled is False
    assert result.active is False
    assert result.fallback_to_classic is True
    assert created == []


@pytest.mark.asyncio
async def test_webrtc_rejects_v2_protocol_without_starting_session():
    created: list[FakeSession] = []
    manager = CodexRealtimeVoiceManager(
        session_factory=lambda **kwargs: (
            created.append(FakeSession(**kwargs)) or created[-1]
        ),
        dependency_ensurer=lambda: None,
    )
    adapter = FakeAdapter({
        "enabled": True,
        "user_id": "42",
        "protocol_version": "v2",
    })

    result = await manager.start_for_voice_channel(
        adapter=adapter,
        guild_id=7,
        user_id=42,
        on_transcript=lambda *_: None,
    )

    assert result.enabled is True
    assert result.active is False
    assert "protocol_version must be v1 or v3" in str(result.reason)
    assert created == []


@pytest.mark.asyncio
async def test_enabled_route_requires_the_configured_single_user():
    manager = CodexRealtimeVoiceManager(
        session_factory=FakeSession,
        dependency_ensurer=lambda: None,
    )
    adapter = FakeAdapter({
        "enabled": True,
        "user_id": "42",
        "voice": "cedar",
        "spoken_language": "nl-NL",
    })

    rejected = await manager.start_for_voice_channel(
        adapter=adapter,
        guild_id=7,
        user_id=99,
        on_transcript=lambda *_: None,
    )
    assert rejected.enabled is True
    assert rejected.active is False
    assert rejected.fallback_to_classic is True

    accepted = await manager.start_for_voice_channel(
        adapter=adapter,
        guild_id=7,
        user_id=42,
        on_transcript=lambda *_: None,
    )
    assert accepted.active is True
    assert accepted.capabilities.protocol_version == "v3"
    active_session = manager.session_for(adapter, 7)
    assert active_session is not None
    assert active_session.kwargs["protocol_version"] == "v3"
    assert active_session.kwargs["spoken_language"] == "nl-NL"
    assert adapter.mixer_started == [7]

    # While realtime is active, non-bound speakers are consumed/dropped rather
    # than leaking into a second classic-STT conversation path.
    assert manager.push_discord_pcm(adapter, 7, 99, b"no") is True
    assert manager.push_discord_pcm(adapter, 7, 0, b"unmapped") is True
    assert manager.push_discord_pcm(adapter, 7, 42, b"yes") is True
    session = manager.session_for(adapter, 7)
    assert session.pcm == [b"yes"]

    assert (
        await manager.append_speech(adapter, 7, "Hoi", transcript_generation=9) is True
    )
    assert adapter.mixer_started == [7, 7]
    assert session.spoken == [("Hoi", 9)]

    await manager.stop_for_voice_channel(adapter, 7)
    assert session.stopped is True
    assert adapter.stream_ended == [7]


@pytest.mark.asyncio
async def test_append_speech_fails_fast_when_discord_output_cannot_be_restored():
    from agent.transports.codex_realtime_voice import CodexRealtimeUnavailable

    manager = CodexRealtimeVoiceManager(
        session_factory=FakeSession,
        dependency_ensurer=lambda: None,
    )
    adapter = FakeAdapter({"enabled": True, "user_id": "42"})
    started = await manager.start_for_voice_channel(
        adapter=adapter,
        guild_id=7,
        user_id=42,
        on_transcript=lambda *_: None,
    )
    assert started.active is True
    adapter.output_ready = False

    with pytest.raises(CodexRealtimeUnavailable, match="mixer is unavailable"):
        await manager.append_speech(adapter, 7, "Hoi")


@pytest.mark.asyncio
async def test_transcript_is_stamped_with_bound_user_and_output_reaches_adapter():
    sessions: list[FakeSession] = []
    manager = CodexRealtimeVoiceManager(
        session_factory=lambda **kwargs: (
            sessions.append(FakeSession(**kwargs)) or sessions[-1]
        ),
        dependency_ensurer=lambda: None,
    )
    adapter = FakeAdapter({"enabled": True, "user_id": 42})
    transcripts: list[tuple[int, str, int]] = []

    result = await manager.start_for_voice_channel(
        adapter=adapter,
        guild_id=7,
        user_id=42,
        on_transcript=lambda user_id, text, generation: transcripts.append((
            user_id,
            text,
            generation,
        )),
    )
    assert result.active is True
    session = sessions[0]

    session.kwargs["on_user_transcript"]("wat is de status", 12)
    session.kwargs["on_output_pcm"](b"pcm")

    assert transcripts == [(42, "wat is de status", 12)]
    assert adapter.output_pcm == [(7, b"pcm")]


@pytest.mark.asyncio
async def test_runtime_failure_releases_session_and_realtime_mixer_stream():
    sessions: list[FakeSession] = []
    manager = CodexRealtimeVoiceManager(
        session_factory=lambda **kwargs: (
            sessions.append(FakeSession(**kwargs)) or sessions[-1]
        ),
        dependency_ensurer=lambda: None,
    )
    adapter = FakeAdapter({
        "enabled": True,
        "user_id": 42,
        "fallback_to_classic": False,
    })
    failures: list[tuple[str, bool]] = []

    result = await manager.start_for_voice_channel(
        adapter=adapter,
        guild_id=7,
        user_id=42,
        on_transcript=lambda *_: None,
        on_runtime_failure=lambda reason, fallback: failures.append((reason, fallback)),
    )
    assert result.active is True

    sessions[0].kwargs["on_error"]("WebRTC connection failed")
    await asyncio.sleep(0.05)

    assert sessions[0].stopped is True
    assert manager.session_for(adapter, 7) is None
    assert adapter.stream_ended == [7]
    assert failures == [("WebRTC connection failed", False)]


@pytest.mark.asyncio
async def test_stale_runtime_failure_cannot_stop_a_replacement_session():
    sessions: list[FakeSession] = []
    manager = CodexRealtimeVoiceManager(
        session_factory=lambda **kwargs: (
            sessions.append(FakeSession(**kwargs)) or sessions[-1]
        ),
        dependency_ensurer=lambda: None,
    )
    adapter = FakeAdapter({"enabled": True, "user_id": 42})
    failures: list[tuple[str, bool]] = []

    first = await manager.start_for_voice_channel(
        adapter=adapter,
        guild_id=7,
        user_id=42,
        on_transcript=lambda *_: None,
        on_runtime_failure=lambda reason, fallback: failures.append((reason, fallback)),
    )
    assert first.active is True
    stale_error = sessions[0].kwargs["on_error"]

    await manager.stop_for_voice_channel(adapter, 7)
    second = await manager.start_for_voice_channel(
        adapter=adapter,
        guild_id=7,
        user_id=42,
        on_transcript=lambda *_: None,
        on_runtime_failure=lambda reason, fallback: failures.append((reason, fallback)),
    )
    assert second.active is True

    stale_error("old WebRTC connection failed")
    await asyncio.sleep(0.05)

    assert manager.session_for(adapter, 7) is sessions[1]
    assert sessions[1].stopped is False
    assert failures == []


@pytest.mark.asyncio
async def test_start_failure_falls_back_and_cleans_partial_session():
    class BrokenSession(FakeSession):
        async def start(self, *, voice=None):
            raise RuntimeError(
                "experimental API unavailable at https://secret.example/realtime, "
                "request id: deadbeef"
            )

    manager = CodexRealtimeVoiceManager(
        session_factory=BrokenSession,
        dependency_ensurer=lambda: None,
    )
    adapter = FakeAdapter({"enabled": True, "user_id": 42, "fallback_to_classic": True})

    result = await manager.start_for_voice_channel(
        adapter=adapter,
        guild_id=7,
        user_id=42,
        on_transcript=lambda *_: None,
    )

    assert result.enabled is True
    assert result.active is False
    assert result.fallback_to_classic is True
    assert result.reason is not None
    assert "secret.example" not in result.reason
    assert "deadbeef" not in result.reason
    assert manager.session_for(adapter, 7) is None


@pytest.mark.asyncio
async def test_pcm_is_consumed_during_startup_to_avoid_parallel_classic_buffer():
    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowSession(FakeSession):
        async def start(self, *, voice=None):
            entered.set()
            await release.wait()
            return await super().start(voice=voice)

    manager = CodexRealtimeVoiceManager(
        session_factory=SlowSession,
        dependency_ensurer=lambda: None,
    )
    adapter = FakeAdapter({"enabled": True, "user_id": 42})
    startup = asyncio.create_task(
        manager.start_for_voice_channel(
            adapter=adapter,
            guild_id=7,
            user_id=42,
            on_transcript=lambda *_: None,
        )
    )
    await entered.wait()

    assert manager.push_discord_pcm(adapter, 7, 42, b"during-start") is True
    assert manager.push_discord_pcm(adapter, 7, 99, b"other-speaker") is True
    session = manager.session_for(adapter, 7)
    assert session is not None
    assert session.pcm == []

    release.set()
    result = await startup
    assert result.active is True


def test_pcm_is_reserved_before_discord_join_starts():
    manager = CodexRealtimeVoiceManager(
        session_factory=FakeSession,
        dependency_ensurer=lambda: None,
    )
    adapter = FakeAdapter({"enabled": True, "user_id": 42})

    assert manager.prepare_for_voice_channel(adapter, 7) is True
    assert manager.push_discord_pcm(adapter, 7, 42, b"early") is True
    manager.cancel_voice_channel_start(adapter, 7)
    assert manager.push_discord_pcm(adapter, 7, 42, b"classic") is False


@pytest.mark.asyncio
async def test_no_fallback_failure_consumes_pcm_until_slow_cleanup_finishes():
    stop_entered = asyncio.Event()
    release_stop = asyncio.Event()

    class SlowStopSession(FakeSession):
        async def stop(self):
            self.active = False
            stop_entered.set()
            await release_stop.wait()
            self.stopped = True

    sessions: list[SlowStopSession] = []
    manager = CodexRealtimeVoiceManager(
        session_factory=lambda **kwargs: (
            sessions.append(SlowStopSession(**kwargs)) or sessions[-1]
        ),
        dependency_ensurer=lambda: None,
    )
    adapter = FakeAdapter({
        "enabled": True,
        "user_id": 42,
        "fallback_to_classic": False,
    })
    result = await manager.start_for_voice_channel(
        adapter=adapter,
        guild_id=7,
        user_id=42,
        on_transcript=lambda *_: None,
    )
    assert result.active is True

    sessions[0].push_discord_pcm = lambda _pcm: False
    assert manager.push_discord_pcm(adapter, 7, 42, b"input-failure") is True
    sessions[0].active = False
    sessions[0].kwargs["on_error"]("WebRTC failed")
    await stop_entered.wait()
    assert manager.push_discord_pcm(adapter, 7, 42, b"during-cleanup") is True

    release_stop.set()
    await asyncio.sleep(0.05)
    assert manager.session_for(adapter, 7) is None
    assert manager.push_discord_pcm(adapter, 7, 42, b"before-disconnect") is True


@pytest.mark.asyncio
async def test_close_racing_start_cannot_publish_a_late_active_session():
    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowSession(FakeSession):
        async def start(self, *, voice=None):
            entered.set()
            await release.wait()
            return await super().start(voice=voice)

    manager = CodexRealtimeVoiceManager(
        session_factory=SlowSession,
        dependency_ensurer=lambda: None,
    )
    adapter = FakeAdapter({"enabled": True, "user_id": 42})
    startup = asyncio.create_task(
        manager.start_for_voice_channel(
            adapter=adapter,
            guild_id=7,
            user_id=42,
            on_transcript=lambda *_: None,
        )
    )
    await entered.wait()

    await manager.close()
    release.set()
    result = await startup

    assert result.active is False
    assert "shutting down" in str(result.reason)
    assert manager.session_for(adapter, 7) is None
    assert adapter.stream_ended == [7]
