from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from agent import realtime_voice_registry
from agent.realtime_voice import (
    HeardAudioBoundary,
    RealtimeEvent,
    RealtimeEventType,
    RealtimeSession,
    RealtimeVoiceProvider,
)
from agent.realtime_voice_coordinator import RealtimeVoiceCoordinator


class FakeSession(RealtimeSession):
    def __init__(self, events: list[RealtimeEvent]) -> None:
        self._events = events
        self.audio: list[bytes] = []
        self.tool_results: list[tuple[str, str]] = []
        self.cancelled = False
        self.cancellation_boundaries: list[HeardAudioBoundary | None] = []
        self.cancellation_operations: list[str] = []
        self.closed = False

    async def send_audio(self, pcm: bytes) -> None:
        self.audio.append(pcm)

    async def events(self) -> AsyncIterator[RealtimeEvent]:
        for event in self._events:
            yield event

    async def submit_tool_result(self, call_id: str, output: str) -> None:
        self.tool_results.append((call_id, output))

    async def cancel_response(self) -> None:
        self.cancelled = True
        self.cancellation_operations.append("cancel")

    async def close(self) -> None:
        self.closed = True


class FakeProvider(RealtimeVoiceProvider):
    def __init__(self, name: str, session: FakeSession) -> None:
        self._name = name
        self.session = session
        self.opened_with: dict[str, Any] | None = None

    @property
    def name(self) -> str:
        return self._name

    async def open_session(self, *, instructions, tools, voice=None):
        self.opened_with = {"instructions": instructions, "tools": tools, "voice": voice}
        return self.session


class BoundarySession(FakeSession):
    async def truncate_response(self, boundary: HeardAudioBoundary) -> None:
        self.cancellation_boundaries.append(boundary)
        self.cancellation_operations.append("truncate")


class LegacyCancelSession(FakeSession):
    async def cancel_response(self) -> None:
        self.cancelled = True
        self.cancellation_boundaries.append(None)


@pytest.fixture(autouse=True)
def reset_registry():
    realtime_voice_registry._reset_for_tests()
    yield
    realtime_voice_registry._reset_for_tests()


def test_registry_is_profile_scoped_and_accepts_two_providers():
    alpha = FakeProvider("alpha", FakeSession([]))
    beta = FakeProvider("beta", FakeSession([]))
    realtime_voice_registry.register_provider(alpha, scope="home-a")
    realtime_voice_registry.register_provider(beta, scope="home-a")

    assert realtime_voice_registry.get_provider(" ALPHA ", scope="home-a") is alpha
    assert [provider.name for provider in realtime_voice_registry.list_providers(scope="home-a")] == [
        "alpha",
        "beta",
    ]
    assert realtime_voice_registry.get_provider("alpha", scope="home-b") is None


def test_registry_rejects_invalid_provider_and_empty_name():
    with pytest.raises(TypeError, match="RealtimeVoiceProvider"):
        realtime_voice_registry.register_provider(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-empty"):
        realtime_voice_registry.register_provider(FakeProvider(" ", FakeSession([])))


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name", ["grok-plugin", "second-provider"])
async def test_coordinator_keeps_tool_dispatch_in_hermes(provider_name: str):
    session = FakeSession(
        [
            RealtimeEvent.audio(b"reply-pcm"),
            RealtimeEvent.transcript("hello", final=True),
            RealtimeEvent.tool_call("call-1", "terminal", {"command": "pwd"}),
        ]
    )
    provider = FakeProvider(provider_name, session)
    dispatched: list[tuple[str, dict[str, Any]]] = []

    async def dispatch(name: str, arguments: dict[str, Any]) -> str:
        dispatched.append((name, arguments))
        return "/safe/workspace"

    coordinator = RealtimeVoiceCoordinator(provider, dispatch_tool=dispatch)
    await coordinator.open(instructions="Hermes owns tools", tools=[{"name": "terminal"}], voice="eve")
    await coordinator.send_audio(b"user-pcm")
    observed = [event async for event in coordinator.events()]
    await coordinator.close()

    assert provider.opened_with == {
        "instructions": "Hermes owns tools",
        "tools": [{"name": "terminal"}],
        "voice": "eve",
    }
    assert session.audio == [b"user-pcm"]
    assert dispatched == [("terminal", {"command": "pwd"})]
    assert session.tool_results == [("call-1", "/safe/workspace")]
    assert [event.type for event in observed] == [
        RealtimeEventType.AUDIO,
        RealtimeEventType.TRANSCRIPT,
        RealtimeEventType.TOOL_CALL,
    ]
    assert session.closed is True


@pytest.mark.asyncio
async def test_coordinator_cancels_at_the_latest_heard_output_boundary_once():
    session = BoundarySession([RealtimeEvent.audio(b"reply-pcm", item_id="item-1")])
    coordinator = RealtimeVoiceCoordinator(
        FakeProvider("fake", session), dispatch_tool=lambda _name, _args: "ok"
    )
    await coordinator.open(instructions="", tools=[])
    [output] = [event async for event in coordinator.events()]

    assert output.item_id == "item-1"
    assert coordinator.report_audio_heard(output, audio_end_ms=240) is True
    await coordinator.cancel_response()

    assert session.cancellation_boundaries == [
        HeardAudioBoundary(item_id="item-1", audio_end_ms=240)
    ]
    assert session.cancellation_operations == ["truncate", "cancel"]


@pytest.mark.asyncio
async def test_coordinator_rejects_foreign_stale_and_regressing_heard_boundaries():
    first = RealtimeEvent.audio(b"first", item_id="item-1")
    second = RealtimeEvent.audio(b"second", item_id="item-2")
    session = FakeSession([first, second])
    coordinator = RealtimeVoiceCoordinator(
        FakeProvider("fake", session), dispatch_tool=lambda _name, _args: "ok"
    )
    await coordinator.open(instructions="", tools=[])
    observed = [event async for event in coordinator.events()]

    assert coordinator.report_audio_heard(first, audio_end_ms=100) is False
    assert coordinator.report_audio_heard(
        RealtimeEvent.audio(b"foreign", item_id="item-2"), audio_end_ms=100
    ) is False
    assert coordinator.report_audio_heard(observed[1], audio_end_ms=100) is True
    assert coordinator.report_audio_heard(observed[1], audio_end_ms=90) is False


@pytest.mark.asyncio
async def test_coordinator_rejects_foreign_event_with_the_same_emission_identity():
    emitted = RealtimeEvent.audio(b"emitted", item_id="item-1")
    foreign = RealtimeEvent(
        type=RealtimeEventType.AUDIO,
        audio_bytes=b"foreign",
        item_id="item-1",
        emission_id=emitted.emission_id,
    )
    coordinator = RealtimeVoiceCoordinator(
        FakeProvider("fake", FakeSession([emitted])),
        dispatch_tool=lambda _name, _args: "ok",
    )
    await coordinator.open(instructions="", tools=[])
    [observed] = [event async for event in coordinator.events()]

    assert coordinator.report_audio_heard(observed, audio_end_ms=100) is True
    assert coordinator.report_audio_heard(foreign, audio_end_ms=120) is False


@pytest.mark.asyncio
async def test_zero_heard_and_legacy_cancel_remain_compatible_across_reconnect():
    old_output = RealtimeEvent.audio(b"old", item_id="reused-item")
    session = LegacyCancelSession([old_output])
    provider = FakeProvider("legacy", session)
    coordinator = RealtimeVoiceCoordinator(
        provider, dispatch_tool=lambda _name, _args: "ok"
    )
    await coordinator.open(instructions="", tools=[])
    [observed_old] = [event async for event in coordinator.events()]
    await coordinator.close()

    replacement = LegacyCancelSession([])
    provider.session = replacement
    await coordinator.open(instructions="", tools=[])
    assert coordinator.report_audio_heard(observed_old, audio_end_ms=0) is False
    await coordinator.cancel_response()

    assert replacement.cancellation_boundaries == [None]


@pytest.mark.asyncio
async def test_zero_heard_boundary_truncates_to_start_before_cancel():
    session = BoundarySession([RealtimeEvent.audio(b"reply", item_id="item-zero")])
    coordinator = RealtimeVoiceCoordinator(
        FakeProvider("fake", session), dispatch_tool=lambda _name, _args: "ok"
    )
    await coordinator.open(instructions="", tools=[])
    [output] = [event async for event in coordinator.events()]
    assert coordinator.report_audio_heard(output, audio_end_ms=0) is True

    await coordinator.cancel_response()

    assert session.cancellation_boundaries == [HeardAudioBoundary("item-zero", 0)]
    assert session.cancellation_operations == ["truncate", "cancel"]


@pytest.mark.asyncio
async def test_coordinator_logs_dispatch_failures_with_tool_context(
    caplog: pytest.LogCaptureFixture,
):
    session = FakeSession([RealtimeEvent.tool_call("call-2", "browser", {})])

    async def dispatch(_name: str, _arguments: dict[str, Any]) -> str:
        raise RuntimeError("approval denied")

    coordinator = RealtimeVoiceCoordinator(FakeProvider("fake", session), dispatch_tool=dispatch)
    await coordinator.open(instructions="", tools=[])
    events = [event async for event in coordinator.events()]

    assert len(events) == 1
    assert session.tool_results == [("call-2", "Error: approval denied")]
    [record] = [
        record
        for record in caplog.records
        if record.getMessage() == "Realtime voice tool dispatch failed"
    ]
    assert record.__dict__["tool_name"] == "browser"
    assert record.__dict__["call_id"] == "call-2"
    assert record.exc_info is not None


@pytest.mark.asyncio
async def test_coordinator_requires_open_session_and_closes_idempotently():
    session = FakeSession([])
    coordinator = RealtimeVoiceCoordinator(
        FakeProvider("fake", session), dispatch_tool=lambda _name, _args: "ok"
    )

    with pytest.raises(RuntimeError, match="not open"):
        await coordinator.send_audio(b"pcm")
    await coordinator.close()
    await coordinator.open(instructions="", tools=[])
    await coordinator.close()
    await coordinator.close()
    assert session.closed is True
