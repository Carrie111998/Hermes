from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent import turn_routing_runtime


def _routing_config(*, affinity_turns=2, failure_limit=3):
    return {
        "mode": "auto",
        "affinity_turns": affinity_turns,
        "failure_limit": failure_limit,
        "routes": {
            "current": {"kind": "current"},
            "deep": {
                "kind": "model",
                "provider": "openai-codex",
                "model": "gpt-5.6-sol",
            },
        },
        "lanes": {"plain": "current", "deep": "deep"},
        "classifier": {"enabled": False},
    }


class _Token:
    def __init__(self, decision):
        self.decision = decision
        self.restore = MagicMock(return_value=True)


def _run_turn(monkeypatch, agent, state, config, text, *, fail=False, **request_fields):
    built = []

    def _build(_agent, decision):
        token = _Token(decision)
        built.append(token)
        return token

    monkeypatch.setattr(turn_routing_runtime, "build_transient_route", _build)
    events = []
    request = turn_routing_runtime.TurnRoutingRequest(
        surface="test",
        session_id="session-state",
        user_text=text,
        allow_automatic=True,
        config_loader=lambda: config,
        session_state=state,
        emit=lambda event, payload: events.append((event, payload)),
        **request_fields,
    )
    lifecycle = None
    if fail:
        with pytest.raises(RuntimeError, match="turn failed"):
            with turn_routing_runtime.turn_routing_lifecycle(agent, request) as lifecycle:
                lifecycle.prepare(agent, user_message=text, turn_id="turn-failed")
                raise RuntimeError("turn failed")
    else:
        with turn_routing_runtime.turn_routing_lifecycle(agent, request) as lifecycle:
            lifecycle.prepare(agent, user_message=text, turn_id="turn-ok")
    assert lifecycle is not None
    assert isinstance(lifecycle.decision, turn_routing_runtime.RouteDecision)
    return lifecycle.decision, events, built


def test_affinity_is_bounded_and_precedes_new_semantic_classification(monkeypatch):
    agent = SimpleNamespace(model="k3", provider="kimi-coding")
    state = turn_routing_runtime.TurnRoutingSessionState()
    config = _routing_config(affinity_turns=2)

    first, _, _ = _run_turn(
        monkeypatch,
        agent,
        state,
        config,
        "Architect a high-risk cross-system migration",
    )
    assert first.source == "rule"
    assert state.affinity_route == "deep"
    assert state.affinity_remaining == 2

    second, _, _ = _run_turn(monkeypatch, agent, state, config, "continue")
    third, _, _ = _run_turn(monkeypatch, agent, state, config, "one more follow-up")
    fourth, _, built = _run_turn(monkeypatch, agent, state, config, "new unrelated task")

    assert second.source == "affinity"
    assert third.source == "affinity"
    assert fourth.route == "current"
    assert fourth.reason_code == "default_route"
    assert not built
    assert state.affinity_route is None
    assert state.affinity_remaining == 0


def test_repeated_automatic_failures_latch_fail_off_but_explicit_wins(monkeypatch):
    agent = SimpleNamespace(model="k3", provider="kimi-coding")
    state = turn_routing_runtime.TurnRoutingSessionState()
    config = _routing_config(failure_limit=2)

    for _ in range(2):
        decision, _, _ = _run_turn(
            monkeypatch,
            agent,
            state,
            config,
            "Architect a high-risk cross-system migration",
            fail=True,
        )
        assert decision.source == "rule"

    assert state.fail_off is True
    assert state.consecutive_failures == 2
    assert state.affinity_route is None

    stopped, events, built = _run_turn(
        monkeypatch,
        agent,
        state,
        config,
        "Architect a high-risk cross-system migration again",
    )
    assert stopped.source == "fail_off"
    assert stopped.should_apply is False
    assert not built
    assert events[-1][0] == "route.completed"
    assert events[-1][1]["source"] == "fail_off"

    explicit, _, built = _run_turn(
        monkeypatch,
        agent,
        state,
        config,
        "manual selection",
        explicit_turn_override=True,
        explicit_target={
            "kind": "model",
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
        },
    )
    assert explicit.source == "explicit"
    assert explicit.should_apply is True
    assert len(built) == 1
    assert state.fail_off is True

    state.reset()
    resumed, _, _ = _run_turn(
        monkeypatch,
        agent,
        state,
        config,
        "Architect a high-risk cross-system migration",
    )
    assert resumed.source == "rule"
    assert resumed.should_apply is True


def test_latest_route_provenance_is_metadata_only(monkeypatch):
    agent = SimpleNamespace(model="k3", provider="kimi-coding")
    state = turn_routing_runtime.TurnRoutingSessionState()
    user_text = "Architect a high-risk cross-system migration SECRET_PROMPT_TEXT"

    _run_turn(monkeypatch, agent, state, _routing_config(), user_text)

    assert state.latest_event == "route.completed"
    assert state.latest_payload is not None
    assert state.latest_payload["source"] == "rule"
    assert user_text not in repr(state.latest_payload)
    assert "SECRET_PROMPT_TEXT" not in repr(state.latest_payload)
