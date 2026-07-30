from dataclasses import replace
import re
import threading

import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent import turn_routing_runtime
from agent.agent_runtime_helpers import (
    restore_agent_model_runtime,
    snapshot_agent_model_runtime,
)
from agent.turn_router import RouteAuthorization, RouteDecision, authorize_route
from agent.turn_routing_runtime import (
    BudgetRouteContext,
    RouteApplicationError,
    TurnRoutingLifecycle,
    prepare_turn_route,
)


def _route_decision(*, kind: str, value: str, should_apply: bool = True) -> RouteDecision:
    target = {"kind": kind}
    if kind == "moa":
        target["preset"] = value
    elif kind == "model":
        target["model"] = value
    return RouteDecision(
        route=value or "current",
        target=target,
        mode="auto" if should_apply else "off",
        source="rule" if should_apply else "current",
        reason_code="test_route",
        confidence=0.9 if should_apply else 1.0,
        should_apply=should_apply,
    )


class _FakeAgent:
    def __init__(self):
        self.model = "k3"
        self.provider = "kimi-coding"
        self.api_key = "redacted"
        self.base_url = "https://example.invalid"
        self.api_mode = "chat_completions"
        self._primary_runtime = {
            "model": "k3",
            "provider": "kimi-coding",
            "nested": {"generation": 1},
        }
        self._fallback_activated = False
        self._rate_limited_until = 7
        self.restored_primary = False
        self.switch_calls = []

    def _restore_primary_runtime(self):
        self.restored_primary = True
        self.model = self._primary_runtime["model"]
        self.provider = self._primary_runtime["provider"]
        return True

    def switch_model(self, **kwargs):
        self.switch_calls.append(kwargs)


def test_provider_submission_scope_commits_acceptance_and_restores_agent_hooks():
    ledger = MagicMock()
    reservation = SimpleNamespace(
        reservation_id="reservation-1",
        state="reserved",
    )
    agent = SimpleNamespace(_turn_route_budget_submission_started="existing")
    lifecycle = TurnRoutingLifecycle(
        agent=agent,
        request=turn_routing_runtime.TurnRoutingRequest(surface="tui"),
        turn_id="turn-1",
        budget_context=BudgetRouteContext(
            ledger=ledger,
            reservation=reservation,
            cooldown_seconds=60,
        ),
        budget_state="reserved",
    )

    with lifecycle.provider_submission_scope(agent, "request-1"):
        agent._turn_route_budget_submission_started()
        response = SimpleNamespace(id="provider-response-1")
        agent._turn_route_budget_submission_accepted(response)

    ledger.commit.assert_called_once_with(
        "reservation-1",
        provider_submission_id="provider-response-1",
    )
    assert lifecycle.budget_state == "committed"
    assert agent._turn_route_budget_submission_started == "existing"
    assert not hasattr(agent, "_turn_route_budget_submission_accepted")
    assert not hasattr(agent, "_turn_route_budget_submission_failed")


def test_finish_conservatively_commits_an_invoked_submission_before_worker_settles():
    ledger = MagicMock()
    agent = SimpleNamespace()
    lifecycle = TurnRoutingLifecycle(
        agent=agent,
        request=turn_routing_runtime.TurnRoutingRequest(surface="tui"),
        turn_id="turn-interrupted",
        budget_context=BudgetRouteContext(
            ledger=ledger,
            reservation=SimpleNamespace(
                reservation_id="reservation-interrupted",
                state="reserved",
            ),
            cooldown_seconds=60,
        ),
        budget_state="reserved",
    )

    lifecycle.provider_submission_started("request-interrupted")
    lifecycle.mark_turn_failed("turn_exception")
    lifecycle.finish()

    ledger.commit.assert_called_once_with(
        "reservation-interrupted",
        provider_submission_id="request-interrupted",
    )
    ledger.release.assert_not_called()
    assert lifecycle.budget_state == "committed"

    # A daemon worker may settle after the conversation thread has already
    # finalized. Its late callback cannot release or double-commit the slot.
    lifecycle.provider_submission_failed(
        InterruptedError("late interrupted worker"),
        "request-interrupted",
    )
    ledger.commit.assert_called_once()
    ledger.release.assert_not_called()


def test_finish_serializes_with_a_late_provider_acceptance_callback():
    first_commit_entered = threading.Event()
    allow_first_commit = threading.Event()

    class _Ledger:
        def __init__(self):
            self.commits = []
            self.releases = []

        def commit(self, reservation_id, *, provider_submission_id):
            self.commits.append((reservation_id, provider_submission_id))
            if len(self.commits) == 1:
                first_commit_entered.set()
                assert allow_first_commit.wait(timeout=2)

        def release(self, reservation_id, *, reason_code):
            self.releases.append((reservation_id, reason_code))

    ledger = _Ledger()
    lifecycle = TurnRoutingLifecycle(
        agent=SimpleNamespace(),
        request=turn_routing_runtime.TurnRoutingRequest(surface="tui"),
        turn_id="turn-race",
        budget_context=BudgetRouteContext(
            ledger=ledger,
            reservation=SimpleNamespace(
                reservation_id="reservation-race",
                state="reserved",
            ),
            cooldown_seconds=60,
        ),
        budget_state="reserved",
    )
    lifecycle.provider_submission_started("request-race")

    finish_thread = threading.Thread(target=lifecycle.finish)
    finish_thread.start()
    assert first_commit_entered.wait(timeout=2)

    accepted_thread = threading.Thread(
        target=lambda: lifecycle.provider_submission_accepted(
            SimpleNamespace(id="provider-response-race"),
            "request-race",
        )
    )
    accepted_thread.start()
    allow_first_commit.set()
    finish_thread.join(timeout=2)
    accepted_thread.join(timeout=2)

    assert not finish_thread.is_alive()
    assert not accepted_thread.is_alive()
    assert ledger.commits == [("reservation-race", "request-race")]
    assert ledger.releases == []
    assert lifecycle.budget_state == "committed"


def test_provider_submission_scope_replaces_credential_like_response_id():
    ledger = MagicMock()
    agent = SimpleNamespace()
    lifecycle = TurnRoutingLifecycle(
        agent=agent,
        request=turn_routing_runtime.TurnRoutingRequest(surface="tui"),
        turn_id="turn-safe-audit",
        budget_context=BudgetRouteContext(
            ledger=ledger,
            reservation=SimpleNamespace(
                reservation_id="reservation-safe-audit",
                state="reserved",
            ),
            cooldown_seconds=60,
        ),
        budget_state="reserved",
    )

    with lifecycle.provider_submission_scope(agent, "request-safe-fallback"):
        agent._turn_route_budget_submission_started()
        agent._turn_route_budget_submission_accepted(
            SimpleNamespace(id="Bearer sk-secret-provider-body")
        )

    safe_id = ledger.commit.call_args.kwargs["provider_submission_id"]
    assert re.fullmatch(r"attempt:[0-9a-f]{32}", safe_id)
    assert lifecycle.provider_submission_id == safe_id
    assert "sk-secret-provider-body" not in repr(
        lifecycle._payload(stage="completed", reason_code="ok")
    )


def test_released_budget_blocks_a_second_provider_submission_before_sdk_call():
    from agent.turn_routing_runtime import RouteBudgetDispatchBlocked

    agent = SimpleNamespace()
    lifecycle = TurnRoutingLifecycle(
        agent=agent,
        request=turn_routing_runtime.TurnRoutingRequest(surface="tui"),
        turn_id="turn-1",
        budget_context=BudgetRouteContext(
            ledger=MagicMock(),
            reservation=SimpleNamespace(
                reservation_id="reservation-1",
                state="reserved",
            ),
            cooldown_seconds=60,
        ),
        budget_state="released",
    )

    with lifecycle.provider_submission_scope(agent, "request-2"):
        with pytest.raises(RouteBudgetDispatchBlocked):
            agent._turn_route_budget_submission_started()


def test_core_budget_reserves_after_turn_id_and_releases_before_dispatch(monkeypatch):
    events = []

    def _token_for(_agent, decision):
        return SimpleNamespace(
            decision=decision,
            restore=MagicMock(return_value=True),
        )

    monkeypatch.setattr(turn_routing_runtime, "build_transient_route", _token_for)
    agent = SimpleNamespace(model="k3", provider="kimi-coding")
    request = turn_routing_runtime.TurnRoutingRequest(
        surface="tui",
        session_id="session-1",
        user_text="Review this patch",
        allow_automatic=True,
        config_loader=lambda: {
            "mode": "auto",
            "default_route": "grok-review",
            "routes": {
                "grok-review": {
                    "kind": "model",
                    "provider": "xai",
                    "model": "grok-4.5",
                }
            },
            "budget": {
                "grok_weekly_limit": 1,
                "reservation_lease_seconds": 300,
                "cooldown_seconds": 60,
            },
        },
        emit=lambda event, payload: events.append((event, payload)),
    )

    with turn_routing_runtime.turn_routing_lifecycle(agent, request) as lifecycle:
        lifecycle.prepare(
            agent,
            user_message=request.user_text,
            turn_id="turn-budget-1",
        )
        context = lifecycle.budget_context
        assert context is not None
        reservation_id = context.reservation.reservation_id
        assert reservation_id
        assert context.ledger.get(reservation_id).state == "reserved"
        decision = lifecycle.decision
        assert decision is not None
        authorization = decision.authorization
        assert authorization is not None
        assert authorization.reservation_id == reservation_id

    assert context.ledger.get(reservation_id).state == "released"
    assert events[-1][0] == "route.completed"
    assert events[-1][1]["budget_state"] == "released"


def test_core_budget_reserves_all_resolved_grok_slots_for_explicit_moa(monkeypatch):
    monkeypatch.setattr(
        turn_routing_runtime,
        "build_transient_route",
        lambda _agent, decision: SimpleNamespace(
            decision=decision,
            restore=MagicMock(return_value=True),
        ),
    )
    agent = SimpleNamespace(model="k3", provider="kimi-coding")
    request = turn_routing_runtime.TurnRoutingRequest(
        surface="tui",
        explicit_moa_override=True,
        explicit_target={"kind": "moa", "preset": "frontier"},
        moa_config={
            "presets": {
                "frontier": {
                    "reference_models": [
                        {"provider": "xai-oauth", "model": "opaque-review"},
                        {"provider": "kimi-coding", "model": "k3-256k"},
                    ],
                    "aggregator": {"provider": "xai", "model": "grok-4.5"},
                }
            }
        },
        config_loader=lambda: {
            "mode": "off",
            "budget": {"grok_weekly_limit": 2},
        },
    )

    with turn_routing_runtime.turn_routing_lifecycle(agent, request) as lifecycle:
        lifecycle.prepare(agent, user_message="review", turn_id="turn-frontier")
        context = lifecycle.budget_context
        assert context is not None
        assert context.reservation.slots == 2
        assert context.ledger.status().reserved_slots == 2

    assert context.ledger.status().available_slots == 2


def test_caller_cannot_mint_fake_authorization_for_budgeted_explicit_target(monkeypatch):
    builder = MagicMock()
    monkeypatch.setattr(turn_routing_runtime, "build_transient_route", builder)
    agent = SimpleNamespace(model="k3", provider="kimi-coding")
    request = turn_routing_runtime.TurnRoutingRequest(
        surface="tui",
        user_text="review this",
        explicit_turn_override=True,
        explicit_target={
            "kind": "model",
            "provider": "xai",
            "model": "grok-4.5",
        },
        authorization=RouteAuthorization(
            allowed=True,
            reason_code="caller_claimed_allowed",
            reservation_id="fake-reservation",
        ),
        config_loader=lambda: {
            "mode": "off",
            "budget": {"grok_weekly_limit": 0},
        },
    )

    with pytest.raises(turn_routing_runtime.RouteAuthorizationError) as denied:
        turn_routing_runtime.prepare_turn_route(
            agent,
            request,
            user_message=request.user_text,
            turn_id="turn-fake-authorization",
        )

    assert denied.value.decision.reason_code == "budget_authorization_unavailable"
    builder.assert_not_called()


def test_provider_429_releases_reservation_and_sets_durable_cooldown():
    class _RateLimitError(RuntimeError):
        status_code = 429

    ledger = MagicMock()
    lifecycle = TurnRoutingLifecycle(
        agent=SimpleNamespace(),
        request=turn_routing_runtime.TurnRoutingRequest(surface="tui"),
        turn_id="turn-1",
        budget_context=BudgetRouteContext(
            ledger=ledger,
            reservation=SimpleNamespace(
                reservation_id="reservation-1",
                state="reserved",
            ),
            cooldown_seconds=60,
        ),
        budget_state="reserved",
    )
    lifecycle.provider_submission_started("request-1")
    error = _RateLimitError("rate limited")

    lifecycle.provider_submission_failed(error, "request-1")

    ledger.release.assert_called_once_with(
        "reservation-1",
        reason_code="provider_rate_limited",
    )
    cooldown = ledger.set_cooldown.call_args.kwargs
    assert cooldown["scope"] == "grok"
    assert cooldown["reason_code"] == "provider_rate_limited"
    assert cooldown["until_at"] > 0
    assert lifecycle.budget_state == "released"


@pytest.mark.parametrize("status_code", [402, 403])
def test_provider_entitlement_errors_release_and_set_durable_cooldown(status_code):
    error_type = type(
        "_EntitlementError",
        (RuntimeError,),
        {"status_code": status_code},
    )
    ledger = MagicMock()
    lifecycle = TurnRoutingLifecycle(
        agent=SimpleNamespace(),
        request=turn_routing_runtime.TurnRoutingRequest(surface="tui"),
        turn_id="turn-1",
        budget_context=BudgetRouteContext(
            ledger=ledger,
            reservation=SimpleNamespace(
                reservation_id="reservation-1",
                state="reserved",
            ),
            cooldown_seconds=60,
        ),
        budget_state="reserved",
    )
    lifecycle.provider_submission_started("request-1")

    lifecycle.provider_submission_failed(error_type("not entitled"), "request-1")

    ledger.release.assert_called_once_with(
        "reservation-1",
        reason_code="provider_entitlement_rejected",
    )
    cooldown = ledger.set_cooldown.call_args.kwargs
    assert cooldown["scope"] == "grok"
    assert cooldown["reason_code"] == "provider_entitlement_rejected"
    assert lifecycle.budget_state == "released"


@pytest.mark.parametrize(
    ("status_code", "reason_code"),
    [
        (402, "provider_entitlement_rejected"),
        (403, "provider_entitlement_rejected"),
        (429, "provider_rate_limited"),
    ],
)
def test_provider_denial_persists_cooldown_that_blocks_another_process(
    tmp_path,
    status_code,
    reason_code,
):
    from agent.turn_router_budget import TurnRouterBudgetLedger

    db_path = tmp_path / "turn-router-budget.db"
    ledger = TurnRouterBudgetLedger(db_path=db_path, weekly_limit=2)
    reservation = ledger.reserve(
        turn_id="turn-provider-denied",
        route_id="grok-review",
        cooldown_scope="grok",
    )
    assert reservation.reservation_id is not None
    lifecycle = TurnRoutingLifecycle(
        agent=SimpleNamespace(),
        request=turn_routing_runtime.TurnRoutingRequest(surface="tui"),
        turn_id="turn-provider-denied",
        budget_context=BudgetRouteContext(
            ledger=ledger,
            reservation=reservation,
            cooldown_seconds=60,
        ),
        budget_state="reserved",
    )
    error_type = type(
        "_ProviderDenied",
        (RuntimeError,),
        {"status_code": status_code},
    )
    lifecycle.provider_submission_started("request-provider-denied")

    lifecycle.provider_submission_failed(
        error_type("sensitive provider body must not be stored"),
        "request-provider-denied",
    )

    other_process = TurnRouterBudgetLedger(
        db_path=db_path,
        weekly_limit=2,
        owner_id="other-process",
    )
    blocked = other_process.reserve(
        turn_id="turn-after-denial",
        route_id="grok-review",
        cooldown_scope="grok",
    )
    assert lifecycle.budget_state == "released"
    assert blocked.allowed is False
    assert blocked.reason_code == reason_code
    audit = ledger.audit_rows(reservation_id=reservation.reservation_id)
    assert [row.state for row in audit] == ["reserved", "released"]
    assert audit[-1].reason_code == reason_code
    assert "sensitive provider body" not in repr(audit)


def test_uncertain_post_invocation_failure_conservatively_commits():
    ledger = MagicMock()
    lifecycle = TurnRoutingLifecycle(
        agent=SimpleNamespace(),
        request=turn_routing_runtime.TurnRoutingRequest(surface="tui"),
        turn_id="turn-1",
        budget_context=BudgetRouteContext(
            ledger=ledger,
            reservation=SimpleNamespace(
                reservation_id="reservation-1",
                state="reserved",
            ),
            cooldown_seconds=60,
        ),
        budget_state="reserved",
    )
    lifecycle.provider_submission_started("request-1")

    lifecycle.provider_submission_failed(RuntimeError("socket closed"), "request-1")

    ledger.commit.assert_called_once_with(
        "reservation-1",
        provider_submission_id="request-1",
    )
    assert lifecycle.budget_state == "committed"


def test_provider_acceptance_accounting_failure_quarantines_and_blocks_retry():
    ledger = MagicMock()
    ledger.commit.side_effect = RuntimeError("database unavailable")
    quarantined = []
    events = []
    agent = SimpleNamespace()
    lifecycle = TurnRoutingLifecycle(
        agent=agent,
        request=turn_routing_runtime.TurnRoutingRequest(
            surface="tui",
            session_id="session-1",
            quarantine=lambda routed_agent, reason: quarantined.append(
                (routed_agent, reason)
            ),
            emit=lambda event, payload: events.append((event, payload)),
        ),
        turn_id="turn-accounting-failed",
        budget_context=BudgetRouteContext(
            ledger=ledger,
            reservation=SimpleNamespace(
                reservation_id="reservation-1",
                state="reserved",
            ),
            cooldown_seconds=60,
        ),
        budget_state="reserved",
    )
    lifecycle.provider_submission_started("request-1")

    with pytest.raises(turn_routing_runtime.RouteBudgetDispatchBlocked) as blocked:
        lifecycle.provider_submission_accepted(
            SimpleNamespace(id="provider-response-1"),
            "request-1",
        )

    assert blocked.value.budget_state == "accounting_failed"
    assert lifecycle.budget_state == "accounting_failed"
    assert quarantined == [(agent, "route_budget_accounting_failed")]
    assert events[-1][0] == "route.degraded"
    assert events[-1][1]["reason_code"] == "route_budget_accounting_failed"


def test_snapshot_agent_model_runtime_deep_copies_primary_runtime():
    agent = _FakeAgent()

    snapshot = snapshot_agent_model_runtime(agent)
    agent._primary_runtime["nested"]["generation"] = 2

    assert snapshot["primary_runtime"]["nested"]["generation"] == 1


def test_restore_agent_model_runtime_prefers_primary_runtime_snapshot():
    agent = _FakeAgent()
    snapshot = snapshot_agent_model_runtime(agent)
    agent.model = "deep"
    agent.provider = "moa"
    agent._primary_runtime = {"model": "deep"}

    restore_agent_model_runtime(agent, snapshot)

    assert agent.restored_primary is True
    assert agent.model == "k3"
    assert agent._fallback_activated is True
    assert agent._rate_limited_until == 0
    assert agent.switch_calls == []


def test_prepare_turn_route_observe_emits_turn_scoped_decision_without_apply():
    from agent.turn_routing_runtime import TurnRoutingRequest

    events = []
    request = TurnRoutingRequest(
        surface="tui",
        session_id="session-1",
        user_text="Architect a high-risk cross-system migration",
        config_loader=lambda: {
            "mode": "observe",
            "default_route": "k3",
            "routes": {
                "k3": {
                    "kind": "model",
                    "provider": "kimi-coding",
                    "model": "k3",
                },
                "deep-moa": {"kind": "moa", "preset": "deep"},
            },
            "lanes": {"plain": "k3", "deep": "deep-moa"},
        },
        emit=lambda event, payload: events.append((event, payload)),
    )
    agent = SimpleNamespace(
        model="k3",
        provider="kimi-coding",
        switch_model=MagicMock(
            side_effect=AssertionError("observe mode must not apply a route")
        ),
    )

    token = prepare_turn_route(
        agent,
        request,
        user_message=request.user_text,
        turn_id="turn-1",
    )

    assert token is None
    assert events == [
        (
            "route.decided",
            {
                "session_id": "session-1",
                "turn_id": "turn-1",
                "surface": "tui",
                "route": "deep-moa",
                "target": {"kind": "moa", "preset": "deep", "enabled": True},
                "mode": "observe",
                "source": "rule",
                "reason_code": "architecture_complexity",
                "confidence": 0.9,
                "should_apply": False,
                "requires_confirmation": False,
            },
        )
    ]
    agent.switch_model.assert_not_called()


def test_prepare_turn_route_emits_monotonic_session_turn_sequence():
    events = []
    state = turn_routing_runtime.TurnRoutingSessionState()
    request = turn_routing_runtime.TurnRoutingRequest(
        surface="tui",
        session_id="session-1",
        user_text="Summarize this note",
        config_loader=lambda: {
            "mode": "observe",
            "routes": {"k3": {"kind": "model", "provider": "kimi-coding", "model": "k3"}},
            "lanes": {"plain": "k3"},
        },
        emit=lambda event, payload: events.append((event, payload)),
        session_state=state,
    )
    agent = SimpleNamespace(model="k3", provider="kimi-coding")

    for turn_id in ("turn-1", "turn-2"):
        assert prepare_turn_route(
            agent,
            request,
            user_message=request.user_text,
            turn_id=turn_id,
        ) is None

    assert [payload["turn_sequence"] for event, payload in events if event == "route.decided"] == [1, 2]
    assert state.turn_sequence == 2


def test_turn_routing_lifecycle_keeps_one_sequence_for_terminal_event():
    events = []
    state = turn_routing_runtime.TurnRoutingSessionState()
    request = turn_routing_runtime.TurnRoutingRequest(
        surface="tui",
        session_id="session-1",
        user_text="Summarize this note",
        config_loader=lambda: {"mode": "observe"},
        emit=lambda event, payload: events.append((event, payload)),
        session_state=state,
    )
    agent = SimpleNamespace(model="k3", provider="kimi-coding")

    with turn_routing_runtime.turn_routing_lifecycle(agent, request) as lifecycle:
        lifecycle.prepare(agent, user_message=request.user_text, turn_id="turn-1")

    assert [event for event, _payload in events] == ["route.decided", "route.completed"]
    assert {payload["turn_sequence"] for _event, payload in events} == {1}


def test_prepare_turn_route_same_current_is_noop_without_transient_runtime():
    from agent.turn_routing_runtime import TurnRoutingRequest

    events = []
    request = TurnRoutingRequest(
        surface="tui",
        session_id="session-1",
        user_text="Summarize this note",
        allow_automatic=True,
        config_loader=lambda: {
            "mode": "auto",
            "default_route": "k3",
            "routes": {
                "k3": {
                    "kind": "model",
                    "provider": "kimi-coding",
                    "model": "k3",
                }
            },
            "lanes": {"plain": "k3", "deep": "k3"},
        },
        emit=lambda event, payload: events.append((event, payload)),
    )
    agent = SimpleNamespace(model="k3", provider="kimi-coding")

    token = prepare_turn_route(
        agent,
        request,
        user_message=request.user_text,
        turn_id="turn-current",
    )

    assert token is None
    assert [event for event, _payload in events] == ["route.decided"]
    assert events[0][1]["reason_code"] == "same_current"
    assert events[0][1]["should_apply"] is False


def test_prepare_turn_route_auto_builds_one_token_and_emits_apply(monkeypatch):
    from agent.turn_routing_runtime import TurnRoutingRequest

    events = []
    token = object()
    builder = MagicMock(return_value=token)
    monkeypatch.setattr(turn_routing_runtime, "build_transient_route", builder)
    request = TurnRoutingRequest(
        surface="tui",
        session_id="session-1",
        user_text="Summarize this note",
        allow_automatic=True,
        config_loader=lambda: {
            "mode": "auto",
            "default_route": "k3",
            "routes": {
                "k3": {
                    "kind": "model",
                    "provider": "kimi-coding",
                    "model": "k3",
                }
            },
            "lanes": {"plain": "k3", "deep": "k3"},
        },
        emit=lambda event, payload: events.append((event, payload)),
    )
    agent = SimpleNamespace(model="Terra", provider="nous")

    applied = prepare_turn_route(
        agent,
        request,
        user_message=request.user_text,
        turn_id="turn-auto",
    )

    assert applied is token
    builder.assert_called_once()
    decision = builder.call_args.args[1]
    assert decision.route == "k3"
    assert decision.should_apply is True
    assert [event for event, _payload in events] == [
        "route.decided",
        "route.applied",
    ]
    assert events[0][1]["turn_id"] == "turn-auto"
    assert events[1][1] == events[0][1]


def test_deterministic_rule_does_not_call_classifier(monkeypatch):
    classifier = MagicMock(side_effect=AssertionError("classifier must not run"))
    monkeypatch.setattr("agent.turn_router.classify_ambiguous_turn", classifier)
    request = turn_routing_runtime.TurnRoutingRequest(
        surface="tui",
        config_loader=lambda: {
            "mode": "observe",
            "routes": {"deep": {"kind": "moa", "preset": "deep"}},
            "lanes": {"deep": "deep"},
            "classifier": {"enabled": True},
        },
    )

    assert prepare_turn_route(
        SimpleNamespace(model="k3", provider="kimi-coding"),
        request,
        user_message="Design a high-risk production architecture migration",
        turn_id="turn-deterministic",
    ) is None
    classifier.assert_not_called()


def test_ambiguous_turn_calls_classifier_once_and_fallback_keeps_runtime(monkeypatch):
    fallback = RouteDecision(
        route="current",
        target={"kind": "current", "enabled": True},
        mode="auto",
        source="classifier",
        reason_code="classifier_unavailable",
        confidence=0.0,
        should_apply=False,
    )
    classifier = MagicMock(return_value=fallback)
    monkeypatch.setattr("agent.turn_router.classify_ambiguous_turn", classifier)
    agent = SimpleNamespace(model="k3", provider="kimi-coding")
    request = turn_routing_runtime.TurnRoutingRequest(
        surface="gateway",
        config_loader=lambda: {
            "mode": "auto",
            "classifier": {"enabled": True},
        },
    )

    assert prepare_turn_route(
        agent,
        request,
        user_message="Please help with this request",
        turn_id="turn-classifier-fallback",
    ) is None
    classifier.assert_called_once()
    assert (agent.provider, agent.model) == ("kimi-coding", "k3")


def test_explicit_turn_never_calls_classifier(monkeypatch):
    classifier = MagicMock(side_effect=AssertionError("classifier must not run"))
    monkeypatch.setattr("agent.turn_router.classify_ambiguous_turn", classifier)
    request = turn_routing_runtime.TurnRoutingRequest(
        surface="cli",
        explicit_turn_override=True,
        explicit_target={
            "kind": "model",
            "provider": "kimi-coding",
            "model": "k3",
        },
        config_loader=lambda: {
            "mode": "auto",
            "routes": {
                "selected": {
                    "kind": "model",
                    "provider": "kimi-coding",
                    "model": "k3",
                }
            },
            "classifier": {"enabled": True},
        },
    )

    assert prepare_turn_route(
        SimpleNamespace(model="k3", provider="kimi-coding"),
        request,
        user_message="use this model",
        turn_id="turn-explicit-no-classifier",
    ) is None
    classifier.assert_not_called()


@pytest.mark.parametrize(
    ("request_flag", "provider", "model", "reason_code", "target"),
    [
        (
            "explicit_turn_override",
            "openai-codex",
            "gpt-5.6-sol",
            "explicit_turn_override",
            {
                "kind": "model",
                "enabled": True,
                "provider": "openai-codex",
                "model": "gpt-5.6-sol",
            },
        ),
        (
            "explicit_moa_override",
            "moa",
            "deep",
            "explicit_moa_override",
            {"kind": "moa", "enabled": True, "preset": "deep"},
        ),
        (
            "session_pinned",
            "kimi-coding",
            "k3-256k",
            "session_pin",
            {
                "kind": "model",
                "enabled": True,
                "provider": "kimi-coding",
                "model": "k3-256k",
            },
        ),
        (
            "manual_mode",
            "kimi-coding",
            "k3",
            "manual_mode",
            {
                "kind": "model",
                "enabled": True,
                "provider": "kimi-coding",
                "model": "k3",
            },
        ),
    ],
)
def test_prepare_turn_route_explicit_selection_precedes_config_and_policy(
    request_flag,
    provider,
    model,
    reason_code,
    target,
):
    from agent.turn_routing_runtime import TurnRoutingRequest

    events = []
    config_loader = MagicMock(
        return_value={"mode": "auto", "routes": {"selected": target}}
    )
    request = TurnRoutingRequest(
        surface="tui",
        session_id="session-1",
        user_text="Architect a high-risk migration",
        config_loader=config_loader,
        emit=lambda event, payload: events.append((event, payload)),
        **{request_flag: True},
    )
    agent = SimpleNamespace(model=model, provider=provider)

    token = prepare_turn_route(
        agent,
        request,
        user_message=request.user_text,
        turn_id="turn-explicit",
    )

    assert token is None
    config_loader.assert_called_once_with()
    assert events == [
        (
            "route.decided",
            {
                "session_id": "session-1",
                "turn_id": "turn-explicit",
                "surface": "tui",
                "route": "current",
                "target": target,
                "mode": "manual",
                "source": "explicit",
                "reason_code": reason_code,
                "confidence": 1.0,
                "should_apply": False,
                "requires_confirmation": False,
            },
        )
    ]


@pytest.mark.parametrize(
    ("flags", "provider", "model", "target", "expected_reason"),
    [
        (
            {
                "explicit_moa_override": True,
                "explicit_turn_override": True,
                "session_pinned": True,
                "manual_mode": True,
            },
            "moa",
            "deep",
            {"kind": "moa", "preset": "deep"},
            "explicit_moa_override",
        ),
        (
            {
                "explicit_turn_override": True,
                "session_pinned": True,
                "manual_mode": True,
            },
            "openai-codex",
            "gpt-5.6-sol",
            {
                "kind": "model",
                "provider": "openai-codex",
                "model": "gpt-5.6-sol",
            },
            "explicit_turn_override",
        ),
        (
            {"session_pinned": True, "manual_mode": True},
            "kimi-coding",
            "k3-256k",
            None,
            "session_pin",
        ),
        (
            {"manual_mode": True},
            "kimi-coding",
            "k3",
            None,
            "manual_mode",
        ),
    ],
)
def test_explicit_precedence_matrix_with_overlapping_intent_facts(
    flags,
    provider,
    model,
    target,
    expected_reason,
):
    events = []
    allowed_target = target or {
        "kind": "moa" if provider == "moa" else "model",
        **(
            {"preset": model}
            if provider == "moa"
            else {"provider": provider, "model": model}
        ),
    }
    request = turn_routing_runtime.TurnRoutingRequest(
        surface="matrix",
        user_text="Architect a high-risk migration",
        explicit_target=target,
        config_loader=lambda: {
            "mode": "auto",
            "default_route": "automatic-deep",
            "routes": {
                "allowed-explicit": allowed_target,
                "automatic-deep": {"kind": "moa", "preset": "frontier"},
            },
        },
        emit=lambda event, payload: events.append((event, payload)),
        **flags,
    )

    token = prepare_turn_route(
        SimpleNamespace(provider=provider, model=model),
        request,
        user_message=request.user_text,
        turn_id=f"turn-{expected_reason}",
    )

    assert token is None
    assert [event for event, _payload in events] == ["route.decided"]
    assert events[0][1]["source"] == "explicit"
    assert events[0][1]["reason_code"] == expected_reason
    assert events[0][1]["should_apply"] is False


def test_real_turn_request_keeps_configured_auto_in_observe_only_mode(monkeypatch):
    builder = MagicMock()
    monkeypatch.setattr(turn_routing_runtime, "build_transient_route", builder)
    events = []
    decision_box = {}
    request = turn_routing_runtime.TurnRoutingRequest(
        surface="gateway",
        user_text="Review this security-sensitive architecture deeply",
        config_loader=lambda: {
            "mode": "auto",
            "default_route": "deep",
            "routes": {"deep": {"kind": "moa", "preset": "deep"}},
        },
        emit=lambda event, payload: events.append((event, payload)),
    )

    token = prepare_turn_route(
        SimpleNamespace(provider="kimi-coding", model="k3"),
        request,
        user_message=request.user_text,
        turn_id="turn-auto-locked",
        decision_sink=lambda decision, _budget: decision_box.setdefault(
            "decision", decision
        ),
    )

    assert token is None
    assert decision_box["decision"].mode == "observe"
    assert decision_box["decision"].should_apply is False
    assert events[0][1]["mode"] == "observe"
    builder.assert_not_called()


def test_typed_explicit_target_is_applied_by_shared_lifecycle(monkeypatch):
    agent = SimpleNamespace(model="k3", provider="kimi-coding")
    token = object()
    builder = MagicMock(return_value=token)
    monkeypatch.setattr(turn_routing_runtime, "build_transient_route", builder)
    events = []
    config_loader = MagicMock(
        return_value={
            "mode": "auto",
            "routes": {
                "sol": {
                    "kind": "model",
                    "provider": "openai-codex",
                    "model": "gpt-5.6-sol",
                }
            },
        }
    )
    request = turn_routing_runtime.TurnRoutingRequest(
        surface="tui",
        session_id="session-1",
        explicit_turn_override=True,
        explicit_target={
            "kind": "model",
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
        },
        config_loader=config_loader,
        emit=lambda event, payload: events.append((event, payload)),
    )

    applied = prepare_turn_route(
        agent,
        request,
        user_message="Implement this change",
        turn_id="turn-explicit-once",
    )

    assert applied is token
    config_loader.assert_called_once_with()
    builder.assert_called_once()
    decision = builder.call_args.args[1]
    assert decision.route == "explicit"
    assert decision.target == {
        "kind": "model",
        "enabled": True,
        "provider": "openai-codex",
        "model": "gpt-5.6-sol",
    }
    assert decision.should_apply is True
    assert [event for event, _payload in events] == [
        "route.decided",
        "route.applied",
    ]


@pytest.mark.parametrize(
    ("routes", "expected_reason"),
    [
        (
            {
                "sol": {
                    "kind": "model",
                    "provider": "openai-codex",
                    "model": "gpt-5.6-sol",
                    "enabled": False,
                }
            },
            "target_not_allowed",
        ),
        ({}, None),
    ],
)
def test_explicit_target_uses_configured_enabled_routes_as_core_allowlist(
    monkeypatch,
    routes,
    expected_reason,
):
    builder = MagicMock(return_value=object())
    monkeypatch.setattr(turn_routing_runtime, "build_transient_route", builder)
    request = turn_routing_runtime.TurnRoutingRequest(
        surface="gateway",
        explicit_turn_override=True,
        explicit_target={
            "kind": "model",
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
        },
        config_loader=lambda: {"mode": "off", "routes": routes},
    )
    agent = SimpleNamespace(model="k3", provider="kimi-coding")

    if expected_reason is None:
        assert prepare_turn_route(
            agent,
            request,
            user_message="use sol once",
            turn_id="turn-legacy-explicit",
        ) is not None
        builder.assert_called_once()
    else:
        with pytest.raises(turn_routing_runtime.RouteAuthorizationError) as exc_info:
            prepare_turn_route(
                agent,
                request,
                user_message="use sol once",
                turn_id="turn-disabled-explicit",
            )
        assert exc_info.value.decision.reason_code == expected_reason
        builder.assert_not_called()


def test_explicit_target_fails_closed_when_core_allowlist_is_unavailable(monkeypatch):
    builder = MagicMock()
    monkeypatch.setattr(turn_routing_runtime, "build_transient_route", builder)
    request = turn_routing_runtime.TurnRoutingRequest(
        surface="cli",
        explicit_turn_override=True,
        explicit_target={
            "kind": "model",
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
        },
        config_loader=MagicMock(side_effect=OSError("config unavailable")),
    )

    with pytest.raises(turn_routing_runtime.RouteAuthorizationError) as exc_info:
        prepare_turn_route(
            SimpleNamespace(model="k3", provider="kimi-coding"),
            request,
            user_message="use sol once",
            turn_id="turn-allowlist-unavailable",
        )

    assert exc_info.value.decision.reason_code == "allowlist_unavailable"
    builder.assert_not_called()


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        RouteAuthorization(allowed=False, reason_code="target_not_entitled"),
    ],
)
def test_explicit_override_cannot_bypass_denial_or_budget_reservation(authorization):
    agent = SimpleNamespace(model="grok-4.5", provider="xai")
    emitted = []
    provider_dispatch = MagicMock()
    message_preparer = MagicMock()
    request = turn_routing_runtime.TurnRoutingRequest(
        surface="tui",
        session_id="session-1",
        explicit_turn_override=True,
        explicit_target={
            "kind": "model",
            "provider": "xai",
            "model": "grok-4.5",
        },
        authorization=authorization,
        prepare_user_message=message_preparer,
        config_loader=lambda: {"mode": "auto"},
        emit=lambda event, payload: emitted.append((event, payload)),
    )

    with pytest.raises(turn_routing_runtime.RouteAuthorizationError):
        with turn_routing_runtime.turn_routing_lifecycle(agent, request) as lifecycle:
            lifecycle.prepare(
                agent,
                user_message="Review this patch",
                turn_id="turn-explicit-denied",
            )
            provider_dispatch()

    provider_dispatch.assert_not_called()
    message_preparer.assert_not_called()
    assert agent.model == "grok-4.5"
    assert agent.provider == "xai"
    assert emitted[0][0] == "route.decided"
    assert emitted[0][1]["should_apply"] is False
    assert emitted[0][1]["requires_confirmation"] is False

    allowed_request = replace(
        request,
        authorization=None,
        config_loader=lambda: {
            "mode": "auto",
            "budget": {"grok_weekly_limit": 1},
        },
        emit=None,
    )
    assert prepare_turn_route(
        agent,
        allowed_request,
        user_message="Review this patch",
        turn_id="turn-explicit-allowed",
    ) is None


@pytest.mark.parametrize(
    ("allowed_routes", "allowed"),
    [
        (frozenset({"deep"}), True),
        (frozenset({"k3"}), False),
    ],
)
def test_explicit_selection_cannot_bypass_allowed_routes(allowed_routes, allowed):
    agent = SimpleNamespace(model="deep", provider="moa")
    provider_dispatch = MagicMock()
    request = turn_routing_runtime.TurnRoutingRequest(
        surface="tui",
        session_id="session-1",
        explicit_moa_override=True,
        allowed_routes=allowed_routes,
        config_loader=lambda: {
            "mode": "auto",
            "routes": {
                "k3": {"kind": "model", "provider": "kimi-coding", "model": "k3"},
                "deep": {"kind": "moa", "preset": "deep"},
            },
        },
    )

    if allowed:
        with turn_routing_runtime.turn_routing_lifecycle(agent, request) as lifecycle:
            lifecycle.prepare(
                agent,
                user_message="review",
                turn_id="turn-explicit-allowed",
            )
            provider_dispatch()
        provider_dispatch.assert_called_once()
    else:
        with pytest.raises(turn_routing_runtime.RouteAuthorizationError) as exc_info:
            with turn_routing_runtime.turn_routing_lifecycle(agent, request) as lifecycle:
                lifecycle.prepare(
                    agent,
                    user_message="review",
                    turn_id="turn-explicit-denied",
                )
                provider_dispatch()
        provider_dispatch.assert_not_called()
        assert exc_info.value.decision.reason_code == "target_not_allowed"


def test_explicit_target_mismatch_cannot_hide_budgeted_resident_runtime():
    agent = _TransientRuntimeAgent()
    agent.provider = "xai"
    agent.model = "grok-4.5"
    request = turn_routing_runtime.TurnRoutingRequest(
        surface="tui",
        explicit_turn_override=True,
        explicit_target={
            "kind": "model",
            "provider": "kimi-coding",
            "model": "k3",
        },
        authorization=RouteAuthorization(
            allowed=True,
            reason_code="entitled",
        ),
    )

    with pytest.raises(turn_routing_runtime.RouteAuthorizationError) as exc_info:
        prepare_turn_route(
            agent,
            request,
            user_message="explicit stale target",
            turn_id="turn-mismatch",
        )

    assert exc_info.value.decision.target["provider"] == "xai"
    assert exc_info.value.decision.target["model"] == "grok-4.5"
    assert exc_info.value.decision.reason_code == "budget_authorization_unavailable"


def test_unpinned_off_mode_resident_grok_still_uses_the_hard_budget_ledger(
    tmp_path,
    monkeypatch,
):
    from agent.turn_router_budget import TurnRouterBudgetLedger

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    agent = SimpleNamespace(provider="xai", model="grok-4.5")
    request = turn_routing_runtime.TurnRoutingRequest(
        surface="gateway",
        config_loader=lambda: {
            "mode": "off",
            "budget": {
                "grok_weekly_limit": 1,
                "reservation_lease_seconds": 300,
                "cooldown_seconds": 60,
            },
        },
    )

    with turn_routing_runtime.turn_routing_lifecycle(agent, request) as lifecycle:
        lifecycle.prepare(
            agent,
            user_message="ordinary unpinned turn",
            turn_id="turn-unpinned-resident-grok",
        )
        assert lifecycle.token is None
        assert lifecycle.budget_context is not None
        assert lifecycle.budget_context.protects_resident_fallback is True
        assert lifecycle.budget_state == "reserved"
        assert lifecycle.decision is not None
        assert lifecycle.decision.target["provider"] == "xai"
        assert lifecycle.decision.target["model"] == "grok-4.5"

        with lifecycle.provider_submission_scope(agent, "attempt-unpinned-resident"):
            agent._turn_route_budget_submission_started()
            agent._turn_route_budget_submission_accepted(
                SimpleNamespace(id="response-unpinned-resident")
            )

    status = TurnRouterBudgetLedger(weekly_limit=1).status()
    assert status.committed_slots == 1
    assert status.reserved_slots == 0


def test_unpinned_resident_grok_is_hard_denied_when_budget_is_disabled(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    agent = SimpleNamespace(provider="xai", model="grok-4.5")
    request = turn_routing_runtime.TurnRoutingRequest(
        surface="tui",
        config_loader=lambda: {
            "mode": "off",
            "budget": {"grok_weekly_limit": 0},
        },
    )

    with pytest.raises(turn_routing_runtime.RouteAuthorizationError) as exc_info:
        with turn_routing_runtime.turn_routing_lifecycle(agent, request) as lifecycle:
            lifecycle.prepare(
                agent,
                user_message="ordinary unpinned turn",
                turn_id="turn-unpinned-resident-denied",
            )

    assert exc_info.value.decision.reason_code == "budget_authorization_unavailable"
    assert exc_info.value.decision.target["provider"] == "xai"
    assert exc_info.value.decision.target["model"] == "grok-4.5"


def test_explicit_target_mismatch_rejects_forged_resident_budget_authorization(
    monkeypatch,
):
    builder = MagicMock(return_value=object())
    monkeypatch.setattr(turn_routing_runtime, "build_transient_route", builder)
    request = turn_routing_runtime.TurnRoutingRequest(
        surface="gateway",
        explicit_turn_override=True,
        explicit_target={
            "kind": "model",
            "provider": "kimi-coding",
            "model": "k3",
        },
        authorization=RouteAuthorization(
            allowed=True,
            reason_code="caller_claimed_authorized",
            reservation_id="forged-reservation",
        ),
        config_loader=lambda: {
            "mode": "off",
            "budget": {"grok_weekly_limit": 0},
        },
    )

    with pytest.raises(turn_routing_runtime.RouteAuthorizationError) as exc_info:
        prepare_turn_route(
            SimpleNamespace(provider="xai", model="grok-4.5"),
            request,
            user_message="switch away from grok",
            turn_id="turn-forged-resident-budget",
        )

    assert exc_info.value.decision.reason_code == "budget_authorization_unavailable"
    builder.assert_not_called()


def test_budgeted_resident_guard_releases_when_nonbudget_target_applies(
    tmp_path,
    monkeypatch,
):
    from agent.turn_router_budget import TurnRouterBudgetLedger

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    token = SimpleNamespace(restore=MagicMock(return_value=True), decision=None)
    monkeypatch.setattr(
        turn_routing_runtime,
        "build_transient_route",
        MagicMock(return_value=token),
    )
    request = turn_routing_runtime.TurnRoutingRequest(
        surface="gateway",
        explicit_turn_override=True,
        explicit_target={
            "kind": "model",
            "provider": "kimi-coding",
            "model": "k3",
        },
        config_loader=lambda: {
            "mode": "off",
            "budget": {"grok_weekly_limit": 1},
        },
    )
    agent = SimpleNamespace(provider="xai", model="grok-4.5")

    with turn_routing_runtime.turn_routing_lifecycle(agent, request) as lifecycle:
        lifecycle.prepare(
            agent,
            user_message="switch away from grok",
            turn_id="turn-resident-switch-success",
        )
        assert lifecycle.token is token
        assert lifecycle.budget_context is None

    status = TurnRouterBudgetLedger(weekly_limit=1).status()
    assert status.committed_slots == 0
    assert status.reserved_slots == 0
    assert token.restore.call_count == 1


def test_budgeted_resident_guard_survives_apply_failure_until_provider_acceptance(
    tmp_path,
    monkeypatch,
):
    from agent.turn_router_budget import TurnRouterBudgetLedger

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(
        turn_routing_runtime,
        "build_transient_route",
        MagicMock(side_effect=RuntimeError("apply failed")),
    )
    request = turn_routing_runtime.TurnRoutingRequest(
        surface="gateway",
        explicit_turn_override=True,
        explicit_target={
            "kind": "model",
            "provider": "kimi-coding",
            "model": "k3",
        },
        config_loader=lambda: {
            "mode": "off",
            "budget": {"grok_weekly_limit": 1},
        },
    )
    agent = SimpleNamespace(provider="xai", model="grok-4.5")

    with turn_routing_runtime.turn_routing_lifecycle(agent, request) as lifecycle:
        lifecycle.prepare(
            agent,
            user_message="switch away from grok",
            turn_id="turn-resident-switch-failed",
        )
        assert lifecycle.token is None
        assert lifecycle.budget_context is not None
        assert lifecycle.budget_context.protects_resident_fallback is True
        assert lifecycle.budget_state == "reserved"
        with lifecycle.provider_submission_scope(agent, "attempt-resident-fallback"):
            agent._turn_route_budget_submission_started()
            agent._turn_route_budget_submission_accepted(
                SimpleNamespace(id="response-resident-fallback")
            )
        assert lifecycle.budget_state == "committed"

    status = TurnRouterBudgetLedger(weekly_limit=1).status()
    assert status.committed_slots == 1
    assert status.reserved_slots == 0


def test_prepare_turn_route_budgeted_target_requires_authorization_before_builder(
    monkeypatch,
):
    from agent.turn_router import RouteAuthorization
    from agent.turn_routing_runtime import TurnRoutingRequest

    events = []
    builder = MagicMock(return_value=object())
    monkeypatch.setattr(turn_routing_runtime, "build_transient_route", builder)
    config = {
        "mode": "auto",
        "default_route": "grok-review",
        "routes": {
            "grok-review": {
                "kind": "model",
                "provider": "xai",
                "model": "grok-4.5",
                "budgeted": True,
            }
        },
    }
    agent = SimpleNamespace(model="k3", provider="kimi-coding")

    denied_request = TurnRoutingRequest(
        surface="tui",
        user_text="Review this patch",
        allow_automatic=True,
        config_loader=lambda: config,
        emit=lambda event, payload: events.append((event, payload)),
    )
    assert prepare_turn_route(
        agent,
        denied_request,
        user_message=denied_request.user_text,
        turn_id="turn-denied",
    ) is None
    builder.assert_not_called()
    assert events[-1][1]["reason_code"] == "budget_authorization_unavailable"
    assert events[-1][1]["requires_confirmation"] is False

    allowed_config = {**config, "budget": {"grok_weekly_limit": 1}}
    allowed_request = TurnRoutingRequest(
        surface="tui",
        user_text="Review this patch",
        allow_automatic=True,
        config_loader=lambda: allowed_config,
        authorization=RouteAuthorization(
            allowed=True,
            reason_code="budget_reserved",
            reservation_id="reservation-1",
        ),
    )
    captured = []
    token = prepare_turn_route(
        agent,
        allowed_request,
        user_message=allowed_request.user_text,
        turn_id="turn-allowed",
        decision_sink=lambda decision, context: captured.append((decision, context)),
    )
    assert token is builder.return_value
    assert captured[0][1] is not None
    assert captured[0][1].reservation.reservation_id != "reservation-1"
    builder.assert_called_once()


def test_prepare_turn_route_allowed_routes_hard_gate_denies_unlisted_selection(
    monkeypatch,
):
    from agent.turn_routing_runtime import TurnRoutingRequest

    events = []
    builder = MagicMock(return_value=object())
    monkeypatch.setattr(turn_routing_runtime, "build_transient_route", builder)
    request = TurnRoutingRequest(
        surface="tui",
        user_text="Architect a high-risk cross-system migration",
        allowed_routes=frozenset({"k3"}),
        config_loader=lambda: {
            "mode": "auto",
            "default_route": "k3",
            "routes": {
                "k3": {"kind": "model", "provider": "kimi-coding", "model": "k3"},
                "deep": {"kind": "moa", "preset": "deep"},
            },
            "lanes": {"plain": "k3", "deep": "deep"},
        },
        emit=lambda event, payload: events.append((event, payload)),
    )

    assert prepare_turn_route(
        SimpleNamespace(model="k3", provider="kimi-coding"),
        request,
        user_message=request.user_text,
        turn_id="turn-unlisted",
    ) is None
    builder.assert_not_called()
    assert events[0][0] == "route.decided"
    assert events[0][1]["route"] == "deep"
    assert events[0][1]["reason_code"] == "target_not_allowed"
    assert events[0][1]["should_apply"] is False


class _RuntimeCompressor:
    def __init__(self):
        self.model = "k3"
        self.context_length = 1_000_000
        self.base_url = "https://primary.invalid"
        self.api_key = "primary-key"
        self.provider = "kimi-coding"
        self.api_mode = "chat_completions"
        self.threshold_tokens = 800_000


class _TransientRuntimeAgent:
    def __init__(self):
        self.model = "k3"
        self.provider = "kimi-coding"
        self.requested_provider = "kimi-coding"
        self.base_url = "https://primary.invalid"
        self.api_mode = "chat_completions"
        self.api_key = "primary-key"
        self.client = object()
        self._anthropic_client = None
        self._anthropic_api_key = None
        self._anthropic_base_url = None
        self._is_anthropic_oauth = False
        self._client_kwargs = {"api_key": "primary-key"}
        self._credential_pool = object()
        self._credential_pool_entry_id = "primary-entry"
        self._config_context_length = 1_000_000
        self._use_prompt_caching = True
        self._use_native_cache_layout = True
        self.reasoning_config = {"effort": "high"}
        self._transport_cache = {"chat_completions": object()}
        self.context_compressor = _RuntimeCompressor()

        # Guard-only state: request-scoped activation must never mutate these.
        self._cached_system_prompt: str | None = "SYSTEM-BYTES"
        self._primary_runtime = {"model": "k3", "nested": {"generation": 1}}
        self._fallback_chain = [{"provider": "safe"}]
        self._fallback_index = 0
        self._fallback_activated = False
        self._rate_limited_until = 123.5
        self._consecutive_stale_streams = 0


def _mutate_transient_runtime(agent, transient_client):
    agent.model = "deep"
    agent.provider = "moa"
    agent.requested_provider = "moa"
    agent.base_url = "moa://local"
    agent.api_mode = "chat_completions"
    agent.api_key = "moa-virtual-provider"
    agent.client = transient_client
    agent._client_kwargs = {}
    agent._credential_pool = None
    agent._credential_pool_entry_id = None
    agent._config_context_length = None
    agent._use_prompt_caching = False
    agent._use_native_cache_layout = False
    agent.reasoning_config = {"effort": "xhigh"}
    agent._transport_cache.clear()
    agent.context_compressor.model = "deep"
    agent.context_compressor.context_length = 200_000
    agent.context_compressor.base_url = "moa://local"
    agent.context_compressor.api_key = "moa-virtual-provider"
    agent.context_compressor.provider = "moa"
    agent.context_compressor.api_mode = "chat_completions"
    agent.context_compressor.threshold_tokens = 160_000


def test_transient_runtime_token_restores_exact_state_without_clearing_cooldown():
    agent = _TransientRuntimeAgent()
    old_client = agent.client
    old_pool = agent._credential_pool
    old_transport_cache = agent._transport_cache
    old_transport_items = dict(old_transport_cache)
    old_compressor = agent.context_compressor
    transient_client = SimpleNamespace(close=MagicMock())

    snapshot = turn_routing_runtime.TransientRuntimeSnapshot.capture(agent)
    _mutate_transient_runtime(agent, transient_client)
    token = turn_routing_runtime.TransientRouteToken(
        agent=agent,
        snapshot=snapshot,
        transient_resources=(transient_client,),
    )

    assert token.restore() is True
    assert token.restore() is True  # idempotent finalization
    assert agent.model == "k3"
    assert agent.provider == "kimi-coding"
    assert agent.requested_provider == "kimi-coding"
    assert agent.base_url == "https://primary.invalid"
    assert agent.api_key == "primary-key"
    assert agent.client is old_client
    assert agent._credential_pool is old_pool
    assert agent._credential_pool_entry_id == "primary-entry"
    assert agent._config_context_length == 1_000_000
    assert agent._use_prompt_caching is True
    assert agent._use_native_cache_layout is True
    assert agent.reasoning_config == {"effort": "high"}
    assert agent._transport_cache is old_transport_cache
    assert agent._transport_cache == old_transport_items
    assert agent.context_compressor is old_compressor
    assert vars(agent.context_compressor) == vars(_RuntimeCompressor())
    assert agent._cached_system_prompt == "SYSTEM-BYTES"
    assert agent._primary_runtime == {"model": "k3", "nested": {"generation": 1}}
    assert agent._fallback_chain == [{"provider": "safe"}]
    assert agent._fallback_index == 0
    assert agent._fallback_activated is False
    assert agent._rate_limited_until == 123.5
    transient_client.close.assert_called_once_with()


def test_transient_runtime_token_detects_and_repairs_guard_state_mutation():
    agent = _TransientRuntimeAgent()
    snapshot = turn_routing_runtime.TransientRuntimeSnapshot.capture(agent)
    agent._cached_system_prompt = None
    agent._primary_runtime["nested"]["generation"] = 2
    agent._fallback_chain.append({"provider": "unsafe"})
    agent._rate_limited_until = 0

    token = turn_routing_runtime.TransientRouteToken(agent=agent, snapshot=snapshot)

    assert token.restore() is False
    assert agent._cached_system_prompt == "SYSTEM-BYTES"
    assert agent._primary_runtime == {"model": "k3", "nested": {"generation": 1}}
    assert agent._fallback_chain == [{"provider": "safe"}]
    assert agent._rate_limited_until == 123.5


def test_transient_snapshot_rejects_route_owned_cold_prompt_initialization():
    agent = _TransientRuntimeAgent()
    agent._cached_system_prompt = None
    snapshot = turn_routing_runtime.TransientRuntimeSnapshot.capture(agent)
    token = turn_routing_runtime.TransientRouteToken(agent=agent, snapshot=snapshot)

    agent._cached_system_prompt = "ROUTE-SPECIFIC-SYSTEM-BYTES"

    assert token.restore() is False
    assert agent._cached_system_prompt is None


@pytest.mark.parametrize("during_turn_value", [0, 4])
def test_transient_route_restores_cross_turn_stale_breaker(during_turn_value):
    agent = _TransientRuntimeAgent()
    agent._consecutive_stale_streams = 3
    snapshot = turn_routing_runtime.TransientRuntimeSnapshot.capture(agent)
    token = turn_routing_runtime.TransientRouteToken(agent=agent, snapshot=snapshot)

    agent._consecutive_stale_streams = during_turn_value

    assert token.restore() is True
    assert agent._consecutive_stale_streams == 3


def _resolved_moa_switch():
    return SimpleNamespace(
        success=True,
        new_model="deep",
        target_provider="moa",
        api_key="moa-virtual-provider",
        base_url="moa://local",
        api_mode="chat_completions",
        error_message="",
    )


def test_build_transient_route_uses_nonpersistent_helper_not_agent_switch_model():
    agent = _TransientRuntimeAgent()
    agent.switch_model = MagicMock(side_effect=AssertionError("persistent switch called"))
    resolver = MagicMock(return_value=_resolved_moa_switch())
    transient_client = SimpleNamespace(close=MagicMock())

    def runtime_switcher(target, **kwargs):
        assert target is agent
        assert kwargs["persist"] is False
        target.model = kwargs["new_model"]
        target.provider = kwargs["new_provider"]
        target.requested_provider = kwargs["new_provider"]
        target.base_url = kwargs["base_url"]
        target.api_key = kwargs["api_key"]
        target.client = transient_client

    token = turn_routing_runtime.build_transient_route(
        agent,
        _route_decision(kind="moa", value="deep"),
        switch_resolver=resolver,
        runtime_switcher=runtime_switcher,
    )

    assert isinstance(token, turn_routing_runtime.TransientRouteToken)
    agent.switch_model.assert_not_called()
    assert agent.model == "deep"
    assert agent.provider == "moa"
    assert agent._cached_system_prompt == "SYSTEM-BYTES"
    assert agent._primary_runtime == {"model": "k3", "nested": {"generation": 1}}
    assert agent._fallback_chain == [{"provider": "safe"}]
    assert agent._rate_limited_until == 123.5
    assert token.restore() is True
    transient_client.close.assert_called_once_with()


def test_real_transient_token_terminal_event_preserves_reservation_provenance():
    agent = _TransientRuntimeAgent()
    decision = RouteDecision(
        route="deep",
        target={"kind": "moa", "preset": "deep", "budgeted": True},
        mode="auto",
        source="rule",
        reason_code="architecture_complexity",
        confidence=0.9,
        should_apply=True,
    )
    decision = authorize_route(
        decision,
        RouteAuthorization(
            allowed=True,
            reason_code="budget_reserved",
            reservation_id="reservation-1",
        ),
    )

    def runtime_switcher(target, **kwargs):
        target.model = kwargs["new_model"]
        target.provider = kwargs["new_provider"]

    token = turn_routing_runtime.build_transient_route(
        agent,
        decision,
        switch_resolver=MagicMock(return_value=_resolved_moa_switch()),
        runtime_switcher=runtime_switcher,
    )
    assert isinstance(token, turn_routing_runtime.TransientRouteToken)
    with pytest.raises(AttributeError):
        token.decision = replace(
            decision,
            authorization=RouteAuthorization(
                allowed=True,
                reason_code="budget_reserved",
                reservation_id="different-reservation",
            ),
        )
    emitted = []
    lifecycle = turn_routing_runtime.TurnRoutingLifecycle(
        agent=agent,
        request=turn_routing_runtime.TurnRoutingRequest(
            surface="tui",
            session_id="session-1",
            emit=lambda event, payload: emitted.append((event, payload)),
        ),
        turn_id="session-1:task:turn",
        token=token,
        prepared=True,
    )

    lifecycle.finish()

    assert emitted == [
        (
            "route.completed",
            {
                "session_id": "session-1",
                "turn_id": "session-1:task:turn",
                "surface": "tui",
                "route": "deep",
                "target": {
                    "kind": "moa",
                    "enabled": True,
                    "preset": "deep",
                    "budgeted": True,
                },
                "mode": "auto",
                "source": "rule",
                "reason_code": "route_completed",
                "selection_reason_code": "architecture_complexity",
                "confidence": 0.9,
                "should_apply": True,
                "requires_confirmation": False,
                "authorization": {
                    "allowed": True,
                    "reason_code": "budget_reserved",
                    "reservation_id": "reservation-1",
                    "requires_confirmation": False,
                },
                "stage": "restore",
            },
        )
    ]


def test_build_transient_route_repairs_partial_apply_and_closes_new_client():
    agent = _TransientRuntimeAgent()
    original_client = agent.client
    transient_client = SimpleNamespace(close=MagicMock())

    def broken_runtime_switcher(target, **kwargs):
        target.model = kwargs["new_model"]
        target.provider = kwargs["new_provider"]
        target.client = transient_client
        target._transport_cache.clear()
        raise RuntimeError("client build failed")

    with pytest.raises(RouteApplicationError, match="client build failed"):
        turn_routing_runtime.build_transient_route(
            agent,
            _route_decision(kind="moa", value="deep"),
            switch_resolver=MagicMock(return_value=_resolved_moa_switch()),
            runtime_switcher=broken_runtime_switcher,
        )

    assert agent.model == "k3"
    assert agent.provider == "kimi-coding"
    assert agent.client is original_client
    assert agent._transport_cache
    assert agent._rate_limited_until == 123.5
    transient_client.close.assert_called_once_with()


def test_build_transient_route_current_target_is_a_true_noop():
    resolver = MagicMock()
    runtime_switcher = MagicMock()

    token = turn_routing_runtime.build_transient_route(
        _TransientRuntimeAgent(),
        _route_decision(kind="current", value="", should_apply=False),
        switch_resolver=resolver,
        runtime_switcher=runtime_switcher,
    )

    assert token is None
    resolver.assert_not_called()
    runtime_switcher.assert_not_called()
