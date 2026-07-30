"""Behavior contracts for the core-owned turn-routing boundary."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock

import pytest

import agent.conversation_loop as conversation_loop
import agent.turn_routing_runtime as turn_routing_runtime
from agent.turn_router import RouteDecision
from run_agent import AIAgent


class _ForwarderAgent:
    """Small object that exercises the real AIAgent forwarder method."""

    run_conversation = AIAgent.run_conversation
    session_id = "routing-boundary-session"
    _session_db = None

    def _conversation_root_id(self) -> str:
        return self.session_id


@pytest.mark.parametrize("loop_raises", [False, True])
def test_run_conversation_owns_opt_in_route_lifecycle_until_return_or_error(
    monkeypatch,
    loop_raises,
):
    agent = _ForwarderAgent()
    request = object()
    scope = object()
    events = []

    @contextmanager
    def fake_turn_routing_lifecycle(target_agent, routing_request):
        assert target_agent is agent
        assert routing_request is request
        events.append("enter")
        try:
            yield scope
        finally:
            events.append("exit")

    def fake_conversation_loop(*args, **kwargs):
        events.append(("run", kwargs.get("turn_routing_lifecycle")))
        if loop_raises:
            raise RuntimeError("synthetic early exit")
        return {"final_response": "ok", "messages": []}

    monkeypatch.setattr(
        turn_routing_runtime,
        "turn_routing_lifecycle",
        fake_turn_routing_lifecycle,
        raising=False,
    )
    monkeypatch.setattr(conversation_loop, "run_conversation", fake_conversation_loop)

    if loop_raises:
        with pytest.raises(RuntimeError, match="synthetic early exit"):
            agent.run_conversation("hello", turn_routing_request=request)
    else:
        result = agent.run_conversation("hello", turn_routing_request=request)
        assert result["final_response"] == "ok"

    assert events == ["enter", ("run", scope), "exit"]


def test_run_conversation_without_opt_in_preserves_legacy_boundary(monkeypatch):
    agent = _ForwarderAgent()

    def fail_if_lifecycle_is_created(*_args, **_kwargs):
        raise AssertionError("legacy turns must not create a routing lifecycle")

    def fake_conversation_loop(*_args, **kwargs):
        assert kwargs.get("turn_routing_lifecycle") is None
        return {"final_response": "legacy", "messages": []}

    monkeypatch.setattr(
        turn_routing_runtime,
        "turn_routing_lifecycle",
        fail_if_lifecycle_is_created,
        raising=False,
    )
    monkeypatch.setattr(conversation_loop, "run_conversation", fake_conversation_loop)

    result = agent.run_conversation("hello")

    assert result["final_response"] == "legacy"


def test_synthetic_display_turn_does_not_create_routing_lifecycle(monkeypatch):
    agent = _ForwarderAgent()
    request = object()

    def fail_if_lifecycle_is_created(*_args, **_kwargs):
        raise AssertionError("synthetic turns must not create a routing lifecycle")

    def fake_conversation_loop(*_args, **kwargs):
        assert kwargs.get("turn_routing_lifecycle") is None
        return {"final_response": "continued", "messages": []}

    monkeypatch.setattr(
        turn_routing_runtime,
        "turn_routing_lifecycle",
        fail_if_lifecycle_is_created,
        raising=False,
    )
    monkeypatch.setattr(conversation_loop, "run_conversation", fake_conversation_loop)

    result = agent.run_conversation(
        "[System recovery note] Continue the interrupted response.",
        turn_routing_request=request,
        persist_user_display_kind="auto_continue",
    )

    assert result["final_response"] == "continued"


def test_conversation_loop_passes_route_lifecycle_to_turn_context(monkeypatch):
    scope = object()
    captured = []

    def fake_build_turn_context(*_args, **kwargs):
        captured.append(kwargs.get("turn_routing_lifecycle"))
        return SimpleNamespace(
            user_message="hello",
            original_user_message="hello",
            messages=[{"role": "user", "content": "hello"}],
            conversation_history=[],
            active_system_prompt="stable prompt",
            effective_task_id="task-1",
            turn_id="session:task-1:turn-1",
            current_turn_user_idx=0,
            should_review_memory=False,
            plugin_user_context="",
            ext_prefetch_cache="",
            preflight_compression_blocked=False,
        )

    class _CodexAppServerAgent:
        api_mode = "codex_app_server"
        max_iterations = 1

        def _run_codex_app_server_turn(self, **kwargs):
            return {"final_response": "ok", "messages": kwargs["messages"]}

    monkeypatch.setattr(conversation_loop, "build_turn_context", fake_build_turn_context)

    result = conversation_loop.run_conversation(
        _CodexAppServerAgent(),
        "hello",
        turn_routing_lifecycle=scope,
    )

    assert result["final_response"] == "ok"
    assert captured == [scope]


@pytest.mark.parametrize("turn_raises", [False, True])
def test_route_lifecycle_restores_prepared_token_on_return_or_error(
    monkeypatch,
    turn_raises,
):
    agent = object()
    request = object()
    token = SimpleNamespace(restore=MagicMock(return_value=True))

    prepare = MagicMock(return_value=token)
    monkeypatch.setattr(
        turn_routing_runtime,
        "prepare_turn_route",
        prepare,
        raising=False,
    )

    def run_turn():
        with turn_routing_runtime.turn_routing_lifecycle(agent, request) as lifecycle:
            lifecycle.prepare(
                agent,
                user_message="hello",
                turn_id="session:task:turn",
            )
            if turn_raises:
                raise RuntimeError("synthetic provider failure")

    if turn_raises:
        with pytest.raises(RuntimeError, match="synthetic provider failure"):
            run_turn()
    else:
        run_turn()

    prepare.assert_called_once_with(
        agent,
        request,
        user_message="hello",
        turn_id="session:task:turn",
        decision_sink=ANY,
    )
    assert callable(prepare.call_args.kwargs["decision_sink"])
    token.restore.assert_called_once_with()


def test_route_lifecycle_does_not_emit_terminal_event_without_decision_provenance():
    emitted = []
    request = turn_routing_runtime.TurnRoutingRequest(
        surface="tui",
        session_id="session-1",
        emit=lambda event, payload: emitted.append((event, payload)),
    )
    lifecycle = turn_routing_runtime.TurnRoutingLifecycle(
        agent=object(),
        request=request,
        turn_id="session-1:task:turn",
        token=SimpleNamespace(restore=MagicMock(return_value=True)),
        prepared=True,
    )

    lifecycle.finish()

    assert emitted == []


def test_route_prepare_failure_fails_open_with_turn_scoped_degradation(monkeypatch):
    emitted = []
    request = turn_routing_runtime.TurnRoutingRequest(
        surface="tui",
        session_id="session-1",
        emit=lambda event, payload: emitted.append((event, payload)),
    )

    def fail_prepare(*_args, **_kwargs):
        raise RuntimeError("synthetic policy failure")

    monkeypatch.setattr(turn_routing_runtime, "prepare_turn_route", fail_prepare)

    with turn_routing_runtime.turn_routing_lifecycle(object(), request) as lifecycle:
        lifecycle.prepare(
            lifecycle.agent,
            user_message="hello",
            turn_id="session-1:task:turn",
        )

    assert emitted == [
        (
            "route.degraded",
            {
                "session_id": "session-1",
                "turn_id": "session-1:task:turn",
                "surface": "tui",
                "stage": "prepare",
                "reason_code": "route_prepare_failed",
            },
        )
    ]


def test_prepare_partial_restore_failure_quarantines_resident_agent(monkeypatch):
    emitted = []
    quarantined = []
    agent = object()
    request = turn_routing_runtime.TurnRoutingRequest(
        surface="tui",
        session_id="session-1",
        emit=lambda event, payload: emitted.append((event, payload)),
        quarantine=lambda target, reason: quarantined.append((target, reason)),
    )
    decision = RouteDecision(
        route="deep",
        target={"kind": "moa", "preset": "deep"},
        mode="auto",
        source="rule",
        reason_code="semantic_deep",
        confidence=1.0,
        should_apply=True,
    )
    monkeypatch.setattr(
        turn_routing_runtime,
        "prepare_turn_route",
        MagicMock(
            side_effect=turn_routing_runtime.RouteApplicationError(
                "partial apply could not restore",
                restore_failed=True,
                decision=decision,
            )
        ),
    )

    provider_dispatch = MagicMock()
    with pytest.raises(turn_routing_runtime.RouteApplicationError):
        with turn_routing_runtime.turn_routing_lifecycle(agent, request) as lifecycle:
            lifecycle.prepare(
                agent,
                user_message="hello",
                turn_id="session-1:prepare:turn",
            )
            provider_dispatch()

    provider_dispatch.assert_not_called()
    assert lifecycle.token is None
    assert quarantined == [(agent, "route_restore_failed")]
    assert emitted == [
        (
            "route.degraded",
            {
                "session_id": "session-1",
                "turn_id": "session-1:prepare:turn",
                "surface": "tui",
                "route": "deep",
                "target": {
                    "kind": "moa",
                    "preset": "deep",
                    "enabled": True,
                },
                "mode": "auto",
                "source": "rule",
                "reason_code": "route_restore_failed",
                "confidence": 1.0,
                "should_apply": True,
                "requires_confirmation": False,
                "selection_reason_code": "semantic_deep",
                "stage": "prepare",
            },
        )
    ]


@pytest.mark.parametrize("restore_outcome", [False, RuntimeError("restore failed")])
def test_route_restore_failure_quarantines_without_masking_provider_result(
    monkeypatch,
    restore_outcome,
):
    emitted = []
    quarantined = []
    request = turn_routing_runtime.TurnRoutingRequest(
        surface="tui",
        session_id="session-1",
        emit=lambda event, payload: emitted.append((event, payload)),
        quarantine=lambda agent, reason: quarantined.append((agent, reason)),
    )
    agent = object()

    def restore():
        if isinstance(restore_outcome, Exception):
            raise restore_outcome
        return restore_outcome

    monkeypatch.setattr(
        turn_routing_runtime,
        "prepare_turn_route",
        MagicMock(return_value=SimpleNamespace(restore=restore)),
    )

    with pytest.raises(RuntimeError, match="provider failed"):
        with turn_routing_runtime.turn_routing_lifecycle(agent, request) as lifecycle:
            lifecycle.prepare(
                agent,
                user_message="hello",
                turn_id="session-1:task:turn",
            )
            raise RuntimeError("provider failed")

    assert quarantined == [(agent, "route_restore_failed")]
    assert emitted[-1] == (
        "route.degraded",
        {
            "session_id": "session-1",
            "turn_id": "session-1:task:turn",
            "surface": "tui",
            "stage": "restore",
            "reason_code": "route_restore_failed",
        },
    )


def test_route_event_transport_failure_cannot_skip_restore(monkeypatch):
    token = SimpleNamespace(restore=MagicMock(return_value=True))
    request = turn_routing_runtime.TurnRoutingRequest(
        surface="tui",
        session_id="session-1",
        emit=MagicMock(side_effect=RuntimeError("event transport failed")),
    )
    monkeypatch.setattr(
        turn_routing_runtime,
        "prepare_turn_route",
        MagicMock(return_value=token),
    )

    with turn_routing_runtime.turn_routing_lifecycle(object(), request) as lifecycle:
        lifecycle.prepare(
            lifecycle.agent,
            user_message="hello",
            turn_id="session-1:task:turn",
        )

    token.restore.assert_called_once_with()


def test_route_completed_preserves_turn_identity_and_decision_provenance(monkeypatch):
    from agent.turn_router import RouteDecision

    emitted = []
    decision = RouteDecision(
        route="deep",
        target={"kind": "moa", "preset": "deep"},
        mode="auto",
        source="rule",
        reason_code="architecture_complexity",
        confidence=0.9,
        should_apply=True,
    )
    token = SimpleNamespace(
        decision=decision,
        restore=MagicMock(return_value=True),
    )
    request = turn_routing_runtime.TurnRoutingRequest(
        surface="tui",
        session_id="session-1",
        emit=lambda event, payload: emitted.append((event, payload)),
    )
    monkeypatch.setattr(
        turn_routing_runtime,
        "prepare_turn_route",
        MagicMock(return_value=token),
    )

    with turn_routing_runtime.turn_routing_lifecycle(object(), request) as lifecycle:
        lifecycle.prepare(
            lifecycle.agent,
            user_message="Architect a high-risk migration",
            turn_id="session-1:task:turn",
        )

    assert emitted == [
        (
            "route.completed",
            {
                "session_id": "session-1",
                "turn_id": "session-1:task:turn",
                "surface": "tui",
                "route": "deep",
                "target": {"kind": "moa", "enabled": True, "preset": "deep"},
                "mode": "auto",
                "source": "rule",
                "reason_code": "route_completed",
                "selection_reason_code": "architecture_complexity",
                "confidence": 0.9,
                "should_apply": True,
                "requires_confirmation": False,
                "stage": "restore",
            },
        )
    ]


@pytest.mark.parametrize("failure_site", ["config", "policy"])
def test_real_prepare_config_and_policy_fail_open_with_degradation(
    monkeypatch,
    failure_site,
):
    import agent.turn_router as turn_router

    emitted = []
    config_loader = MagicMock(return_value={"mode": "observe"})
    if failure_site == "config":
        config_loader.side_effect = RuntimeError("config unavailable")
    else:
        monkeypatch.setattr(
            turn_router,
            "decide_turn_route",
            MagicMock(side_effect=RuntimeError("policy failed")),
        )
    request = turn_routing_runtime.TurnRoutingRequest(
        surface="tui",
        session_id="session-1",
        config_loader=config_loader,
        emit=lambda event, payload: emitted.append((event, payload)),
    )

    with turn_routing_runtime.turn_routing_lifecycle(object(), request) as lifecycle:
        lifecycle.prepare(
            lifecycle.agent,
            user_message="hello",
            turn_id=f"session-1:{failure_site}:turn",
        )

    assert lifecycle.token is None
    assert emitted == [
        (
            "route.degraded",
            {
                "session_id": "session-1",
                "turn_id": f"session-1:{failure_site}:turn",
                "surface": "tui",
                "stage": "prepare",
                "reason_code": "route_prepare_failed",
            },
        )
    ]


def test_real_apply_failure_keeps_current_runtime_and_emits_degradation(monkeypatch):
    emitted = []
    monkeypatch.setattr(
        turn_routing_runtime,
        "build_transient_route",
        MagicMock(
            side_effect=turn_routing_runtime.RouteApplicationError(
                "apply failed",
                decision=RouteDecision(
                    route="deep",
                    target={"kind": "moa", "preset": "deep"},
                    mode="auto",
                    source="rule",
                    reason_code="semantic_deep",
                    confidence=1.0,
                    should_apply=True,
                ),
            )
        ),
    )
    request = turn_routing_runtime.TurnRoutingRequest(
        surface="tui",
        session_id="session-1",
        user_text="Architect a high-risk cross-system migration",
        allow_automatic=True,
        config_loader=lambda: {
            "mode": "auto",
            "default_route": "k3",
            "routes": {
                "k3": {"kind": "model", "provider": "kimi-coding", "model": "k3"},
                "deep": {"kind": "moa", "preset": "deep"},
            },
            "lanes": {"plain": "k3", "deep": "deep"},
        },
        emit=lambda event, payload: emitted.append((event, payload)),
    )
    agent = SimpleNamespace(model="k3", provider="kimi-coding")

    with turn_routing_runtime.turn_routing_lifecycle(agent, request) as lifecycle:
        lifecycle.prepare(
            agent,
            user_message=request.user_text,
            turn_id="session-1:apply:turn",
        )

    assert lifecycle.token is None
    assert [event for event, _payload in emitted] == [
        "route.decided",
        "route.degraded",
    ]
    assert emitted[-1][1] == {
        "session_id": "session-1",
        "turn_id": "session-1:apply:turn",
        "surface": "tui",
        "route": "deep",
        "target": {"kind": "moa", "preset": "deep", "enabled": True},
        "mode": "auto",
        "source": "rule",
        "confidence": 1.0,
        "should_apply": True,
        "requires_confirmation": False,
        "selection_reason_code": "semantic_deep",
        "stage": "prepare",
        "reason_code": "route_prepare_failed",
    }
