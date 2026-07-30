from __future__ import annotations

from types import SimpleNamespace

import pytest

import agent.conversation_loop as conversation_loop
from agent.turn_gate import (
    GateDecision,
    GateState,
    TurnGateBlocked,
    clear_turn_gate_registry_for_testing,
    configure_turn_gate_from_config,
    current_turn_gate_decision,
    register_turn_gate_provider as _register_turn_gate_provider,
)


def _configure_gate(provider_id: str) -> None:
    configure_turn_gate_from_config(
        {
            "agent": {
                "turn_gate": {
                    "required_provider": provider_id,
                    "runtime_identity": {"machine_id": "test-machine"},
                }
            }
        }
    )


def register_turn_gate_provider(provider_id: str, provider) -> None:
    _register_turn_gate_provider(
        provider_id,
        provider,
        owner_id=provider_id,
    )


class RecordingProvider:
    def __init__(self, events):
        self.events = events
        self.requests = []

    def acquire(self, request):
        self.requests.append(request)
        self.events.append(("acquire", request.entrypoint, request.purpose))
        return GateDecision(
            provider_id="test-gate",
            state=GateState.OPEN,
            lease_id="lease-conversation",
            generation=26,
            allowed_tools=(),
        )

    def validate(self, decision, checkpoint):
        return decision

    def release(self, decision):
        self.events.append(("release", decision.lease_id))


@pytest.fixture(autouse=True)
def reset_registry():
    clear_turn_gate_registry_for_testing()
    yield
    clear_turn_gate_registry_for_testing()


def test_run_conversation_acquires_gate_before_body_and_releases_after_return(monkeypatch):
    events = []
    provider = RecordingProvider(events)
    register_turn_gate_provider("test-gate", provider)
    _configure_gate("test-gate")

    def fake_body(*args, **kwargs):
        decision = current_turn_gate_decision()
        assert decision is not None
        events.append(("body", decision.lease_id))
        return {"final_response": "ok"}

    monkeypatch.setattr(conversation_loop, "_run_conversation_unleased", fake_body)
    agent = SimpleNamespace(stream_delta_callback=None, session_id="session-1")
    result = conversation_loop.run_conversation(
        agent, "hello", task_id="task-1"
    )

    assert result == {"final_response": "ok"}
    assert provider.requests[0].task_id == "task-1"
    assert provider.requests[0].identity is not None
    assert provider.requests[0].identity.surface == "conversation"
    assert provider.requests[0].identity.turn_id.startswith("session-1:task-1:")
    assert events == [
        ("acquire", "conversation", "business"),
        ("body", "lease-conversation"),
        ("release", "lease-conversation"),
    ]


def test_run_conversation_releases_gate_when_body_raises(monkeypatch):
    events = []
    provider = RecordingProvider(events)
    register_turn_gate_provider("test-gate", provider)
    _configure_gate("test-gate")

    def fail_body(*args, **kwargs):
        events.append(("body", current_turn_gate_decision().lease_id))
        raise RuntimeError("body failed")

    monkeypatch.setattr(conversation_loop, "_run_conversation_unleased", fail_body)
    with pytest.raises(RuntimeError, match="body failed"):
        conversation_loop.run_conversation(SimpleNamespace(stream_delta_callback=None), "hello")

    assert events == [
        ("acquire", "conversation", "business"),
        ("body", "lease-conversation"),
        ("release", "lease-conversation"),
    ]


def test_required_gate_missing_blocks_before_conversation_body(monkeypatch):
    _configure_gate("missing-gate")
    called = False

    def fake_body(*args, **kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(conversation_loop, "_run_conversation_unleased", fake_body)
    with pytest.raises(TurnGateBlocked, match="required provider"):
        conversation_loop.run_conversation(SimpleNamespace(stream_delta_callback=None), "hello")
    assert called is False
