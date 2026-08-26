from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from agent import realtime_voice_registry
from agent.realtime_voice import (
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
async def test_coordinator_returns_dispatch_failures_to_provider_without_losing_session():
    session = FakeSession([RealtimeEvent.tool_call("call-2", "browser", {})])

    async def dispatch(_name: str, _arguments: dict[str, Any]) -> str:
        raise RuntimeError("approval denied")

    coordinator = RealtimeVoiceCoordinator(FakeProvider("fake", session), dispatch_tool=dispatch)
    await coordinator.open(instructions="", tools=[])
    events = [event async for event in coordinator.events()]

    assert len(events) == 1
    assert session.tool_results == [("call-2", "Error: approval denied")]


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
