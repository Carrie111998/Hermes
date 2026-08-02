"""Adversarial regressions for the native Responses compaction v3 blockers."""

from __future__ import annotations

import json
import logging
import sys
import types
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.modules.setdefault("fire", types.SimpleNamespace(Fire=lambda *a, **k: None))
sys.modules.setdefault("firecrawl", types.SimpleNamespace(Firecrawl=object))
sys.modules.setdefault("fal_client", types.SimpleNamespace())

import run_agent
from agent.responses_compaction import (
    MAX_COMPACTION_LEDGER_ROUTES,
    NativeCompactionLedger,
    NativeCompactionPolicy,
    NativeCompactionReadError,
    NativeCompactionRoute,
    NativeCompactionStateError,
    NativeCompactionTransitionReceipt,
    advance_policy_after_success,
    compaction_checkpoint_digest,
    compaction_route_key,
    failed_closed_transition_receipt,
    has_replayable_compaction_sidecar,
    has_unresolved_native_compaction_failure,
    load_policy_for_route,
    persist_policy_compare_and_set,
    prepare_emergency_hermes_compaction,
    reconcile_policy_for_current_route,
    record_native_compaction_transition_receipt,
    route_for_request,
    should_defer_automatic_hermes_compaction,
    stage_native_compaction_checkpoint,
    validate_compaction_lifecycle,
)
from agent.transports.codex import ResponsesApiTransport
from hermes_state import SessionDB


def _route(model: str = "gpt-5.6-sol") -> NativeCompactionRoute:
    return route_for_request(
        provider="openai-codex",
        endpoint="https://chatgpt.com/backend-api/codex",
        model=model,
    )


def _sidecar(route: NativeCompactionRoute, encrypted: str) -> list[dict]:
    return [
        {
            "type": "compaction",
            "encrypted_content": encrypted,
            "_issuer_kind": route.issuer_kind,
            "_compaction_route": route.to_dict(),
        }
    ]


def _observed_policy(
    route: NativeCompactionRoute, encrypted: str
) -> NativeCompactionPolicy:
    return advance_policy_after_success(
        NativeCompactionPolicy(route=route),
        codex_output_items=_sidecar(route, encrypted),
        replay_attempted=False,
    )


class _PersistenceExceptionDB:
    def __init__(self, state: dict | None = None):
        self.state = state or NativeCompactionLedger.empty().to_dict()
        self.cas_calls = 0

    def get_codex_responses_compaction_state(self, _session_id: str) -> dict:
        return self.state

    def compare_and_set_codex_responses_compaction_state(self, *_args, **_kwargs):
        self.cas_calls += 1
        raise RuntimeError("simulated durable write failure")


class _AlwaysConflictDB(_PersistenceExceptionDB):
    def compare_and_set_codex_responses_compaction_state(self, *_args, **_kwargs):
        self.cas_calls += 1
        return False


class _MissingRowDB(_PersistenceExceptionDB):
    def compare_and_set_codex_responses_compaction_state(self, *_args, **_kwargs):
        self.cas_calls += 1
        return False


class _ReadFailureDB:
    def __init__(self, state: dict, *, failures: int | None = None):
        self.state = state
        self.failures = failures
        self.read_calls = 0
        self.cas_calls = 0

    def get_codex_responses_compaction_state(self, _session_id: str) -> dict:
        self.read_calls += 1
        if self.failures is None or self.read_calls <= self.failures:
            raise OSError("simulated transient durable read failure")
        return self.state

    def compare_and_set_codex_responses_compaction_state(self, *_args, **_kwargs):
        self.cas_calls += 1
        raise AssertionError("read failure must not attempt persistence")


@pytest.mark.parametrize(
    ("db", "expected_calls"),
    [
        (_PersistenceExceptionDB(), 1),
        (_AlwaysConflictDB(), 2),
        (_MissingRowDB(), 2),
    ],
    ids=["persistence-exception", "second-cas-conflict", "missing-session-row"],
)
def test_terminal_transition_returns_explicit_failed_closed_receipt(
    db, expected_calls
):
    desired = NativeCompactionPolicy(route=_route()).transition(
        "unsupported", error="provider_rejected_context_management"
    )

    receipt = persist_policy_compare_and_set(db, "missing-or-broken", desired)

    assert receipt.failed_closed is True
    assert receipt.committed is False
    assert receipt.conflict_reconciled is False
    assert receipt.outcome == "failed_closed"
    assert receipt.policy.capability == "quarantined"
    assert receipt.durable_revision is None
    assert db.cas_calls == expected_calls


def _runtime_agent(monkeypatch, hermes_home, *, max_iterations: int = 5):
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(run_agent, "_hermes_home", hermes_home)
    monkeypatch.setattr(run_agent, "get_tool_definitions", lambda **kwargs: [])
    monkeypatch.setattr(run_agent, "check_toolset_requirements", lambda: {})
    agent = run_agent.AIAgent(
        model="gpt-5.6-sol",
        provider="openai-codex",
        api_mode="codex_responses",
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="test-token",
        quiet_mode=True,
        max_iterations=max_iterations,
        skip_context_files=True,
        skip_memory=True,
    )
    agent._cleanup_task_resources = lambda task_id: None
    agent._persist_session = lambda messages, history=None: None
    agent._save_trajectory = lambda messages, user_message, completed: None
    agent.request_overrides = {}
    agent._native_compaction_policy = NativeCompactionPolicy(route=_route())
    agent.codex_responses_auto_compaction = "native"
    agent.compression_enabled = True
    agent.codex_responses_compact_threshold = 200_000
    return agent


def _text_response(text: str):
    return SimpleNamespace(
        status="completed",
        incomplete_details=None,
        output_text=None,
        output=[
            SimpleNamespace(
                type="message",
                id=f"msg_{text}",
                role="assistant",
                status="completed",
                phase=None,
                content=[SimpleNamespace(type="output_text", text=text)],
            )
        ],
        usage=SimpleNamespace(input_tokens=5, output_tokens=1, total_tokens=6),
    )


def _compaction_only(encrypted: str):
    return SimpleNamespace(
        status="completed",
        incomplete_details=None,
        output_text=None,
        output=[
            SimpleNamespace(
                type="compaction",
                id=f"cmp_{encrypted}",
                encrypted_content=encrypted,
            )
        ],
        usage=SimpleNamespace(input_tokens=5, output_tokens=1, total_tokens=6),
    )


def test_v5_malformed_durable_read_blocks_fresh_agent_hermes_handoff(
    monkeypatch, tmp_path,
):
    agent = _runtime_agent(monkeypatch, tmp_path)
    db = SessionDB(db_path=tmp_path / "v5-malformed-read.db")
    session_id = db.create_session("v5-malformed", "test", model=agent.model)
    db._conn.execute(
        "UPDATE sessions SET codex_responses_compaction_state = ? WHERE id = ?",
        ('{"version":3,"revision":"invalid","routes":{}}', session_id),
    )
    db._conn.commit()
    durable_before = db._conn.execute(
        "SELECT codex_responses_compaction_state FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()[0]
    initial_policy = NativeCompactionPolicy(route=_route())
    agent._session_db = db
    agent.session_id = session_id
    agent._native_compaction_policy = initial_policy

    assert should_defer_automatic_hermes_compaction(agent, refresh=True) is True
    assert prepare_emergency_hermes_compaction(
        agent, reason="provider_context_overflow"
    ) is False

    read_status = getattr(agent, "_native_compaction_read_status", None)
    assert read_status is not None and read_status.failed_closed is True
    assert agent._native_compaction_policy == initial_policy
    durable_after = db._conn.execute(
        "SELECT codex_responses_compaction_state FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()[0]
    assert durable_after == durable_before
    db.close()


def test_v5_transient_durable_read_blocks_automatic_and_emergency_handoff(
    monkeypatch, tmp_path,
):
    agent = _runtime_agent(monkeypatch, tmp_path)
    db = _ReadFailureDB(NativeCompactionLedger.empty().to_dict())
    initial_policy = NativeCompactionPolicy(route=_route())
    agent._session_db = db
    agent.session_id = "v5-transient-read"
    agent._native_compaction_policy = initial_policy

    assert should_defer_automatic_hermes_compaction(agent, refresh=True) is True
    assert prepare_emergency_hermes_compaction(
        agent, reason="provider_payload_too_large"
    ) is False

    read_status = getattr(agent, "_native_compaction_read_status", None)
    assert read_status is not None and read_status.failed_closed is True
    assert agent._native_compaction_policy == initial_policy
    assert db.read_calls == 2
    assert db.cas_calls == 0


def test_v5_cached_owner_read_failure_preserves_custody_but_blocks_advancement(
    monkeypatch, tmp_path,
):
    owner = _observed_policy(_route(), "v5-cached-owner")
    db = _ReadFailureDB(
        NativeCompactionLedger.empty().with_policy(owner).to_dict()
    )
    agent = _runtime_agent(monkeypatch, tmp_path)
    agent._session_db = db
    agent.session_id = "v5-cached-owner"
    agent._native_compaction_policy = owner
    agent._native_compaction_request_active = True
    agent._native_compaction_replay_attempted = True

    assert should_defer_automatic_hermes_compaction(agent, refresh=True) is True
    assert prepare_emergency_hermes_compaction(
        agent, reason="provider_context_overflow"
    ) is False

    assert agent._native_compaction_policy == owner
    assert agent._native_compaction_request_active is True
    assert agent._native_compaction_replay_attempted is True
    assert db.cas_calls == 0


def test_v5_successful_reread_clears_fail_closed_read_guard(
    monkeypatch, tmp_path,
):
    owner = _observed_policy(_route(), "v5-reread-owner")
    db = _ReadFailureDB(
        NativeCompactionLedger.empty().with_policy(owner).to_dict(),
        failures=1,
    )
    agent = _runtime_agent(monkeypatch, tmp_path)
    agent._session_db = db
    agent.session_id = "v5-successful-reread"
    agent._native_compaction_policy = owner

    first = reconcile_policy_for_current_route(agent, refresh=True)
    first_status = getattr(agent, "_native_compaction_read_status", None)
    assert first == owner
    assert first_status is not None and first_status.failed_closed is True

    second = reconcile_policy_for_current_route(agent, refresh=True)
    second_status = getattr(agent, "_native_compaction_read_status", None)
    assert second == owner
    assert second_status is not None and second_status.succeeded is True
    assert db.read_calls == 2
    assert db.cas_calls == 0


def test_v5_failed_read_guard_survives_durable_boundary_loss(
    monkeypatch, tmp_path,
):
    owner = _observed_policy(_route(), "v5-boundary-loss-owner")
    db = _ReadFailureDB(
        NativeCompactionLedger.empty().with_policy(owner).to_dict()
    )
    agent = _runtime_agent(monkeypatch, tmp_path)
    agent._session_db = db
    agent.session_id = "v5-boundary-loss"
    agent._native_compaction_policy = owner

    assert reconcile_policy_for_current_route(agent, refresh=True) == owner
    failed_status = getattr(agent, "_native_compaction_read_status", None)
    assert failed_status is not None and failed_status.failed_closed is True

    # Losing the durable boundary is not a successful reread and therefore
    # cannot clear an active fail-closed custody guard.
    agent._session_db = None
    agent.session_id = None

    assert reconcile_policy_for_current_route(agent, refresh=True) == owner
    retained_status = getattr(agent, "_native_compaction_read_status", None)
    assert retained_status is failed_status
    assert retained_status.failed_closed is True
    assert should_defer_automatic_hermes_compaction(agent, refresh=True) is True
    assert prepare_emergency_hermes_compaction(
        agent, reason="provider_context_overflow"
    ) is False
    assert db.read_calls == 1
    assert db.cas_calls == 0


def test_v5_read_failure_disables_native_request_without_mutation_or_retry(
    monkeypatch, tmp_path,
):
    owner = _observed_policy(_route(), "v5-no-advance")
    db = _ReadFailureDB(
        NativeCompactionLedger.empty().with_policy(owner).to_dict()
    )
    agent = _runtime_agent(monkeypatch, tmp_path)
    agent._session_db = db
    agent.session_id = "v5-no-advance"
    agent._native_compaction_policy = owner
    agent._native_compaction_request_active = False
    agent._native_compaction_replay_attempted = False

    kwargs = agent._build_api_kwargs([{"role": "user", "content": "read once"}])

    read_status = getattr(agent, "_native_compaction_read_status", None)
    assert read_status is not None and read_status.failed_closed is True
    assert agent._native_compaction_policy == owner
    assert agent._native_compaction_request_active is False
    assert agent._native_compaction_replay_attempted is False
    assert "context_management" not in kwargs
    assert db.read_calls == 1
    assert db.cas_calls == 0


@pytest.mark.parametrize("terminal_capability", ["unsupported", "quarantined"])
def test_v5_terminal_reread_immediately_authorizes_emergency_handoff(
    monkeypatch, tmp_path, terminal_capability,
):
    owner = _observed_policy(_route(), "v5-terminal-reread-owner")
    terminal = NativeCompactionPolicy(route=_route()).transition(
        terminal_capability,
        error="durable_terminal_handoff",
    )
    db = _ReadFailureDB(
        NativeCompactionLedger.empty().with_policy(terminal).to_dict(),
        failures=1,
    )
    agent = _runtime_agent(monkeypatch, tmp_path)
    agent._session_db = db
    agent.session_id = "v5-terminal-reread"
    agent._native_compaction_policy = owner

    assert prepare_emergency_hermes_compaction(
        agent, reason="provider_context_overflow"
    ) is False
    failed_status = getattr(agent, "_native_compaction_read_status", None)
    assert failed_status is not None and failed_status.failed_closed is True

    # Boundary loss cannot clear the guard or grant automatic Hermes custody.
    agent._session_db = None
    agent.session_id = None
    assert prepare_emergency_hermes_compaction(
        agent, reason="provider_context_overflow"
    ) is False
    assert getattr(agent, "_native_compaction_read_status", None) is failed_status

    # The first successful durable terminal reread is authoritative immediately;
    # a stale pre-read ownership snapshot must not force a second invocation.
    agent._session_db = db
    agent.session_id = "v5-terminal-reread"
    emergency_authorization = prepare_emergency_hermes_compaction(
        agent, reason="provider_context_overflow"
    )
    assert emergency_authorization is not False
    reread_status = getattr(agent, "_native_compaction_read_status", None)
    assert reread_status is not None and reread_status.succeeded is True
    assert agent._native_compaction_policy == terminal
    assert db.read_calls == 2
    assert db.cas_calls == 0


def test_v6_failed_closed_receipt_survives_boundaryless_route_detour():
    route = _route()
    owner = _observed_policy(route, "v5-boundaryless-owner")
    agent = SimpleNamespace(
        api_mode="codex_responses",
        compression_enabled=True,
        codex_responses_auto_compaction="native",
        provider="openai-codex",
        base_url=route.endpoint,
        model=route.model,
        _native_compaction_policy=owner,
        _session_db=None,
        session_id=None,
        _native_compaction_request_active=True,
        _native_compaction_replay_attempted=True,
    )

    assert prepare_emergency_hermes_compaction(
        agent, reason="provider_context_overflow"
    ) is False
    receipt = getattr(agent, "_native_compaction_transition_receipt", None)
    assert receipt is not None and receipt.failed_closed is True
    assert prepare_emergency_hermes_compaction(
        agent, reason="provider_context_overflow"
    ) is False
    assert getattr(agent, "_native_compaction_transition_receipt", None) is receipt
    assert agent._native_compaction_request_active is True
    assert agent._native_compaction_replay_attempted is True

    route_b = _route("gpt-5.6-sol-route-b")
    agent.model = route_b.model
    # Route-A failure remains local, but native-mode emergency recovery still
    # requires a successful durable read for route B.
    assert prepare_emergency_hermes_compaction(
        agent, reason="provider_context_overflow"
    ) is False
    assert getattr(agent, "_native_compaction_transition_receipt", None) is receipt

    agent.model = route.model
    assert prepare_emergency_hermes_compaction(
        agent, reason="provider_context_overflow"
    ) is False
    assert getattr(agent, "_native_compaction_transition_receipt", None) is receipt

    agent.model = route_b.model
    agent._native_compaction_policy = _observed_policy(
        route_b, "v6-boundaryless-owner-b"
    )
    assert prepare_emergency_hermes_compaction(
        agent, reason="provider_context_overflow"
    ) is False
    receipt_b = getattr(agent, "_native_compaction_transition_receipt", None)
    assert receipt_b is not None and receipt_b.failed_closed is True
    assert receipt_b.policy.route == route_b

    agent.model = route.model
    assert prepare_emergency_hermes_compaction(
        agent, reason="provider_context_overflow"
    ) is False

    terminal = NativeCompactionPolicy(route=route).transition(
        "unsupported", error="durable_route_a_handoff"
    )
    db = _ReadFailureDB(
        NativeCompactionLedger.empty().with_policy(terminal).to_dict(), failures=0
    )
    agent._session_db = db
    agent.session_id = "v6-route-detour"
    route_a_authorization = prepare_emergency_hermes_compaction(
        agent, reason="provider_context_overflow"
    )
    assert route_a_authorization is not False
    assert getattr(agent, "_native_compaction_transition_receipt", None) is receipt_b

    agent._session_db = None
    agent.session_id = None
    agent.model = route_b.model
    assert prepare_emergency_hermes_compaction(
        agent, reason="provider_context_overflow"
    ) is False

    terminal_b = NativeCompactionPolicy(route=route_b).transition(
        "unsupported", error="durable_route_b_handoff"
    )
    ledger = NativeCompactionLedger.empty().with_policy(terminal).with_policy(terminal_b)
    agent._session_db = _ReadFailureDB(ledger.to_dict(), failures=0)
    agent.session_id = "v6-route-detour"
    route_b_authorization = prepare_emergency_hermes_compaction(
        agent, reason="provider_context_overflow"
    )
    assert route_b_authorization is not False
    assert getattr(agent, "_native_compaction_transition_receipt", None) is None


def test_v7_automatic_hermes_deferral_honors_current_route_receipt_and_overflow():
    route_a = _route("v7-auto-route-a")
    route_b = _route("v7-auto-route-b")
    agent = SimpleNamespace(
        api_mode="codex_responses",
        provider="openai-codex",
        base_url=route_a.endpoint,
        model=route_a.model,
        compression_enabled=True,
        codex_responses_auto_compaction="hermes",
        _session_db=None,
        session_id=None,
        _native_compaction_policy=_observed_policy(route_a, "v7-auto-owner-a"),
    )

    reconciled = reconcile_policy_for_current_route(agent, refresh=True)
    assert reconciled.capability == "quarantined"
    assert agent._native_compaction_read_status.outcome == "not_attempted"
    assert has_unresolved_native_compaction_failure(agent, route_a) is True
    assert should_defer_automatic_hermes_compaction(agent, refresh=False) is True

    # A route-local failure must not block unrelated route B.
    agent.model = route_b.model
    agent._native_compaction_policy = NativeCompactionPolicy(route=route_b).transition(
        "unsupported"
    )
    assert has_unresolved_native_compaction_failure(agent, route_b) is False
    assert should_defer_automatic_hermes_compaction(agent, refresh=False) is False

    # Capacity/malformed-registry overflow is deliberately global and must
    # protect every automatic compaction path rather than silently fail open.
    agent._native_compaction_failed_receipts_overflow = True
    assert should_defer_automatic_hermes_compaction(agent, refresh=False) is True


def test_v6_durable_receipt_resolves_only_its_route_guard():
    route_a = _route("v6-route-a")
    route_b = _route("v6-route-b")
    agent = SimpleNamespace()
    failed_a = failed_closed_transition_receipt(
        _observed_policy(route_a, "v6-a"), error="failed-a"
    )
    failed_b = failed_closed_transition_receipt(
        _observed_policy(route_b, "v6-b"), error="failed-b"
    )
    record_native_compaction_transition_receipt(agent, failed_a)
    record_native_compaction_transition_receipt(agent, failed_b)

    terminal_a = NativeCompactionPolicy(route=route_a).transition("unsupported")
    ledger = NativeCompactionLedger.empty().with_policy(terminal_a)
    committed_a = NativeCompactionTransitionReceipt(
        outcome="committed",
        policy=terminal_a,
        ledger=ledger,
        durable_revision=ledger.revision,
        attempts=1,
    )
    record_native_compaction_transition_receipt(agent, committed_a)

    assert agent._native_compaction_transition_receipt is committed_a
    assert has_unresolved_native_compaction_failure(agent, route_a) is False
    assert has_unresolved_native_compaction_failure(agent, route_b) is True


def test_v6_failed_receipt_registry_overflow_is_globally_fail_closed():
    agent = SimpleNamespace()
    for index in range(MAX_COMPACTION_LEDGER_ROUTES):
        route = _route(f"v6-capacity-{index}")
        receipt = failed_closed_transition_receipt(
            _observed_policy(route, f"v6-{index}"), error="capacity"
        )
        record_native_compaction_transition_receipt(agent, receipt)
    registry = agent._native_compaction_failed_receipts_by_route
    original_keys = set(registry)

    extra_route = _route("v6-capacity-extra")
    extra = failed_closed_transition_receipt(
        _observed_policy(extra_route, "v6-extra"), error="overflow"
    )
    record_native_compaction_transition_receipt(agent, extra)

    assert agent._native_compaction_transition_receipt is extra
    assert agent._native_compaction_failed_receipts_overflow is True
    assert len(registry) == MAX_COMPACTION_LEDGER_ROUTES
    assert set(registry) == original_keys
    assert has_unresolved_native_compaction_failure(
        agent, _route("v6-unrelated")
    ) is True


@pytest.mark.parametrize(
    "summary_kind",
    ["empty", "compaction-only"],
)
def test_v5_second_compaction_checkpoint_never_calls_summary_or_retry(
    monkeypatch, tmp_path, summary_kind,
):
    agent = _runtime_agent(monkeypatch, tmp_path, max_iterations=1)
    db = SessionDB(db_path=tmp_path / f"v5-{summary_kind}.db")
    session_id = db.create_session("v5-bounded", "test", model=agent.model)
    agent._session_db = db
    agent.session_id = session_id
    responses = [_compaction_only("checkpoint-a"), _compaction_only("checkpoint-b")]
    provider_calls = []
    monkeypatch.setattr(
        agent,
        "_interruptible_api_call",
        lambda kwargs: provider_calls.append(kwargs) or responses.pop(0),
    )
    summary_calls = []
    summary_response = (
        _text_response("")
        if summary_kind == "empty"
        else _compaction_only("forbidden-summary-checkpoint")
    )
    monkeypatch.setattr(
        agent,
        "_run_codex_stream",
        lambda kwargs, **_extra: summary_calls.append(kwargs) or summary_response,
    )
    handle_calls = []
    original_handle = agent._handle_max_iterations
    monkeypatch.setattr(
        agent,
        "_handle_max_iterations",
        lambda *args: handle_calls.append(args) or original_handle(*args),
    )

    result = agent.run_conversation("Use one continuation credit only")

    assert len(provider_calls) == 2
    assert handle_calls == []
    assert summary_calls == []
    assert result["final_response"] == (
        "Native compaction could not complete after the single checkpoint continuation."
    )
    assert result["completed"] is False
    assert result["partial"] is True
    assert result["turn_exit_reason"] == "native_compaction_continuation_exhausted"
    checkpoints = [
        message
        for message in result["messages"]
        if message.get("codex_output_items")
    ]
    assert [
        item["encrypted_content"]
        for item in checkpoints[-1]["codex_output_items"]
        if item["type"] == "compaction"
    ] == ["checkpoint-b"]
    persisted = db.get_messages_as_conversation(session_id)
    persisted_checkpoint = [
        message for message in persisted if message.get("codex_output_items")
    ][-1]
    assert persisted_checkpoint["codex_output_items"] == checkpoints[-1][
        "codex_output_items"
    ]
    ledger = NativeCompactionLedger.from_dict(
        db.get_codex_responses_compaction_state(session_id)
    )
    terminal_policy = ledger.policy_for(_route())
    assert terminal_policy.capability == "quarantined"
    assert terminal_policy.last_error == "compaction_continuation_limit"
    assert terminal_policy.last_compaction_digest == compaction_checkpoint_digest(
        checkpoints[-1]["codex_output_items"]
    )
    db.close()


_V5_RESERVED_NATIVE_OVERRIDE_VALUES = {
    "model": "override-model",
    "input": [],
    "instructions": "override instructions",
    "tools": [],
    "reasoning": {"effort": "low"},
    "include": [],
    "store": True,
    "previous_response_id": "resp_override",
    "context_management": [],
}


@pytest.mark.parametrize(
    ("reserved_key", "reserved_value"),
    _V5_RESERVED_NATIVE_OVERRIDE_VALUES.items(),
)
def test_v5_agent_builder_rejects_reserved_native_request_override_before_state(
    monkeypatch, tmp_path, reserved_key, reserved_value,
):
    agent = _runtime_agent(monkeypatch, tmp_path)
    db = _ReadFailureDB(
        NativeCompactionLedger.empty().to_dict(), failures=0
    )
    initial_policy = NativeCompactionPolicy(route=_route())
    initial_receipt = object()
    agent._session_db = db
    agent.session_id = "v5-reserved-agent-builder"
    agent._native_compaction_policy = initial_policy
    agent._native_compaction_transition_receipt = initial_receipt
    agent._native_compaction_request_active = False
    agent._native_compaction_replay_attempted = False
    agent.request_overrides = {reserved_key: reserved_value}
    provider_calls = []
    agent._interruptible_api_call = lambda kwargs: provider_calls.append(kwargs)

    with pytest.raises(ValueError, match="reserved native Responses field"):
        agent._build_api_kwargs([{"role": "user", "content": "reject override"}])

    assert provider_calls == []
    assert db.read_calls == 0
    assert db.cas_calls == 0
    assert agent._native_compaction_policy is initial_policy
    assert agent._native_compaction_transition_receipt is initial_receipt
    assert agent._native_compaction_request_active is False
    assert agent._native_compaction_replay_attempted is False


def test_v5_runtime_rejects_reserved_override_before_preflight_or_fallback(
    monkeypatch, tmp_path,
):
    agent = _runtime_agent(monkeypatch, tmp_path)
    db = _ReadFailureDB(
        NativeCompactionLedger.empty().to_dict(), failures=0
    )
    initial_policy = NativeCompactionPolicy(route=_route())
    initial_receipt = object()
    agent._session_db = db
    agent.session_id = "v5-reserved-runtime"
    agent._native_compaction_policy = initial_policy
    agent._native_compaction_transition_receipt = initial_receipt
    agent._native_compaction_request_active = False
    agent._native_compaction_replay_attempted = False
    agent.request_overrides = {"store": True}
    provider_calls = []
    monkeypatch.setattr(
        agent,
        "_interruptible_api_call",
        lambda kwargs: provider_calls.append(kwargs),
    )

    with pytest.raises(ValueError, match="reserved native Responses field"):
        agent.run_conversation("reject before preflight")

    assert provider_calls == []
    assert db.read_calls == 0
    assert db.cas_calls == 0
    assert agent._native_compaction_policy is initial_policy
    assert agent._native_compaction_transition_receipt is initial_receipt
    assert agent._native_compaction_request_active is False
    assert agent._native_compaction_replay_attempted is False


@pytest.mark.parametrize(
    ("reserved_key", "reserved_value"),
    _V5_RESERVED_NATIVE_OVERRIDE_VALUES.items(),
)
def test_v5_direct_transport_rejects_reserved_native_request_override(
    reserved_key, reserved_value,
):
    transport = ResponsesApiTransport()
    initial_state = dict(transport.__dict__)

    with pytest.raises(ValueError, match="reserved native Responses field"):
        transport.build_kwargs(
            model="gpt-5.6-sol",
            messages=[{"role": "user", "content": "reject override"}],
            tools=[],
            request_overrides={reserved_key: reserved_value},
            provider="openai-codex",
            base_url="https://chatgpt.com/backend-api/codex",
            is_codex_backend=True,
            enforce_native_request_custody=True,
        )

    assert transport.__dict__ == initial_state


@pytest.mark.parametrize("mode", ["hermes", "off"])
@pytest.mark.parametrize(
    ("reserved_key", "reserved_value"),
    [
        (key, value)
        for key, value in _V5_RESERVED_NATIVE_OVERRIDE_VALUES.items()
        if key not in {"context_management", "previous_response_id"}
    ],
)
def test_v5_non_native_agent_builder_preserves_existing_reserved_overrides(
    monkeypatch, tmp_path, mode, reserved_key, reserved_value,
):
    agent = _runtime_agent(monkeypatch, tmp_path)
    agent.codex_responses_auto_compaction = mode
    agent.request_overrides = {reserved_key: reserved_value}

    kwargs = agent._build_api_kwargs(
        [{"role": "user", "content": "ordinary Responses override"}]
    )

    assert kwargs[reserved_key] == reserved_value
    assert agent._native_compaction_request_active is False
    assert agent._native_compaction_replay_attempted is False


@pytest.mark.parametrize("mode", ["hermes", "off"])
def test_v5_non_native_agent_builder_preserves_context_management_stripping(
    monkeypatch, tmp_path, mode,
):
    agent = _runtime_agent(monkeypatch, tmp_path)
    agent.codex_responses_auto_compaction = mode
    agent.request_overrides = {"context_management": []}

    kwargs = agent._build_api_kwargs(
        [{"role": "user", "content": "ordinary Responses override"}]
    )

    assert "context_management" not in kwargs
    assert agent._native_compaction_request_active is False
    assert agent._native_compaction_replay_attempted is False


@pytest.mark.parametrize("mode", ["hermes", "off"])
def test_v11_previous_response_id_is_rejected_in_every_agent_mode(
    monkeypatch, tmp_path, mode,
):
    agent = _runtime_agent(monkeypatch, tmp_path)
    agent.codex_responses_auto_compaction = mode
    agent.request_overrides = {"previous_response_id": "resp_hidden_history"}

    with pytest.raises(ValueError, match="previous_response_id"):
        agent._build_api_kwargs(
            [{"role": "user", "content": "must remain transcript-bound"}]
        )


@pytest.mark.parametrize(
    ("reserved_key", "reserved_value"),
    [
        (key, value)
        for key, value in _V5_RESERVED_NATIVE_OVERRIDE_VALUES.items()
        if key != "previous_response_id"
    ],
)
def test_v5_direct_transport_preserves_reserved_overrides_without_native_custody(
    reserved_key, reserved_value,
):
    transport = ResponsesApiTransport()

    kwargs = transport.build_kwargs(
        model="gpt-5.6-sol",
        messages=[{"role": "user", "content": "ordinary transport override"}],
        tools=[],
        request_overrides={reserved_key: reserved_value},
        provider="xai",
        base_url="https://api.x.ai/v1",
        is_codex_backend=False,
        enforce_native_request_custody=False,
    )

    assert kwargs[reserved_key] == reserved_value


def test_v11_direct_transport_rejects_previous_response_id_without_native_mode():
    transport = ResponsesApiTransport()

    with pytest.raises(ValueError, match="previous_response_id"):
        transport.build_kwargs(
            model="gpt-5.6-sol",
            messages=[{"role": "user", "content": "no hidden continuation"}],
            tools=[],
            request_overrides={"previous_response_id": "resp_hidden_history"},
            provider="xai",
            base_url="https://api.x.ai/v1",
            is_codex_backend=False,
            enforce_native_request_custody=False,
        )


@pytest.mark.parametrize("mode", ["hermes", "off"])
def test_v5_non_native_runtime_preserves_existing_reserved_override(
    monkeypatch, tmp_path, mode,
):
    agent = _runtime_agent(monkeypatch, tmp_path)
    agent.codex_responses_auto_compaction = mode
    agent.request_overrides = {"instructions": "ordinary instructions"}
    provider_calls = []
    monkeypatch.setattr(
        agent,
        "_interruptible_api_call",
        lambda kwargs: provider_calls.append(kwargs) or _text_response("ordinary"),
    )

    result = agent.run_conversation("ordinary Responses mode")

    assert len(provider_calls) == 1
    assert provider_calls[0]["instructions"] == "ordinary instructions"
    assert provider_calls[0]["store"] is False
    assert result["final_response"] == "ordinary"
    assert result["completed"] is True


def test_v5_agent_builder_keeps_safe_overrides_and_native_request_invariants(
    monkeypatch, tmp_path,
):
    route = _route()
    owner = _observed_policy(route, "v5-safe-owner")
    db = _ReadFailureDB(
        NativeCompactionLedger.empty().with_policy(owner).to_dict(), failures=0
    )
    agent = _runtime_agent(monkeypatch, tmp_path)
    agent._session_db = db
    agent.session_id = "v5-safe-agent-builder"
    agent._native_compaction_policy = owner
    agent.reasoning_config = {"enabled": True, "effort": "xhigh"}
    agent.request_overrides = {
        "timeout": 17.5,
        "extra_body": {"provider_extension": {"enabled": True}},
    }
    checkpoint = {
        "role": "assistant",
        "content": "",
        "codex_output_items": _sidecar(route, "v5-safe-owner"),
    }

    kwargs = agent._build_api_kwargs(
        [checkpoint, {"role": "user", "content": "safe overrides"}]
    )

    assert kwargs["model"] == agent.model
    assert kwargs["store"] is False
    assert "previous_response_id" not in kwargs
    assert kwargs["reasoning"]["effort"] == "xhigh"
    assert kwargs["include"] == ["reasoning.encrypted_content"]
    assert kwargs["context_management"] == [
        {
            "type": "compaction",
            "compact_threshold": agent.codex_responses_compact_threshold,
        }
    ]
    assert kwargs["input"][0] == {
        "type": "compaction",
        "encrypted_content": "v5-safe-owner",
    }
    assert kwargs["input"][-1] == {"role": "user", "content": "safe overrides"}
    assert kwargs["timeout"] == 17.5
    assert kwargs["extra_body"] == {
        "provider_extension": {"enabled": True}
    }
    assert agent._native_compaction_request_active is True
    assert agent._native_compaction_replay_attempted is True
    assert db.cas_calls == 0


def test_v5_direct_transport_keeps_safe_request_overrides():
    transport = ResponsesApiTransport()

    kwargs = transport.build_kwargs(
        model="gpt-5.6-sol",
        messages=[{"role": "user", "content": "safe transport override"}],
        tools=[],
        reasoning_config={"enabled": True, "effort": "xhigh"},
        request_overrides={
            "timeout": 9,
            "extra_body": {"provider_extension": "safe"},
        },
        provider="openai-codex",
        base_url="https://chatgpt.com/backend-api/codex",
        is_codex_backend=True,
        enforce_native_request_custody=True,
    )

    assert kwargs["model"] == "gpt-5.6-sol"
    assert kwargs["store"] is False
    assert "previous_response_id" not in kwargs
    assert kwargs["reasoning"]["effort"] == "xhigh"
    assert kwargs["include"] == ["reasoning.encrypted_content"]
    assert kwargs["input"] == [
        {"role": "user", "content": "safe transport override"}
    ]
    assert kwargs["timeout"] == 9.0
    assert kwargs["extra_body"] == {"provider_extension": "safe"}


class _StructuredCompactionError(Exception):
    status_code = 400
    body = {
        "error": {
            "code": "unknown_parameter",
            "param": "context_management",
            "message": "unknown parameter",
        }
    }


class _InvalidEncryptedContentError(Exception):
    status_code = 400
    body = {
        "error": {
            "code": "invalid_encrypted_content",
            "message": "encrypted state rejected",
        }
    }


def test_unsupported_persistence_failure_does_not_retry_without_native_fields(
    monkeypatch, tmp_path,
):
    agent = _runtime_agent(monkeypatch, tmp_path)
    agent._session_db = _PersistenceExceptionDB()
    agent.session_id = "unsupported-persistence-failure"
    agent._ensure_db_session = lambda: None
    calls = []

    def _api_call(kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise _StructuredCompactionError("unsupported")
        return _text_response("must-not-be-called")

    monkeypatch.setattr(agent, "_interruptible_api_call", _api_call)

    result = agent.run_conversation("Do not downgrade without a receipt")

    assert len(calls) == 1
    assert "context_management" in calls[0]
    assert result["completed"] is False
    assert agent._native_compaction_policy.capability == "quarantined"


def test_replay_quarantine_persistence_failure_does_not_strip_or_retry(
    monkeypatch, tmp_path,
):
    route = _route()
    policy = _observed_policy(route, "rejected")
    ledger = NativeCompactionLedger.empty().with_policy(policy).to_dict()
    agent = _runtime_agent(monkeypatch, tmp_path)
    agent._session_db = _PersistenceExceptionDB(ledger)
    agent.session_id = "replay-persistence-failure"
    agent._ensure_db_session = lambda: None
    agent._native_compaction_policy = policy
    checkpoint = {
        "role": "assistant",
        "content": "checkpoint",
        "codex_reasoning_items": [
            {
                "type": "reasoning",
                "encrypted_content": "reasoning-evidence",
                "_issuer_kind": route.issuer_kind,
            }
        ],
        "codex_output_items": _sidecar(route, "rejected"),
    }
    calls = []

    def _api_call(kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise _InvalidEncryptedContentError("rejected")
        return _text_response("must-not-be-called")

    monkeypatch.setattr(agent, "_interruptible_api_call", _api_call)

    result = agent.run_conversation(
        "Do not strip without a receipt", conversation_history=[checkpoint]
    )

    assert len(calls) == 1
    assert result["completed"] is False
    assert checkpoint["codex_reasoning_items"][0]["encrypted_content"] == (
        "reasoning-evidence"
    )
    assert agent._codex_reasoning_replay_enabled is True
    assert agent._native_compaction_policy.capability == "quarantined"


def test_emergency_handoff_requires_durable_quarantine_receipt():
    route = _route()
    policy = _observed_policy(route, "owner")
    agent = SimpleNamespace(
        api_mode="codex_responses",
        compression_enabled=True,
        codex_responses_auto_compaction="native",
        provider="openai-codex",
        base_url=route.endpoint,
        model=route.model,
        _native_compaction_policy=policy,
        _session_db=_PersistenceExceptionDB(
            NativeCompactionLedger.empty().with_policy(policy).to_dict()
        ),
        session_id="emergency-persistence-failure",
        _native_compaction_request_active=True,
        _native_compaction_replay_attempted=True,
    )

    authorized = prepare_emergency_hermes_compaction(
        agent, reason="provider_context_overflow"
    )

    assert authorized is False
    assert agent._native_compaction_policy.capability == "quarantined"
    assert agent._native_compaction_request_active is True
    assert agent._native_compaction_replay_attempted is True


def test_no_session_checkpoint_fails_closed_before_automatic_handoff():
    route = _route()
    current = NativeCompactionPolicy(route=route)
    pending = _observed_policy(route, "stateless")
    message = {
        "role": "assistant",
        "content": "",
        "codex_output_items": _sidecar(route, "stateless"),
    }
    agent = SimpleNamespace(
        api_mode="codex_responses",
        compression_enabled=True,
        codex_responses_auto_compaction="native",
        provider="openai-codex",
        base_url=route.endpoint,
        model=route.model,
        _native_compaction_policy=current,
        _native_compaction_pending_policy=pending,
        _session_db=None,
        session_id=None,
    )

    stage_native_compaction_checkpoint(agent, message)

    assert agent._native_compaction_policy.capability not in {
        "item_observed",
        "replay_verified",
    }
    receipt = agent._native_compaction_transition_receipt
    assert receipt.failed_closed is True
    assert receipt.policy.route == route
    assert has_unresolved_native_compaction_failure(agent, route) is True
    assert should_defer_automatic_hermes_compaction(agent) is True


def test_last_ordinary_iteration_gets_one_protocol_continuation(
    monkeypatch, tmp_path
):
    agent = _runtime_agent(monkeypatch, tmp_path, max_iterations=1)
    db = SessionDB(db_path=tmp_path / "continuation.db")
    session_id = db.create_session("continuation", "test", model=agent.model)
    agent._session_db = db
    agent.session_id = session_id
    responses = [_compaction_only("checkpoint"), _text_response("visible answer")]
    calls = []
    monkeypatch.setattr(
        agent,
        "_interruptible_api_call",
        lambda kwargs: calls.append(kwargs) or responses.pop(0),
    )
    agent._handle_max_iterations = lambda *_args: "wrong summary path"

    result = agent.run_conversation("Continue after the final ordinary credit")

    assert len(calls) == 2
    assert result["final_response"] == "visible answer"
    assert result["completed"] is True
    assert agent.iteration_budget.used == 1
    assert calls[1]["input"][0]["type"] == "compaction"
    db.close()


def test_second_compaction_only_continuation_stops_with_newest_sidecar(
    monkeypatch, tmp_path
):
    agent = _runtime_agent(monkeypatch, tmp_path, max_iterations=1)
    db = SessionDB(db_path=tmp_path / "bounded.db")
    session_id = db.create_session("bounded", "test", model=agent.model)
    agent._session_db = db
    agent.session_id = session_id
    responses = [_compaction_only("first"), _compaction_only("second")]
    calls = []
    monkeypatch.setattr(
        agent,
        "_interruptible_api_call",
        lambda kwargs: calls.append(kwargs) or responses.pop(0),
    )
    summary_calls = []
    monkeypatch.setattr(
        agent,
        "_run_codex_stream",
        lambda kwargs, **_extra: (
            summary_calls.append(kwargs)
            or _text_response("Bounded summary from the newest checkpoint.")
        ),
    )

    result = agent.run_conversation("Stop after one protocol continuation")

    assert len(calls) == 2
    assert result["completed"] is False
    assert result["partial"] is True
    assert result["final_response"] == (
        "Native compaction could not complete after the single checkpoint continuation."
    )
    assert summary_calls == []
    checkpoints = [
        message
        for message in result["messages"]
        if message.get("codex_output_items")
    ]
    assert checkpoints[-1]["codex_output_items"][0]["encrypted_content"] == "second"
    persisted = db.get_messages_as_conversation(session_id)
    persisted_checkpoints = [
        message for message in persisted if message.get("codex_output_items")
    ]
    assert persisted_checkpoints[-1]["codex_output_items"][0][
        "encrypted_content"
    ] == "second"
    ledger = NativeCompactionLedger.from_dict(
        db.get_codex_responses_compaction_state(session_id)
    )
    assert ledger.policy_for(_route()).last_compaction_digest == (
        compaction_checkpoint_digest(checkpoints[-1]["codex_output_items"])
    )
    db.close()


def test_malformed_sqlite_sidecar_plus_positive_owner_fails_closed(tmp_path):
    db = SessionDB(db_path=tmp_path / "malformed-sidecar.db")
    session_id = db.create_session("malformed-sidecar", "test")
    route = _route()
    policy = _observed_policy(route, "durable")
    db.append_message(
        session_id,
        role="assistant",
        content="",
        codex_output_items=_sidecar(route, "durable"),
        codex_responses_compaction_policy=policy.to_dict(),
        expected_codex_responses_compaction_revision=0,
    )
    db._conn.execute(
        "UPDATE messages SET codex_output_items = ? WHERE session_id = ?",
        ("{not-json", session_id),
    )
    db._conn.commit()

    with pytest.raises(NativeCompactionReadError):
        load_policy_for_route(db, session_id, route)
    with pytest.raises((NativeCompactionStateError, ValueError)):
        db.get_messages_as_conversation(session_id)
    db.close()


@pytest.mark.parametrize("role", ["user", "tool"])
def test_lifecycle_rejects_owner_sidecar_outside_assistant_custody(role):
    route = _route()
    sidecar = _sidecar(route, f"{role}-owned")
    ledger = NativeCompactionLedger.empty().with_policy(
        _observed_policy(route, f"{role}-owned")
    )

    with pytest.raises(NativeCompactionStateError, match="assistant"):
        validate_compaction_lifecycle(
            ledger,
            [{"role": role, "content": "", "codex_output_items": sidecar}],
        )


@pytest.mark.parametrize(
    ("role", "expected"),
    [("user", False), ("tool", False), ("assistant", True)],
)
def test_replayable_sidecar_requires_explicit_assistant_custody(role, expected):
    route = _route()
    sidecar = _sidecar(route, "replay-custody")

    assert has_replayable_compaction_sidecar(
        [{"role": role, "content": "", "codex_output_items": sidecar}],
        route=route,
        expected_digest=compaction_checkpoint_digest(sidecar),
    ) is expected


@pytest.mark.parametrize("role", ["user", "tool"])
def test_append_message_rejects_sidecar_outside_assistant_custody_without_policy(
    tmp_path, role
):
    db = SessionDB(db_path=tmp_path / f"append-{role}.db")
    session_id = db.create_session(f"append-{role}", "test")

    with pytest.raises(ValueError, match="assistant"):
        db.append_message(
            session_id,
            role=role,
            content="",
            codex_output_items=_sidecar(_route(), f"append-{role}"),
        )

    assert db.get_messages_as_conversation(session_id) == []
    db.close()


@pytest.mark.parametrize("corrupt_role", ["user", "tool"])
def test_sqlite_role_corruption_is_not_reconstructed_as_assistant(
    tmp_path, corrupt_role
):
    db = SessionDB(db_path=tmp_path / f"corrupt-{corrupt_role}.db")
    session_id = db.create_session(f"corrupt-{corrupt_role}", "test")
    route = _route()
    policy = _observed_policy(route, "durable-role")
    db.append_message(
        session_id,
        role="assistant",
        content="",
        codex_output_items=_sidecar(route, "durable-role"),
        codex_responses_compaction_policy=policy.to_dict(),
        expected_codex_responses_compaction_revision=0,
    )
    valid_ledger = NativeCompactionLedger.from_dict(
        db.get_codex_responses_compaction_state(session_id)
    )
    assert valid_ledger.policy_for(route).capability == "item_observed"
    assert valid_ledger.policy_for(route).last_compaction_digest == (
        policy.last_compaction_digest
    )

    db._conn.execute(
        "UPDATE messages SET role = ? WHERE session_id = ?",
        (corrupt_role, session_id),
    )
    db._conn.commit()

    with pytest.raises(NativeCompactionStateError, match="assistant"):
        db.get_codex_responses_compaction_state(session_id)
    db.close()


@pytest.mark.parametrize("role", ["user", "tool"])
def test_import_rejects_owner_sidecar_outside_assistant_custody_atomically(
    tmp_path, role
):
    source = SessionDB(db_path=tmp_path / f"source-{role}.db")
    session_id = source.create_session(f"source-{role}", "test")
    blob = source.export_session(session_id)
    source.close()
    assert blob is not None
    route = _route()
    policy = _observed_policy(route, f"import-{role}")
    blob["codex_responses_compaction_state"] = (
        NativeCompactionLedger.empty().with_policy(policy).to_dict()
    )
    blob["messages"] = [
        {
            "role": role,
            "content": "",
            "codex_output_items": _sidecar(route, f"import-{role}"),
        }
    ]

    imported = SessionDB(db_path=tmp_path / f"imported-{role}.db")
    result = imported.import_sessions([blob])

    assert result["ok"] is False
    assert result["imported"] == 0
    assert imported.get_session(session_id) is None
    assert imported._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert imported._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert any("assistant" in error["error"] for error in result["errors"])
    imported.close()


def test_import_rejects_ledger_sidecar_digest_mismatch_atomically(tmp_path):
    source = SessionDB(db_path=tmp_path / "source.db")
    session_id = source.create_session("mismatched-import", "test")
    blob = source.export_session(session_id)
    source.close()
    assert blob is not None
    route = _route()
    policy = _observed_policy(route, "committed")
    blob["codex_responses_compaction_state"] = (
        NativeCompactionLedger.empty().with_policy(policy).to_dict()
    )
    blob["messages"] = [
        {
            "role": "assistant",
            "content": "",
            "codex_output_items": _sidecar(route, "different"),
        }
    ]

    imported = SessionDB(db_path=tmp_path / "imported.db")
    result = imported.import_sessions([blob])

    assert result["ok"] is False
    assert result["imported"] == 0
    assert imported.get_session(session_id) is None
    assert any(
        "checkpoint" in error["error"] or "digest" in error["error"]
        for error in result["errors"]
    )
    imported.close()


def test_compression_child_dropped_checkpoint_is_not_published_as_owner(tmp_path):
    db = SessionDB(db_path=tmp_path / "compression-child.db")
    parent_id = db.create_session("parent", "test")
    route = _route()
    policy = _observed_policy(route, "parent-owner")
    db.append_message(
        parent_id,
        role="assistant",
        content="",
        codex_output_items=_sidecar(route, "parent-owner"),
        codex_responses_compaction_policy=policy.to_dict(),
        expected_codex_responses_compaction_revision=0,
    )

    db.publish_compression_child(
        parent_session_id=parent_id,
        child_session_id="child",
        source="test",
        messages=[
            {
                "role": "user",
                "content": "[Summary of the conversation before compaction]",
            }
        ],
        require_compression_lease=False,
    )

    child_policy = load_policy_for_route(db, "child", route)
    assert child_policy.capability == "quarantined"
    assert child_policy.last_error == "checkpoint_missing_after_compression"
    db.close()


def test_fork_copies_checkpoint_and_complete_terminal_route_history(tmp_path):
    db_path = tmp_path / "fork-lifecycle.db"
    db = SessionDB(db_path=db_path)
    parent_id = db.create_session("fork-parent", "test")
    owner_route = _route("owner")
    owner_policy = _observed_policy(owner_route, "fork-owner")
    db.append_message(
        parent_id,
        role="assistant",
        content="",
        codex_output_items=_sidecar(owner_route, "fork-owner"),
        codex_responses_compaction_policy=owner_policy.to_dict(),
        expected_codex_responses_compaction_revision=0,
    )
    unsupported = NativeCompactionPolicy(route=_route("unsupported")).transition(
        "unsupported", error="provider_rejected_context_management"
    )
    unsupported_receipt = persist_policy_compare_and_set(
        db, parent_id, unsupported
    )
    assert unsupported_receipt.committed is True
    quarantined = NativeCompactionPolicy(route=_route("quarantined")).transition(
        "quarantined", error="operator_quarantine"
    )
    quarantine_receipt = persist_policy_compare_and_set(
        db, parent_id, quarantined
    )
    assert quarantine_receipt.committed is True
    parent_ledger = NativeCompactionLedger.from_dict(
        db.get_codex_responses_compaction_state(parent_id)
    )

    child_id = "fork-child"
    db.create_session_fork(
        parent_session_id=parent_id,
        child_session_id=child_id,
        source="test",
        model=None,
        model_config=None,
        messages=db.get_messages_as_conversation(parent_id),
        quarantine_error="checkpoint_missing_in_fork",
    )

    assert NativeCompactionLedger.from_dict(
        db.get_codex_responses_compaction_state(child_id)
    ) == parent_ledger
    db.close()

    reopened = SessionDB(db_path=db_path)
    resumed = NativeCompactionLedger.from_dict(
        reopened.get_codex_responses_compaction_state(child_id)
    )
    assert resumed == parent_ledger
    assert resumed.policy_for(owner_route).capability == "item_observed"
    assert resumed.policy_for(unsupported.route).capability == "unsupported"
    assert resumed.policy_for(quarantined.route).capability == "quarantined"
    reopened.close()


def test_v11_generic_fork_failure_rolls_back_child_and_parent_end(
    tmp_path, monkeypatch,
):
    db = SessionDB(db_path=tmp_path / "generic-fork-rollback.db")
    parent_id = db.create_session("parent", "test")
    db.replace_messages(parent_id, [{"role": "user", "content": "stable"}])
    monkeypatch.setattr(
        db,
        "_clone_codex_responses_compaction_lifecycle_on_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("lifecycle failed")
        ),
    )

    with pytest.raises(RuntimeError, match="lifecycle failed"):
        db.create_session_fork(
            parent_session_id=parent_id,
            child_session_id="rejected-child",
            source="test",
            model=None,
            model_config={"_branched_from": parent_id},
            messages=[{"role": "user", "content": "stable"}],
            end_parent_reason="branched",
            quarantine_error="checkpoint_missing_in_fork",
        )

    assert db.get_session("rejected-child") is None
    assert db.get_messages("rejected-child") == []
    parent = db.get_session(parent_id)
    assert parent is not None
    assert parent["ended_at"] is None
    assert parent["end_reason"] is None
    db.close()


def test_v11_missing_child_lifecycle_with_sidecars_fails_closed(tmp_path):
    db = SessionDB(db_path=tmp_path / "missing-child-lifecycle.db")
    route = _route("missing-child-state")
    parent_id = db.create_session("parent", "test")
    policy = _observed_policy(route, "owned")
    db.append_message(
        parent_id,
        role="assistant",
        content="",
        codex_output_items=_sidecar(route, "owned"),
        codex_responses_compaction_policy=policy.to_dict(),
        expected_codex_responses_compaction_revision=0,
    )
    child_id = db.create_session(
        "partial-child",
        "test",
        parent_session_id=parent_id,
        inherit_compaction_state=False,
    )
    copied = db.get_messages_as_conversation(parent_id)

    def _insert_partial(conn):
        db._insert_message_rows(conn, child_id, copied)
        conn.execute(
            "UPDATE sessions SET codex_responses_compaction_state = NULL "
            "WHERE id = ?",
            (child_id,),
        )

    db._execute_write(_insert_partial)

    with pytest.raises(ValueError, match="Missing codex_responses_compaction_state"):
        db.get_codex_responses_compaction_state(child_id)
    db.close()


@pytest.mark.parametrize("lifecycle_kind", ["empty", "nonowning"])
def test_v12_serialized_nonowning_lifecycle_with_sidecars_fails_closed(
    tmp_path, lifecycle_kind,
):
    """Serialized-but-empty/incomplete custody is not safer than missing custody."""
    db = SessionDB(db_path=tmp_path / f"serialized-{lifecycle_kind}.db")
    route = _route(f"serialized-{lifecycle_kind}")
    parent_id = db.create_session("parent", "test")
    policy = _observed_policy(route, "owned")
    db.append_message(
        parent_id,
        role="assistant",
        content="",
        codex_output_items=_sidecar(route, "owned"),
        codex_responses_compaction_policy=policy.to_dict(),
        expected_codex_responses_compaction_revision=0,
    )
    corrupt_ledger = NativeCompactionLedger.empty()
    if lifecycle_kind == "nonowning":
        corrupt_ledger = corrupt_ledger.with_policy(NativeCompactionPolicy(route=route))

    def _serialize_corrupt_state(conn):
        conn.execute(
            "UPDATE sessions SET codex_responses_compaction_state = ? WHERE id = ?",
            (
                json.dumps(
                    corrupt_ledger.to_dict(),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                parent_id,
            ),
        )

    db._execute_write(_serialize_corrupt_state)

    with pytest.raises(
        NativeCompactionStateError,
        match="checkpoint is missing owning route custody",
    ):
        db.get_codex_responses_compaction_state(parent_id)

    child_id = f"rejected-{lifecycle_kind}-child"
    with pytest.raises(
        NativeCompactionStateError,
        match="checkpoint is missing owning route custody",
    ):
        db.create_session_fork(
            parent_session_id=parent_id,
            child_session_id=child_id,
            source="test",
            model=None,
            model_config={"_branched_from": parent_id},
            messages=db.get_messages_as_conversation(parent_id),
            end_parent_reason="branched",
            quarantine_error="checkpoint_missing_in_fork",
        )

    assert db.get_session(child_id) is None
    assert db.get_messages(child_id) == []
    parent = db.get_session(parent_id)
    assert parent is not None
    assert parent["ended_at"] is None
    assert parent["end_reason"] is None
    db.close()


def test_schemeless_endpoint_never_leaks_credentials_or_url_suffixes(
    tmp_path, caplog
):
    caplog.set_level(logging.DEBUG)
    raw_endpoint = "user:secret@example/v1?token=x#fragment"
    route = route_for_request(
        provider="custom",
        endpoint=raw_endpoint,
        model="gpt-5.6-sol",
    )
    route_blob = json.dumps(route.to_dict(), sort_keys=True)

    assert "example" in route.endpoint
    assert route.endpoint.endswith("/v1")
    assert all(
        forbidden not in route_blob
        for forbidden in ("user", "secret", "token", "?", "#", "fragment")
    )
    assert len(compaction_route_key(route)) == 64

    db = SessionDB(db_path=tmp_path / "endpoint.db")
    session_id = db.create_session("endpoint", "test")
    policy = _observed_policy(route, "opaque")
    sidecar = _sidecar(route, "opaque")
    db.append_message(
        session_id,
        role="assistant",
        content="",
        codex_output_items=sidecar,
        codex_responses_compaction_policy=policy.to_dict(),
        expected_codex_responses_compaction_revision=0,
    )
    exported = db.export_session(session_id)
    durable_blob = json.dumps(
        {
            "route": route.to_dict(),
            "route_key": compaction_route_key(route),
            "sidecar": sidecar,
            "export": exported,
            "logs": caplog.text,
        },
        sort_keys=True,
    )
    assert raw_endpoint not in durable_blob
    assert all(
        forbidden not in durable_blob
        for forbidden in ("secret", "token=x", "?token", "#fragment")
    )
    db.close()


def test_full_terminal_ledger_never_evicts_terminal_history():
    policies = [
        NativeCompactionPolicy(route=_route(f"terminal-{index}")).transition(
            "unsupported", error="terminal"
        )
        for index in range(MAX_COMPACTION_LEDGER_ROUTES)
    ]
    ledger = NativeCompactionLedger.empty()
    for policy in policies:
        ledger = ledger.with_policy(policy)
    terminal_a_key = compaction_route_key(policies[0].route)

    with pytest.raises(
        NativeCompactionStateError, match="terminal|capacity|full"
    ):
        ledger.with_policy(
            NativeCompactionPolicy(route=_route("new-route")).transition(
                "unsupported", error="new-terminal"
            )
        )

    assert terminal_a_key in ledger.routes
    assert ledger.routes[terminal_a_key].capability == "unsupported"


def test_v9_full_owning_ledger_never_evicts_route_custody():
    policies = [
        _observed_policy(_route(f"owning-{index}"), f"owning-{index}")
        for index in range(MAX_COMPACTION_LEDGER_ROUTES)
    ]
    ledger = NativeCompactionLedger.empty()
    for policy in policies:
        ledger = ledger.with_policy(policy)
    original_routes = dict(ledger.routes)

    with pytest.raises(
        NativeCompactionStateError, match="custody|capacity|full"
    ):
        ledger.with_policy(NativeCompactionPolicy(route=_route("route-33")))

    assert ledger.routes == original_routes
    assert all(
        policy.capability == "item_observed"
        for policy in ledger.routes.values()
    )


def test_v8_compression_sink_defers_automatic_hermes_for_native_owner(
    monkeypatch, tmp_path,
):
    """Every non-forced sink must enforce ownership, even if a caller forgets."""
    from agent import conversation_compression

    agent = _runtime_agent(monkeypatch, tmp_path)
    owner = _observed_policy(_route(), "v8-sink-owner")
    agent._native_compaction_policy = owner
    agent.__dict__["_session_db"] = None
    messages = [{"role": "user", "content": "preserve me"}]

    def _forbidden_compression(*_args, **_kwargs):
        raise AssertionError("automatic Hermes compression bypassed native ownership")

    monkeypatch.setattr(
        conversation_compression,
        "compress_context",
        _forbidden_compression,
    )

    compressed, prompt = agent._compress_context(messages, "stable system")

    assert compressed is messages
    assert prompt == "stable system"


def test_v8_compression_sink_rejects_bare_force_ownership_bypass(
    monkeypatch, tmp_path,
):
    """Force bypasses summary cooldown only; it is not a custody capability."""
    from agent import conversation_compression

    agent = _runtime_agent(monkeypatch, tmp_path)
    agent._native_compaction_policy = _observed_policy(_route(), "v8-force-owner")
    agent.__dict__["_session_db"] = None
    messages = [{"role": "user", "content": "manual request"}]
    def _forbidden_compression(*_args, **_kwargs):
        raise AssertionError("bare force=True bypassed native ownership")

    monkeypatch.setattr(
        conversation_compression,
        "compress_context",
        _forbidden_compression,
    )

    compressed, prompt = agent._compress_context(
        messages,
        "stable system",
        force=True,
    )

    assert compressed is messages
    assert prompt == "stable system"


def test_v8_manual_authorization_is_route_bound_and_one_shot(
    monkeypatch, tmp_path,
):
    """Explicit manual compression needs proof; reuse and route drift fail closed."""
    from agent import conversation_compression
    from agent import responses_compaction

    prepare_manual = getattr(
        responses_compaction,
        "prepare_manual_hermes_compaction",
        None,
    )
    assert callable(prepare_manual), "manual custody authorization API is missing"

    route = _route()
    owner = _observed_policy(route, "v8-manual-owner")
    db = SessionDB(db_path=tmp_path / "v8-manual-owner.db")
    session_id = db.create_session("v8-manual", "test", model=route.model)
    db.append_message(
        session_id,
        role="assistant",
        content="",
        codex_output_items=_sidecar(route, "v8-manual-owner"),
        codex_responses_compaction_policy=owner.to_dict(),
        expected_codex_responses_compaction_revision=0,
    )

    agent = _runtime_agent(monkeypatch, tmp_path)
    agent._session_db = db
    agent.__dict__["session_id"] = session_id
    agent._native_compaction_policy = NativeCompactionPolicy(route=route)
    agent.__dict__["codex_responses_auto_compaction"] = "hermes"
    messages = [{"role": "user", "content": "manual request"}]
    calls = []

    def _manual_compression(*args, **kwargs):
        calls.append((args, kwargs))
        return ([{"role": "assistant", "content": "manual summary"}], "rebuilt")

    monkeypatch.setattr(
        conversation_compression,
        "compress_context",
        _manual_compression,
    )

    authorization = prepare_manual(agent, reason="explicit_manual_request")
    assert authorization is not False
    compressed, prompt = agent._compress_context(
        messages,
        "stable system",
        force=True,
        hermes_compaction_authorization=authorization,
    )
    assert compressed == [{"role": "assistant", "content": "manual summary"}]
    assert prompt == "rebuilt"
    assert len(calls) == 1

    # The same proof cannot authorize a second rewrite.
    compressed, prompt = agent._compress_context(
        messages,
        "stable system",
        force=True,
        hermes_compaction_authorization=authorization,
    )
    assert compressed is messages
    assert prompt == "stable system"
    assert len(calls) == 1

    # A fresh proof for route A cannot be replayed after switching to route B.
    route_a_authorization = prepare_manual(agent, reason="explicit_manual_request")
    assert route_a_authorization is not False
    agent.__dict__["model"] = "gpt-5.6-sol-route-b"
    compressed, prompt = agent._compress_context(
        messages,
        "stable system",
        force=True,
        hermes_compaction_authorization=route_a_authorization,
    )
    assert compressed is messages
    assert prompt == "stable system"
    assert len(calls) == 1
    db.close()


@pytest.mark.parametrize(
    ("capability", "expected"),
    [
        ("unknown", "unsupported"),
        ("shape_accepted", "unsupported"),
        ("item_observed", "quarantined"),
        ("replay_verified", "quarantined"),
        ("quarantined", "quarantined"),
    ],
)
def test_v10_structured_rejection_never_releases_established_ownership(
    capability, expected,
):
    """A later provider rejection cannot relabel an owning route unsupported."""
    from agent.responses_compaction import (
        policy_after_structured_compaction_rejection,
    )

    policy = replace(
        NativeCompactionPolicy(route=_route()),
        capability=capability,
        last_compaction_digest=(
            "v10-owned-checkpoint"
            if capability in {"item_observed", "replay_verified"}
            else None
        ),
    )

    transitioned = policy_after_structured_compaction_rejection(policy)

    assert transitioned.capability == expected
    if capability in {"item_observed", "replay_verified", "quarantined"}:
        assert transitioned.capability != "unsupported"


def test_v9_off_mode_blocks_manual_authorization_and_registered_legacy_proof(
    monkeypatch, tmp_path,
):
    """Off is an absolute textual-compression ban, including explicit manual calls."""
    from agent import conversation_compression
    from agent import responses_compaction

    route = _route()
    owner = _observed_policy(route, "v9-off-manual-owner")
    db = SessionDB(db_path=tmp_path / "v9-off-manual-owner.db")
    session_id = db.create_session("v9-off-manual", "test", model=route.model)
    db.append_message(
        session_id,
        role="assistant",
        content="",
        codex_output_items=_sidecar(route, "v9-off-manual-owner"),
        codex_responses_compaction_policy=owner.to_dict(),
        expected_codex_responses_compaction_revision=0,
    )
    durable_owner = load_policy_for_route(db, session_id, route)

    agent = _runtime_agent(monkeypatch, tmp_path)
    agent._session_db = db
    agent.__dict__["session_id"] = session_id
    agent.__dict__["codex_responses_auto_compaction"] = "off"
    agent._native_compaction_policy = NativeCompactionPolicy(route=route)
    messages = [{"role": "user", "content": "must remain verbatim"}]

    def _forbidden_compression(*_args, **_kwargs):
        raise AssertionError("off-mode manual proof reached textual compression")

    monkeypatch.setattr(
        conversation_compression,
        "compress_context",
        _forbidden_compression,
    )
    assert (
        responses_compaction.prepare_manual_hermes_compaction(
            agent, reason="explicit_manual_request"
        )
        is False
    )

    legacy_proof = responses_compaction.HermesCompactionAuthorization(
        route=route,
        session_id=session_id,
        policy_revision=durable_owner.revision,
        mode="off",
        reason="legacy_manual_proof",
    )
    agent.__dict__["_manual_hermes_compaction_authorization"] = legacy_proof
    compressed, prompt = agent._compress_context(
        messages,
        "stable system",
        force=True,
        hermes_compaction_authorization=legacy_proof,
    )

    assert compressed is messages
    assert prompt == "stable system"
    assert agent.__dict__["_manual_hermes_compaction_authorization"] is legacy_proof
    db.close()


def test_v10_off_mode_does_not_consume_existing_emergency_proof(
    monkeypatch, tmp_path,
):
    """Off rejects an emergency rewrite without spending its capability."""
    from agent import conversation_compression
    from agent import responses_compaction

    agent = _runtime_agent(monkeypatch, tmp_path)
    route = _route()
    agent.__dict__["session_id"] = "v10-off-emergency"
    agent.__dict__["codex_responses_auto_compaction"] = "off"
    proof = responses_compaction.EmergencyHermesCompactionAuthorization(
        route=route,
        session_id="v10-off-emergency",
        policy_revision=0,
        mode="native",
        reason="provider_overflow",
    )
    agent.__dict__["_emergency_hermes_compaction_authorization"] = proof
    messages = [{"role": "user", "content": "must remain verbatim"}]
    monkeypatch.setattr(
        conversation_compression,
        "compress_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("off-mode emergency proof reached textual compression")
        ),
    )

    compressed, prompt = agent._compress_context(
        messages,
        "stable system",
        force=True,
        emergency_hermes_compaction_authorization=proof,
    )

    assert compressed is messages
    assert prompt == "stable system"
    assert agent.__dict__["_emergency_hermes_compaction_authorization"] is proof


def test_v11_off_preparation_does_not_revoke_existing_proofs(monkeypatch, tmp_path):
    from agent.responses_compaction import (
        EmergencyHermesCompactionAuthorization,
        HermesCompactionAuthorization,
        prepare_manual_hermes_compaction,
    )

    agent = _runtime_agent(monkeypatch, tmp_path)
    agent.codex_responses_auto_compaction = "off"
    route = route_for_request(
        provider=agent.provider,
        endpoint=agent.base_url,
        model=agent.model,
    )
    manual = HermesCompactionAuthorization(
        route=route,
        session_id=agent.session_id,
        policy_revision=0,
        mode="native",
        reason="existing_manual",
    )
    emergency = EmergencyHermesCompactionAuthorization(
        route=route,
        session_id=agent.session_id,
        policy_revision=0,
        mode="native",
        reason="existing_emergency",
    )
    agent.__dict__["_manual_hermes_compaction_authorization"] = manual
    agent.__dict__["_emergency_hermes_compaction_authorization"] = emergency

    assert prepare_manual_hermes_compaction(agent) is False
    assert agent.__dict__["_manual_hermes_compaction_authorization"] is manual
    assert prepare_emergency_hermes_compaction(agent, reason="overflow") is False
    assert agent.__dict__["_emergency_hermes_compaction_authorization"] is emergency


def test_v8_acknowledged_emergency_handoff_remains_available(
    monkeypatch, tmp_path,
):
    """A durable native-owner quarantine permits exactly one overflow recovery."""
    from agent import conversation_compression

    route = _route()
    owner = _observed_policy(route, "v8-emergency-owner")
    db = SessionDB(db_path=tmp_path / "v8-emergency-owner.db")
    session_id = db.create_session("v8-emergency", "test", model=route.model)
    db.append_message(
        session_id,
        role="assistant",
        content="",
        codex_output_items=_sidecar(route, "v8-emergency-owner"),
        codex_responses_compaction_policy=owner.to_dict(),
        expected_codex_responses_compaction_revision=0,
    )
    durable_owner = load_policy_for_route(db, session_id, route)
    agent = _runtime_agent(monkeypatch, tmp_path)
    agent._session_db = db
    agent.__dict__["session_id"] = session_id
    agent._native_compaction_policy = NativeCompactionPolicy(route=route)
    messages = [{"role": "user", "content": "provider overflow"}]
    calls = []

    def _emergency_compression(*args, **kwargs):
        calls.append((args, kwargs))
        return ([{"role": "assistant", "content": "recovered"}], "rebuilt")

    monkeypatch.setattr(
        conversation_compression,
        "compress_context",
        _emergency_compression,
    )

    emergency_authorization = prepare_emergency_hermes_compaction(
        agent, reason="provider_context_overflow"
    )
    assert emergency_authorization is not False
    handed_off = load_policy_for_route(db, session_id, route)
    assert handed_off.capability == "quarantined"
    assert handed_off.revision == durable_owner.revision + 1
    compressed, prompt = agent._compress_context(
        messages,
        "stable system",
        emergency_hermes_compaction_authorization=emergency_authorization,
    )
    assert compressed == [{"role": "assistant", "content": "recovered"}]
    assert prompt == "rebuilt"
    assert len(calls) == 1

    compressed, prompt = agent._compress_context(
        messages,
        "stable system",
        emergency_hermes_compaction_authorization=emergency_authorization,
    )
    assert compressed is messages
    assert prompt == "stable system"
    assert len(calls) == 1
    db.close()


@pytest.mark.parametrize("authorization_kind", ["manual", "emergency"])
@pytest.mark.parametrize("first_outcome", ["abandoned", "noop", "success"])
def test_v8_native_quarantine_never_becomes_generic_hermes_capability(
    monkeypatch,
    tmp_path,
    authorization_kind,
    first_outcome,
):
    """Durable handoff remains proof-gated after abandon, no-op, or success."""
    from agent import conversation_compression
    from agent import responses_compaction

    route = _route()
    marker = f"v8-proof-gated-{authorization_kind}-{first_outcome}"
    owner = _observed_policy(route, marker)
    db = SessionDB(db_path=tmp_path / f"{marker}.db")
    session_id = db.create_session(marker, "test", model=route.model)
    db.append_message(
        session_id,
        role="assistant",
        content="",
        codex_output_items=_sidecar(route, marker),
        codex_responses_compaction_policy=owner.to_dict(),
        expected_codex_responses_compaction_revision=0,
    )
    agent = _runtime_agent(monkeypatch, tmp_path)
    agent._session_db = db
    agent.__dict__["session_id"] = session_id
    agent._native_compaction_policy = NativeCompactionPolicy(route=route)
    agent.__dict__["codex_responses_auto_compaction"] = "native"
    messages = [{"role": "user", "content": marker}]
    calls = []

    def _compression(*args, **kwargs):
        calls.append((args, kwargs))
        if first_outcome == "success":
            return ([{"role": "assistant", "content": "summary"}], "rebuilt")
        return args[1], args[2]

    monkeypatch.setattr(conversation_compression, "compress_context", _compression)
    if authorization_kind == "manual":
        prepare = responses_compaction.prepare_manual_hermes_compaction
        keyword = "hermes_compaction_authorization"
        reason = "explicit_manual_request"
    else:
        prepare = responses_compaction.prepare_emergency_hermes_compaction
        keyword = "emergency_hermes_compaction_authorization"
        reason = "provider_context_overflow"

    authorization = prepare(agent, reason=reason)
    assert authorization is not False
    assert load_policy_for_route(db, session_id, route).capability == "quarantined"

    if first_outcome != "abandoned":
        agent._compress_context(
            messages,
            "stable system",
            force=True,
            **{keyword: authorization},
        )
    expected_calls = 0 if first_outcome == "abandoned" else 1
    assert len(calls) == expected_calls

    automatic, prompt = agent._compress_context(messages, "stable system")
    assert automatic is messages
    assert prompt == "stable system"
    forced, prompt = agent._compress_context(
        messages,
        "stable system",
        force=True,
    )
    assert forced is messages
    assert prompt == "stable system"
    assert len(calls) == expected_calls

    fresh_authorization = prepare(agent, reason=reason)
    assert fresh_authorization is not False
    agent._compress_context(
        messages,
        "stable system",
        force=True,
        **{keyword: fresh_authorization},
    )
    assert len(calls) == expected_calls + 1
    reused, prompt = agent._compress_context(
        messages,
        "stable system",
        force=True,
        **{keyword: fresh_authorization},
    )
    assert reused is messages
    assert prompt == "stable system"
    assert len(calls) == expected_calls + 1
    db.close()


@pytest.mark.parametrize("mismatch", ["route", "mode"])
def test_v8_emergency_authorization_rejects_route_or_mode_switch(
    monkeypatch, tmp_path, mismatch,
):
    """Emergency proof remains bound to its exact route and compaction mode."""
    from agent import conversation_compression

    route = _route()
    owner = _observed_policy(route, f"v8-emergency-mismatch-{mismatch}")
    db = SessionDB(db_path=tmp_path / f"v8-emergency-mismatch-{mismatch}.db")
    session_id = db.create_session(
        f"v8-emergency-mismatch-{mismatch}", "test", model=route.model
    )
    db.append_message(
        session_id,
        role="assistant",
        content="",
        codex_output_items=_sidecar(route, f"v8-emergency-mismatch-{mismatch}"),
        codex_responses_compaction_policy=owner.to_dict(),
        expected_codex_responses_compaction_revision=0,
    )
    agent = _runtime_agent(monkeypatch, tmp_path)
    agent._session_db = db
    agent.__dict__["session_id"] = session_id
    agent._native_compaction_policy = NativeCompactionPolicy(route=route)
    authorization = prepare_emergency_hermes_compaction(
        agent, reason="provider_context_overflow"
    )
    assert authorization is not False
    if mismatch == "route":
        agent.__dict__["model"] = "gpt-5.6-sol-emergency-route-b"
    else:
        agent.__dict__["codex_responses_auto_compaction"] = "hermes"
    messages = [{"role": "user", "content": "preserve mismatched proof"}]

    def _forbidden_compression(*_args, **_kwargs):
        raise AssertionError("mismatched emergency proof reached compression")

    monkeypatch.setattr(
        conversation_compression,
        "compress_context",
        _forbidden_compression,
    )
    compressed, prompt = agent._compress_context(
        messages,
        "stable system",
        force=True,
        emergency_hermes_compaction_authorization=authorization,
    )
    assert compressed is messages
    assert prompt == "stable system"
    db.close()


@pytest.mark.parametrize("mode", ["native", "off"])
@pytest.mark.parametrize("force", [False, True])
def test_v8_sink_defers_durable_owner_after_successful_read(
    monkeypatch, tmp_path, mode, force,
):
    """Durable current-route ownership blocks automatic and bare-force rewrites."""
    from agent import conversation_compression

    route = _route()
    owner = _observed_policy(route, f"v8-durable-owner-{mode}-{force}")
    db = SessionDB(db_path=tmp_path / f"v8-durable-owner-{mode}-{force}.db")
    session_id = db.create_session(
        f"v8-durable-owner-{mode}-{force}", "test", model=route.model
    )
    db.append_message(
        session_id,
        role="assistant",
        content="",
        codex_output_items=_sidecar(route, f"v8-durable-owner-{mode}-{force}"),
        codex_responses_compaction_policy=owner.to_dict(),
        expected_codex_responses_compaction_revision=0,
    )
    durable_owner = load_policy_for_route(db, session_id, route)
    agent = _runtime_agent(monkeypatch, tmp_path)
    agent._session_db = db
    agent.__dict__["session_id"] = session_id
    agent.__dict__["codex_responses_auto_compaction"] = mode
    agent._native_compaction_policy = NativeCompactionPolicy(route=route)
    messages = [{"role": "user", "content": "preserve durable ownership"}]

    def _forbidden_compression(*_args, **_kwargs):
        raise AssertionError("durable native ownership failed open at the sink")

    monkeypatch.setattr(
        conversation_compression,
        "compress_context",
        _forbidden_compression,
    )

    compressed, prompt = agent._compress_context(
        messages,
        "stable system",
        force=force,
    )
    assert compressed is messages
    assert prompt == "stable system"
    if mode == "native":
        assert agent._native_compaction_read_status.succeeded is True
        assert agent._native_compaction_policy == durable_owner
    else:
        assert agent._native_compaction_read_status.outcome == "not_attempted"
        assert load_policy_for_route(db, session_id, route) == durable_owner
    db.close()


@pytest.mark.parametrize("capability", ["item_observed", "quarantined"])
@pytest.mark.parametrize("force", [False, True], ids=["automatic", "bare-force"])
def test_v9_hermes_mode_automatic_sink_preserves_residual_native_owner(
    monkeypatch, tmp_path, force, capability,
):
    """Mode switches do not hand off custody without manual/emergency proof."""
    from agent import conversation_compression

    route = _route()
    owner = _observed_policy(route, f"v9-auto-mode-switch-{capability}-{force}")
    if capability == "quarantined":
        owner = owner.transition("quarantined", error="preserve quarantine")
    db = SessionDB(
        db_path=tmp_path / f"v9-auto-mode-switch-{capability}-{force}.db"
    )
    session_id = db.create_session(
        f"v9-auto-mode-switch-{capability}-{force}", "test", model=route.model
    )
    db.append_message(
        session_id,
        role="assistant",
        content="",
        codex_output_items=_sidecar(
            route, f"v9-auto-mode-switch-{capability}-{force}"
        ),
        codex_responses_compaction_policy=owner.to_dict(),
        expected_codex_responses_compaction_revision=0,
    )
    durable_owner = load_policy_for_route(db, session_id, route)
    agent = _runtime_agent(monkeypatch, tmp_path)
    agent._session_db = db
    agent.__dict__["session_id"] = session_id
    agent.__dict__["codex_responses_auto_compaction"] = "hermes"
    agent._native_compaction_policy = NativeCompactionPolicy(route=route)

    def _forbidden_compression(*_args, **_kwargs):
        raise AssertionError("mode switch bypassed residual native ownership")

    monkeypatch.setattr(
        conversation_compression,
        "compress_context",
        _forbidden_compression,
    )
    messages = [{"role": "user", "content": "preserve native owner"}]

    compressed, prompt = agent._compress_context(
        messages,
        "stable system",
        force=force,
    )

    assert compressed is messages
    assert prompt == "stable system"
    assert load_policy_for_route(db, session_id, route) == durable_owner
    assert agent._native_compaction_policy == durable_owner
    db.close()


def test_v9_hermes_mode_automatic_sink_does_not_attempt_handoff_persistence(
    monkeypatch, tmp_path,
):
    """A failed durable quarantine cannot authorize automatic textual rewrite."""
    from agent import conversation_compression

    route = _route()
    owner = _observed_policy(route, "v8-auto-handoff-write-failure")
    durable_db = SessionDB(db_path=tmp_path / "v8-auto-handoff-write-failure.db")
    session_id = durable_db.create_session(
        "v8-auto-handoff-write-failure", "test", model=route.model
    )
    durable_db.append_message(
        session_id,
        role="assistant",
        content="",
        codex_output_items=_sidecar(route, "v8-auto-handoff-write-failure"),
        codex_responses_compaction_policy=owner.to_dict(),
        expected_codex_responses_compaction_revision=0,
    )
    durable_state = durable_db.get_codex_responses_compaction_state(session_id)
    durable_db.close()
    failing_db = _PersistenceExceptionDB(durable_state)
    agent = _runtime_agent(monkeypatch, tmp_path)
    agent._session_db = failing_db
    agent.__dict__["session_id"] = session_id
    agent.__dict__["codex_responses_auto_compaction"] = "hermes"
    agent._native_compaction_policy = NativeCompactionPolicy(route=route)
    messages = [{"role": "user", "content": "preserve on failed handoff"}]

    def _forbidden_compression(*_args, **_kwargs):
        raise AssertionError("failed handoff entered textual compression")

    monkeypatch.setattr(
        conversation_compression,
        "compress_context",
        _forbidden_compression,
    )

    compressed, prompt = agent._compress_context(
        messages,
        "stable system",
        force=True,
    )

    assert compressed is messages
    assert prompt == "stable system"
    assert failing_db.cas_calls == 0


def test_v8_emergency_hands_off_residual_owner_after_switch_to_hermes(
    monkeypatch, tmp_path,
):
    """Mode changes cannot let emergency recovery rewrite under a native ledger."""
    route = _route()
    owner = _observed_policy(route, "v8-mode-switch-owner")
    db = SessionDB(db_path=tmp_path / "v8-mode-switch-owner.db")
    session_id = db.create_session("v8-mode-switch-owner", "test", model=route.model)
    db.append_message(
        session_id,
        role="assistant",
        content="",
        codex_output_items=_sidecar(route, "v8-mode-switch-owner"),
        codex_responses_compaction_policy=owner.to_dict(),
        expected_codex_responses_compaction_revision=0,
    )
    durable_owner = load_policy_for_route(db, session_id, route)
    agent = _runtime_agent(monkeypatch, tmp_path)
    agent._session_db = db
    agent.__dict__["session_id"] = session_id
    agent.__dict__["codex_responses_auto_compaction"] = "hermes"
    agent._native_compaction_policy = NativeCompactionPolicy(route=route)

    emergency_authorization = prepare_emergency_hermes_compaction(
        agent, reason="provider_context_overflow_after_mode_switch"
    )
    assert emergency_authorization is not False
    handed_off = load_policy_for_route(db, session_id, route)
    assert handed_off.capability == "quarantined"
    assert handed_off.revision == durable_owner.revision + 1
    assert agent._native_compaction_policy == handed_off
    db.close()


@pytest.mark.parametrize("guard_shape", ["malformed", "overflow"])
def test_v8_sink_fails_closed_for_registry_sentinels(
    monkeypatch, tmp_path, guard_shape,
):
    """Malformed or overflowed route custody blocks every automatic sink."""
    from agent import conversation_compression
    from agent import responses_compaction

    agent = _runtime_agent(monkeypatch, tmp_path)
    agent._native_compaction_policy = NativeCompactionPolicy(route=_route())
    agent.__dict__["_session_db"] = None
    if guard_shape == "malformed":
        setattr(
            agent,
            responses_compaction._FAILED_RECEIPTS_ATTR,
            {"not-a-route-key": "bad"},
        )
    else:
        agent._native_compaction_failed_receipts_overflow = True
    messages = [{"role": "user", "content": "preserve sentinel custody"}]

    def _forbidden_compression(*_args, **_kwargs):
        raise AssertionError("registry sentinel failed open at compression sink")

    monkeypatch.setattr(
        conversation_compression,
        "compress_context",
        _forbidden_compression,
    )

    compressed, prompt = agent._compress_context(messages, "stable system")
    assert compressed is messages
    assert prompt == "stable system"


def test_v8_sink_fails_closed_when_durable_route_read_fails(
    monkeypatch, tmp_path,
):
    """The sink cannot infer Hermes custody after a failed durable read."""
    from agent import conversation_compression

    agent = _runtime_agent(monkeypatch, tmp_path)
    agent._session_db = _ReadFailureDB(NativeCompactionLedger.empty().to_dict())
    agent.__dict__["session_id"] = "v8-sink-read-failure"
    agent._native_compaction_policy = NativeCompactionPolicy(route=_route())
    messages = [{"role": "user", "content": "preserve failed read"}]

    def _forbidden_compression(*_args, **_kwargs):
        raise AssertionError("failed durable read opened the compression sink")

    monkeypatch.setattr(
        conversation_compression,
        "compress_context",
        _forbidden_compression,
    )

    compressed, prompt = agent._compress_context(messages, "stable system")
    assert compressed is messages
    assert prompt == "stable system"


def test_v8_sink_does_not_apply_route_a_guard_to_unrelated_route_b(
    monkeypatch, tmp_path,
):
    """A same-process route-A failure cannot block unrelated route B."""
    from agent import conversation_compression

    route_a = _route("gpt-5.6-sol-route-a")
    route_b = _route("gpt-5.6-sol-route-b")
    failed_a = failed_closed_transition_receipt(
        _observed_policy(route_a, "v8-route-a-owner"),
        error="route_a_write_failed",
    )
    agent = _runtime_agent(monkeypatch, tmp_path)
    agent.__dict__["model"] = route_b.model
    agent._native_compaction_policy = NativeCompactionPolicy(route=route_b)
    agent.__dict__["_session_db"] = None
    record_native_compaction_transition_receipt(agent, failed_a)
    messages = [{"role": "user", "content": "route B remains available"}]
    calls = []

    def _route_b_compression(*args, **kwargs):
        calls.append((args, kwargs))
        return ([{"role": "assistant", "content": "route B summary"}], "rebuilt")

    monkeypatch.setattr(
        conversation_compression,
        "compress_context",
        _route_b_compression,
    )

    compressed, prompt = agent._compress_context(messages, "stable system")
    assert compressed == [{"role": "assistant", "content": "route B summary"}]
    assert prompt == "rebuilt"
    assert len(calls) == 1
