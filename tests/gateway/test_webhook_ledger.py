"""Focused durability and transition tests for the inbound webhook ledger."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import MappingProxyType

import pytest

import gateway.platforms.webhook_ledger as ledger_module
from gateway.platforms.webhook_auth import (
    WebhookLocalBypassReceipt,
    WebhookSignatureVerificationReceipt,
)
from gateway.platforms.webhook_contract import WebhookEnvelope, WebhookRouteConfig
from gateway.platforms.webhook_ledger import (
    AdmitDisposition,
    OperationState,
    RecoveryBatch,
    Settlement,
    SettlementKind,
    TargetAttemptDisposition,
    TargetState,
    WebhookLedgerCorruptionError,
    WebhookLedgerError,
    WebhookLedgerTransitionError,
    WebhookOperationLedger,
    content_sha256,
)


def _envelope(
    *,
    delivery_id: str | None = "delivery-1",
    trace_id: str = "trace-1",
    payload: dict | None = None,
    profile: str = "default",
    route_name: str = "events",
    provider: str = "github",
    local_bypass: bool = False,
) -> WebhookEnvelope:
    body_payload = (
        payload
        if payload is not None
        else {"event_type": "push", "value": delivery_id or trace_id}
    )
    raw_body = json.dumps(
        body_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    headers = (
        {
            "X-GitHub-Event": "push",
            **({"X-GitHub-Delivery": delivery_id} if delivery_id is not None else {}),
        }
        if provider == "github"
        else {"svix-id": delivery_id}
        if delivery_id is not None
        else {}
    )
    route = WebhookRouteConfig.bind(
        route_name,
        {"provider": provider, "profile": profile},
        headers=headers,
        request_profile=profile,
    )
    receipt_type = (
        WebhookLocalBypassReceipt
        if local_bypass
        else WebhookSignatureVerificationReceipt
    )
    receipt = receipt_type._issue(route, raw_body, headers)
    return WebhookEnvelope.from_receipt(
        receipt,
        raw_body=raw_body,
        media_type="application/json",
        trace_id=trace_id,
    )


def _snapshots(label: str = "one") -> dict[str, dict]:
    return {
        "event_snapshot": {
            "event_type": "push",
            "payload": {"label": label, "items": [1, 2]},
        },
        "target_snapshot": {
            "type": "slack",
            "channel": "C123",
            "template": "{label}",
        },
        "grant_snapshot": {"toolsets": ["webhook", "slack"]},
    }


def _admit_and_prepare(
    ledger: WebhookOperationLedger,
    *,
    delivery_id: str,
    trace_id: str,
):
    admitted = ledger.admit(_envelope(delivery_id=delivery_id, trace_id=trace_id))
    assert admitted.disposition is AdmitDisposition.ACCEPTED
    assert admitted.authority is not None
    return ledger.prepare(admitted.authority, **_snapshots(delivery_id))


def _stage(
    ledger: WebhookOperationLedger,
    authority,
    *,
    content: str = "agent response",
    carrier: dict | None = None,
):
    assert ledger.mark_running(authority)
    return ledger.stage_delivery(
        authority,
        content=content,
        carrier_snapshot=carrier or {"delivery_type": "slack", "channel": "C123"},
    )


def test_running_gate_has_exactly_one_winner(tmp_path: Path):
    db_path = tmp_path / "state.db"
    first = WebhookOperationLedger(db_path, instance_id="running-owner")
    second = WebhookOperationLedger(db_path, instance_id="running-owner")
    authority = _admit_and_prepare(
        first,
        delivery_id="one-run",
        trace_id="one-run-trace",
    )
    barrier = Barrier(2)

    def start(handle: WebhookOperationLedger) -> bool:
        barrier.wait(timeout=5)
        return handle.mark_running(authority)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(start, (first, second)))

    assert sorted(results) == [False, True]
    current = first.lookup_session(authority.session_key)
    assert current is not None
    assert current.state is OperationState.RUNNING


def test_script_start_gate_has_exactly_one_winner(tmp_path: Path):
    db_path = tmp_path / "state.db"
    first = WebhookOperationLedger(db_path, instance_id="script-owner")
    second = WebhookOperationLedger(db_path, instance_id="script-owner")
    admitted = first.admit(
        _envelope(delivery_id="one-script", trace_id="one-script-trace")
    )
    assert admitted.authority is not None
    authority = admitted.authority

    assert first.mark_script_started(authority)
    assert not first.mark_script_started(authority)

    other = first.admit(
        _envelope(delivery_id="racing-script", trace_id="racing-script-trace")
    )
    assert other.authority is not None
    barrier = Barrier(2)

    def start(handle: WebhookOperationLedger) -> bool:
        barrier.wait(timeout=5)
        return handle.mark_script_started(other.authority)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(start, (first, second)))

    assert sorted(results) == [False, True]


def test_atomic_stable_admission_across_connections(tmp_path: Path):
    db_path = tmp_path / "state.db"
    first = WebhookOperationLedger(db_path)
    second = WebhookOperationLedger(db_path)
    barrier = Barrier(2)
    envelopes = (
        _envelope(delivery_id="same-delivery", trace_id="trace-a"),
        _envelope(delivery_id="same-delivery", trace_id="trace-b"),
    )

    def admit(index: int):
        barrier.wait(timeout=5)
        return (first, second)[index].admit(envelopes[index])

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(admit, range(2)))

    assert {result.disposition for result in results} == {
        AdmitDisposition.ACCEPTED,
        AdmitDisposition.ACTIVE,
    }
    assert first.count() == 1
    assert {
        result.authority.operation_id
        for result in results
        if result.authority is not None
    } == {results[0].authority.operation_id}


def test_replay_identity_uses_authenticated_coverage_not_observed_headers(
    tmp_path: Path,
):
    ledger = WebhookOperationLedger(tmp_path / "state.db")
    native = ledger.admit(
        _envelope(
            delivery_id="provider-id",
            trace_id="trace-a",
            provider="svix",
        )
    )
    native_conflict = ledger.admit(
        _envelope(
            delivery_id="provider-id",
            trace_id="trace-b",
            payload={"event_type": "push", "value": 2},
            provider="svix",
        )
    )
    shared_body = {"event_type": "push", "value": "same-authenticated-body"}
    observed_a = ledger.admit(
        _envelope(delivery_id="unsigned-a", trace_id="observed-a", payload=shared_body)
    )
    observed_b = ledger.admit(
        _envelope(delivery_id="unsigned-b", trace_id="observed-b", payload=shared_body)
    )
    changed_body = ledger.admit(
        _envelope(
            delivery_id="unsigned-a",
            trace_id="observed-c",
            payload={"event_type": "push", "value": "different-body"},
        )
    )

    assert native.disposition is AdmitDisposition.ACCEPTED
    assert native_conflict.disposition is AdmitDisposition.CONFLICT
    assert observed_a.disposition is AdmitDisposition.ACCEPTED
    assert observed_b.disposition is AdmitDisposition.ACTIVE
    assert changed_body.disposition is AdmitDisposition.ACCEPTED
    assert ledger.count() == 3


def test_prepare_persists_exact_frozen_session_authority_across_restart(
    tmp_path: Path,
):
    db_path = tmp_path / "state.db"
    ledger = WebhookOperationLedger(db_path)
    accepted = ledger.admit(_envelope(delivery_id="restart", trace_id="restart-trace"))
    assert accepted.authority is not None
    prepared = ledger.prepare(accepted.authority, **_snapshots("restart"))

    restarted = WebhookOperationLedger(db_path)
    restored = restarted.lookup_session(prepared.session_key)

    assert restored is not None
    assert restored.state is OperationState.READY
    assert restored.operation_id == prepared.operation_id
    assert restored.target_state is TargetState.PENDING
    assert restored.event_snapshot["event_type"] == "push"
    assert restored.event_snapshot["payload"]["label"] == "restart"
    assert restored.event_snapshot["payload"]["items"] == (1, 2)
    assert restored.target_snapshot == _snapshots("restart")["target_snapshot"]
    assert restored.grant_snapshot["toolsets"] == ("webhook", "slack")
    assert isinstance(restored.event_snapshot, MappingProxyType)
    with pytest.raises(TypeError):
        restored.grant_snapshot["toolsets"] = []  # type: ignore[index]


def test_prepare_is_idempotent_only_for_the_exact_snapshot(tmp_path: Path):
    ledger = WebhookOperationLedger(tmp_path / "state.db")
    accepted = ledger.admit(_envelope(delivery_id="prepare", trace_id="prepare-trace"))
    assert accepted.authority is not None
    prepared = ledger.prepare(accepted.authority, **_snapshots("same"))

    repeated = ledger.prepare(accepted.authority, **_snapshots("same"))
    assert repeated == prepared
    with pytest.raises(WebhookLedgerTransitionError):
        ledger.prepare(accepted.authority, **_snapshots("different"))


def test_prepare_accepts_nested_immutable_json_projections(tmp_path: Path):
    ledger = WebhookOperationLedger(tmp_path / "state.db")
    accepted = ledger.admit(
        _envelope(delivery_id="immutable", trace_id="immutable-trace")
    )
    assert accepted.authority is not None

    prepared = ledger.prepare(
        accepted.authority,
        event_snapshot=MappingProxyType({
            "payload": MappingProxyType({"items": ("a", "b")})
        }),
        target_snapshot=MappingProxyType({"deliver": "log"}),
        grant_snapshot=MappingProxyType({"toolsets": ("webhook",)}),
    )

    assert prepared.event_snapshot["payload"]["items"] == ("a", "b")
    assert prepared.grant_snapshot["toolsets"] == ("webhook",)


def test_prepare_rolls_back_target_when_operation_commit_fails(tmp_path: Path):
    db_path = tmp_path / "state.db"
    ledger = WebhookOperationLedger(db_path)
    accepted = ledger.admit(
        _envelope(delivery_id="rollback", trace_id="rollback-trace")
    )
    assert accepted.authority is not None
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TRIGGER reject_webhook_prepare
               BEFORE UPDATE OF state ON webhook_operations
               WHEN NEW.state='ready'
               BEGIN SELECT RAISE(ABORT, 'injected prepare failure'); END"""
        )

    with pytest.raises(WebhookLedgerError, match="SQLITE_CONSTRAINT_TRIGGER"):
        ledger.prepare(accepted.authority, **_snapshots("rollback"))

    with sqlite3.connect(db_path) as conn:
        state = conn.execute(
            "SELECT state FROM webhook_operations WHERE operation_id=?",
            (accepted.authority.operation_id,),
        ).fetchone()[0]
        target_count = conn.execute("SELECT COUNT(*) FROM webhook_targets").fetchone()[
            0
        ]
    assert state == OperationState.PREPARING.value
    assert target_count == 0


def test_stage_delivery_rolls_back_target_when_operation_commit_fails(tmp_path: Path):
    db_path = tmp_path / "state.db"
    ledger = WebhookOperationLedger(db_path)
    prepared = _admit_and_prepare(
        ledger,
        delivery_id="stage-rollback",
        trace_id="stage-rollback-trace",
    )
    assert ledger.mark_running(prepared)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TRIGGER reject_webhook_stage
               BEFORE UPDATE OF state ON webhook_operations
               WHEN NEW.state='delivery_ready'
               BEGIN SELECT RAISE(ABORT, 'injected stage failure'); END"""
        )

    with pytest.raises(WebhookLedgerError, match="SQLITE_CONSTRAINT_TRIGGER"):
        ledger.stage_delivery(
            prepared,
            content="response",
            carrier_snapshot={"kind": "platform", "chat_id": "C123"},
        )

    restored = WebhookOperationLedger(db_path).lookup_session(prepared.session_key)
    assert restored is not None
    assert restored.state is OperationState.RUNNING
    assert restored.target_state is TargetState.PENDING
    assert restored.delivery is None


def test_begin_target_rolls_back_attempt_when_operation_commit_fails(tmp_path: Path):
    db_path = tmp_path / "state.db"
    ledger = WebhookOperationLedger(db_path)
    prepared = _admit_and_prepare(
        ledger,
        delivery_id="begin-rollback",
        trace_id="begin-rollback-trace",
    )
    staged = _stage(ledger, prepared, content="response")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TRIGGER reject_webhook_begin
               BEFORE UPDATE OF state ON webhook_operations
               WHEN NEW.state='delivering'
               BEGIN SELECT RAISE(ABORT, 'injected begin failure'); END"""
        )

    with pytest.raises(WebhookLedgerError, match="SQLITE_CONSTRAINT_TRIGGER"):
        ledger.begin_target(staged)

    restored = WebhookOperationLedger(db_path).lookup_session(prepared.session_key)
    assert restored is not None
    assert restored.state is OperationState.DELIVERY_READY
    assert restored.target_state is TargetState.PENDING


def test_settle_target_rolls_back_target_when_operation_commit_fails(tmp_path: Path):
    db_path = tmp_path / "state.db"
    ledger = WebhookOperationLedger(db_path)
    prepared = _admit_and_prepare(
        ledger,
        delivery_id="settle-rollback",
        trace_id="settle-rollback-trace",
    )
    staged = _stage(ledger, prepared, content="response")
    attempt = ledger.begin_target(staged)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TRIGGER reject_webhook_settle
               BEFORE UPDATE OF state ON webhook_operations
               WHEN NEW.state='settled'
               BEGIN SELECT RAISE(ABORT, 'injected settle failure'); END"""
        )

    with pytest.raises(WebhookLedgerError, match="SQLITE_CONSTRAINT_TRIGGER"):
        ledger.settle_target(
            attempt,
            Settlement(SettlementKind.CONFIRMED, external_id="message-1"),
        )

    restored = WebhookOperationLedger(db_path).lookup_session(prepared.session_key)
    assert restored is not None
    assert restored.state is OperationState.DELIVERING
    assert restored.target_state is TargetState.ATTEMPTING


def test_settle_no_effect_rolls_back_operation_when_target_commit_fails(
    tmp_path: Path,
):
    db_path = tmp_path / "state.db"
    ledger = WebhookOperationLedger(db_path)
    prepared = _admit_and_prepare(
        ledger,
        delivery_id="no-effect-rollback",
        trace_id="no-effect-rollback-trace",
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TRIGGER reject_webhook_no_effect
               BEFORE UPDATE OF state ON webhook_targets
               WHEN NEW.state='suppressed'
               BEGIN SELECT RAISE(ABORT, 'injected no-effect failure'); END"""
        )

    with pytest.raises(WebhookLedgerError, match="SQLITE_CONSTRAINT_TRIGGER"):
        ledger.settle_no_effect(prepared, "ignored")

    restored = WebhookOperationLedger(db_path).lookup_session(prepared.session_key)
    assert restored is not None
    assert restored.state is OperationState.READY
    assert restored.target_state is TargetState.PENDING


def test_target_attempt_is_fenced_settled_and_cached_after_restart(tmp_path: Path):
    db_path = tmp_path / "state.db"
    ledger = WebhookOperationLedger(db_path)
    prepared = _admit_and_prepare(
        ledger,
        delivery_id="target-confirm",
        trace_id="target-confirm-trace",
    )
    digest = content_sha256("agent response")
    staged = _stage(ledger, prepared, content="agent response")

    attempt = ledger.begin_target(staged, content_sha256=digest)
    assert attempt.disposition is TargetAttemptDisposition.STARTED
    assert attempt.delivery is not None
    assert attempt.delivery.content == "agent response"
    assert attempt.delivery.carrier["channel"] == "C123"
    assert ledger.settle_target(
        attempt,
        Settlement(SettlementKind.CONFIRMED, external_id="message-123"),
    )

    restarted = WebhookOperationLedger(db_path)
    restored = restarted.lookup_session(prepared.session_key)
    assert restored is not None
    assert restored.state is OperationState.SETTLED
    assert restored.target_state is TargetState.CONFIRMED
    cached = restarted.begin_target(restored, content_sha256=digest)
    assert cached.disposition is TargetAttemptDisposition.CACHED
    duplicate = restarted.admit(
        _envelope(
            delivery_id="target-confirm",
            trace_id="provider-retry-gets-a-new-trace",
        )
    )
    assert duplicate.disposition is AdmitDisposition.DUPLICATE


def test_pre_effect_failure_reopens_target_but_stale_attempt_cannot_settle(
    tmp_path: Path,
):
    ledger = WebhookOperationLedger(tmp_path / "state.db")
    prepared = _admit_and_prepare(
        ledger,
        delivery_id="retry-target",
        trace_id="retry-target-trace",
    )
    digest = content_sha256("same response")
    staged = _stage(ledger, prepared, content="same response")

    first = ledger.begin_target(staged, content_sha256=digest)
    assert ledger.settle_target(
        first,
        Settlement(SettlementKind.PRE_EFFECT_FAILED, error="socket never opened"),
    )
    retryable = ledger.lookup_session(prepared.session_key)
    assert retryable is not None
    assert retryable.state is OperationState.DELIVERY_READY
    assert retryable.target_state is TargetState.PENDING
    assert retryable.delivery is not None
    assert retryable.delivery.content == "same response"
    second = ledger.begin_target(retryable, content_sha256=digest)

    assert second.disposition is TargetAttemptDisposition.STARTED
    assert second.attempt_token != first.attempt_token
    assert not ledger.settle_target(
        first,
        Settlement(SettlementKind.CONFIRMED, external_id="stale"),
    )
    assert ledger.settle_target(
        second,
        Settlement(SettlementKind.SUPPRESSED, error="policy silence"),
    )
    restored = ledger.lookup_session(prepared.session_key)
    assert restored is not None
    assert restored.state is OperationState.SETTLED
    assert restored.target_state is TargetState.SUPPRESSED


def test_target_content_fence_rejects_mismatched_active_and_cached_attempts(
    tmp_path: Path,
):
    ledger = WebhookOperationLedger(tmp_path / "state.db")
    prepared = _admit_and_prepare(
        ledger,
        delivery_id="content-fence",
        trace_id="content-fence-trace",
    )
    original_digest = content_sha256("original response")
    different_digest = content_sha256("different response")
    staged = _stage(ledger, prepared, content="original response")

    attempt = ledger.begin_target(staged, content_sha256=original_digest)
    with pytest.raises(WebhookLedgerTransitionError, match="durable staged delivery"):
        ledger.begin_target(staged, content_sha256=different_digest)

    assert ledger.settle_target(
        attempt,
        Settlement(SettlementKind.CONFIRMED, external_id="message-1"),
    )
    settled = ledger.lookup_session(prepared.session_key)
    assert settled is not None
    with pytest.raises(WebhookLedgerTransitionError, match="durable staged delivery"):
        ledger.begin_target(settled, content_sha256=different_digest)
    assert (
        ledger.begin_target(settled, content_sha256=original_digest).disposition
        is TargetAttemptDisposition.CACHED
    )


def test_staged_delivery_is_idempotent_only_for_exact_content_and_carrier(
    tmp_path: Path,
):
    ledger = WebhookOperationLedger(tmp_path / "state.db")
    prepared = _admit_and_prepare(
        ledger,
        delivery_id="restage",
        trace_id="restage-trace",
    )
    assert ledger.mark_running(prepared)
    carrier = {"kind": "platform", "chat_id": "C123"}
    staged = ledger.stage_delivery(
        prepared,
        content="exact response",
        carrier_snapshot=carrier,
    )

    assert (
        ledger.stage_delivery(
            staged,
            content="exact response",
            carrier_snapshot=carrier,
        )
        == staged
    )
    with pytest.raises(WebhookLedgerTransitionError, match="cannot be rebound"):
        ledger.stage_delivery(
            staged,
            content="different response",
            carrier_snapshot=carrier,
        )
    with pytest.raises(WebhookLedgerTransitionError, match="cannot be rebound"):
        ledger.stage_delivery(
            staged,
            content="exact response",
            carrier_snapshot={"kind": "platform", "chat_id": "C999"},
        )


def test_dead_owner_recovers_pre_effect_failure_as_delivery_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    ledger = WebhookOperationLedger(tmp_path / "state.db")
    prepared = _admit_and_prepare(
        ledger,
        delivery_id="pre-effect-recovery",
        trace_id="pre-effect-recovery-trace",
    )
    staged = _stage(ledger, prepared, content="response")
    attempt = ledger.begin_target(
        staged,
        content_sha256=content_sha256("response"),
    )
    assert ledger.settle_target(
        attempt,
        Settlement(SettlementKind.PRE_EFFECT_FAILED, error="not invoked"),
    )

    monkeypatch.setattr(ledger_module, "_owner_alive", lambda *_: False)
    recovered = ledger.recover_dead_owners(now=1000.0)

    assert recovered.indeterminate == ()
    assert recovered.event_ready == ()
    assert [item.operation_id for item in recovered.delivery_ready] == [
        prepared.operation_id
    ]
    replay = recovered.delivery_ready[0]
    assert replay.generation == prepared.generation + 1
    assert replay.target_state is TargetState.PENDING
    assert replay.delivery is not None
    assert replay.delivery.content == "response"
    assert ledger.begin_target(replay).disposition is TargetAttemptDisposition.STARTED


def test_current_delivery_ready_lists_only_this_instances_retryable_staged_work(
    tmp_path: Path,
):
    db_path = tmp_path / "state.db"
    owner = WebhookOperationLedger(db_path, instance_id="owner")
    peer = WebhookOperationLedger(db_path, instance_id="peer")

    event_ready = _admit_and_prepare(
        owner,
        delivery_id="event-ready",
        trace_id="event-ready-trace",
    )
    first = _admit_and_prepare(
        owner,
        delivery_id="first-staged",
        trace_id="first-staged-trace",
    )
    first = _stage(owner, first, content="first exact output")
    retryable = _admit_and_prepare(
        owner,
        delivery_id="retryable-staged",
        trace_id="retryable-staged-trace",
    )
    retryable = _stage(owner, retryable, content="retry exact output")
    retry_attempt = owner.begin_target(retryable)
    assert owner.settle_target(
        retry_attempt,
        Settlement(SettlementKind.PRE_EFFECT_FAILED, error="adapter unavailable"),
    )
    attempting = _admit_and_prepare(
        owner,
        delivery_id="attempting",
        trace_id="attempting-trace",
    )
    attempting = _stage(owner, attempting, content="already attempting")
    assert (
        owner.begin_target(attempting).disposition is TargetAttemptDisposition.STARTED
    )
    peer_staged = _admit_and_prepare(
        peer,
        delivery_id="peer-staged",
        trace_id="peer-staged-trace",
    )
    peer_staged = _stage(peer, peer_staged, content="peer exact output")

    current = owner.current_delivery_ready()

    assert {item.operation_id for item in current} == {
        first.operation_id,
        retryable.operation_id,
    }
    assert event_ready.operation_id not in {item.operation_id for item in current}
    assert attempting.operation_id not in {item.operation_id for item in current}
    assert peer_staged.operation_id not in {item.operation_id for item in current}
    assert all(item.owner_instance == owner.instance_id for item in current)
    assert {
        item.operation_id: item.delivery.content if item.delivery is not None else None
        for item in current
    } == {
        first.operation_id: "first exact output",
        retryable.operation_id: "retry exact output",
    }
    assert [item.operation_id for item in peer.current_delivery_ready()] == [
        peer_staged.operation_id
    ]

    assert owner.begin_target(first).disposition is TargetAttemptDisposition.STARTED
    assert [item.operation_id for item in owner.current_delivery_ready()] == [
        retryable.operation_id
    ]


def test_racing_current_delivery_recovery_triggers_cross_one_target_gate(
    tmp_path: Path,
):
    db_path = tmp_path / "state.db"
    first_handle = WebhookOperationLedger(db_path, instance_id="live-adapter")
    second_handle = WebhookOperationLedger(db_path, instance_id="live-adapter")
    prepared = _admit_and_prepare(
        first_handle,
        delivery_id="reconnect-race",
        trace_id="reconnect-race-trace",
    )
    staged = _stage(first_handle, prepared, content="one durable output")
    barrier = Barrier(2)

    def begin(handle: WebhookOperationLedger):
        authority = handle.current_delivery_ready()[0]
        barrier.wait(timeout=5)
        return handle.begin_target(authority)

    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = list(pool.map(begin, (first_handle, second_handle)))

    assert {attempt.disposition for attempt in attempts} == {
        TargetAttemptDisposition.STARTED,
        TargetAttemptDisposition.IN_PROGRESS,
    }
    assert (
        sum(
            attempt.attempt_token is not None
            for attempt in attempts
            if attempt.disposition is TargetAttemptDisposition.STARTED
        )
        == 1
    )
    assert first_handle.current_delivery_ready() == ()
    assert second_handle.current_delivery_ready() == ()

    started = next(
        attempt
        for attempt in attempts
        if attempt.disposition is TargetAttemptDisposition.STARTED
    )
    assert first_handle.settle_target(
        started,
        Settlement(SettlementKind.CONFIRMED, external_id="message-1"),
    )
    restored = second_handle.lookup_session(staged.session_key)
    assert restored is not None
    assert restored.state is OperationState.SETTLED
    assert restored.target_state is TargetState.CONFIRMED


def test_indeterminate_target_never_retries(tmp_path: Path):
    ledger = WebhookOperationLedger(tmp_path / "state.db")
    prepared = _admit_and_prepare(
        ledger,
        delivery_id="unknown-target",
        trace_id="unknown-target-trace",
    )
    digest = content_sha256("response")
    staged = _stage(ledger, prepared, content="response")
    attempt = ledger.begin_target(staged, content_sha256=digest)
    assert not ledger.settle_no_effect(staged, "cannot bypass an active effect")
    active = ledger.lookup_session(prepared.session_key)
    assert active is not None
    assert active.state is OperationState.DELIVERING
    assert active.target_state is TargetState.ATTEMPTING
    assert ledger.settle_target(
        attempt,
        Settlement(SettlementKind.INDETERMINATE, error="connection reset after write"),
    )

    restored = ledger.lookup_session(prepared.session_key)
    assert restored is not None
    assert restored.state is OperationState.INDETERMINATE
    assert restored.target_state is TargetState.INDETERMINATE
    retry = ledger.begin_target(restored, content_sha256=digest)
    assert retry.disposition is TargetAttemptDisposition.INDETERMINATE


def test_dead_owner_recovery_replays_only_committed_pre_effect_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    ledger = WebhookOperationLedger(tmp_path / "state.db")

    safe_preparing = ledger.admit(
        _envelope(delivery_id="pre-safe", trace_id="pre-safe-trace")
    ).authority
    script_preparing = ledger.admit(
        _envelope(delivery_id="pre-script", trace_id="pre-script-trace")
    ).authority
    assert safe_preparing is not None
    assert script_preparing is not None
    assert ledger.mark_script_started(script_preparing)

    ready = _admit_and_prepare(
        ledger,
        delivery_id="ready",
        trace_id="ready-trace",
    )
    running = _admit_and_prepare(
        ledger,
        delivery_id="running",
        trace_id="running-trace",
    )
    assert ledger.mark_running(running)
    attempting = _admit_and_prepare(
        ledger,
        delivery_id="attempting",
        trace_id="attempting-trace",
    )
    attempting = _stage(ledger, attempting, content="response")
    attempt = ledger.begin_target(
        attempting,
        content_sha256=content_sha256("response"),
    )
    assert attempt.disposition is TargetAttemptDisposition.STARTED

    monkeypatch.setattr(ledger_module, "_owner_alive", lambda *_: False)
    recovered = ledger.recover_dead_owners(now=1000.0)

    assert recovered.released == (safe_preparing.operation_id,)
    assert {item.operation_id for item in recovered.event_ready} == {ready.operation_id}
    assert recovered.delivery_ready == ()
    assert set(recovered.indeterminate) == {
        script_preparing.operation_id,
        running.operation_id,
        attempting.operation_id,
    }
    replay = recovered.event_ready[0]
    assert replay.generation == ready.generation + 1
    with pytest.raises(WebhookLedgerTransitionError):
        ledger.stage_delivery(
            ready,
            content="stale",
            carrier_snapshot={"delivery_type": "log"},
        )
    assert ledger.mark_running(replay)
    replay = ledger.stage_delivery(
        replay,
        content="fresh",
        carrier_snapshot={"delivery_type": "log"},
    )
    assert (
        ledger.begin_target(replay, content_sha256=content_sha256("fresh")).disposition
        is TargetAttemptDisposition.STARTED
    )
    attempted_state = ledger.lookup_session(attempting.session_key)
    assert attempted_state is not None
    assert attempted_state.state is OperationState.INDETERMINATE
    assert attempted_state.target_state is TargetState.INDETERMINATE


def test_same_pid_instance_retirement_fences_old_owner_without_stealing_peer(
    tmp_path: Path,
):
    db_path = tmp_path / "state.db"
    old = WebhookOperationLedger(db_path, instance_id="old-adapter")
    peer = WebhookOperationLedger(db_path, instance_id="live-peer")
    replacement = WebhookOperationLedger(db_path, instance_id="replacement")

    event_ready = _admit_and_prepare(
        old,
        delivery_id="old-event-ready",
        trace_id="old-event-ready-trace",
    )
    delivery_ready = _admit_and_prepare(
        old,
        delivery_id="old-delivery-ready",
        trace_id="old-delivery-ready-trace",
    )
    delivery_ready = _stage(old, delivery_ready, content="persisted output")
    running = _admit_and_prepare(
        old,
        delivery_id="old-running",
        trace_id="old-running-trace",
    )
    assert old.mark_running(running)
    peer_ready = _admit_and_prepare(
        peer,
        delivery_id="peer-ready",
        trace_id="peer-ready-trace",
    )

    # A different object in the same live PID is not evidence of owner death.
    assert replacement.recover_dead_owners().event_ready == ()
    retired = old.retire_instance(now=500.0)
    assert retired.indeterminate == (running.operation_id,)
    assert not old.mark_running(event_ready)
    with pytest.raises(
        WebhookLedgerTransitionError, match="different adapter instance"
    ):
        old.begin_target(delivery_ready)

    recovered = replacement.recover_dead_owners(now=501.0)
    assert {item.operation_id for item in recovered.event_ready} == {
        event_ready.operation_id
    }
    assert {item.operation_id for item in recovered.delivery_ready} == {
        delivery_ready.operation_id
    }
    assert all(
        item.owner_instance == replacement.instance_id
        for item in (*recovered.event_ready, *recovered.delivery_ready)
    )
    still_peer_owned = peer.lookup_session(peer_ready.session_key)
    assert still_peer_owned is not None
    assert still_peer_owned.owner_instance == peer.instance_id
    running_state = replacement.lookup_session(running.session_key)
    assert running_state is not None
    assert running_state.state is OperationState.INDETERMINATE


def test_replacement_retries_exact_prior_owner_retirement_idempotently(
    tmp_path: Path,
):
    db_path = tmp_path / "state.db"
    old = WebhookOperationLedger(db_path, instance_id="quarantined-adapter")
    peer = WebhookOperationLedger(db_path, instance_id="unrelated-live-peer")
    replacement = WebhookOperationLedger(db_path, instance_id="replacement")

    preparing = old.admit(
        _envelope(
            delivery_id="quarantined-preparing",
            trace_id="quarantined-preparing-trace",
        )
    )
    assert preparing.authority is not None
    ready = _admit_and_prepare(
        old,
        delivery_id="quarantined-ready",
        trace_id="quarantined-ready-trace",
    )
    running = _admit_and_prepare(
        old,
        delivery_id="quarantined-running",
        trace_id="quarantined-running-trace",
    )
    assert old.mark_running(running)
    peer_ready = _admit_and_prepare(
        peer,
        delivery_id="peer-stays-live",
        trace_id="peer-stays-live-trace",
    )

    assert replacement.recover_dead_owners().event_ready == ()
    retired = replacement.retire_owner_instance(
        old.instance_id,
        now=700.0,
    )
    assert retired.released == (preparing.authority.operation_id,)
    assert retired.indeterminate == (running.operation_id,)
    assert (
        replacement.retire_owner_instance(
            old.instance_id,
            now=701.0,
        )
        == RecoveryBatch()
    )

    recovered = replacement.recover_dead_owners(now=702.0)
    assert tuple(item.operation_id for item in recovered.event_ready) == (
        ready.operation_id,
    )
    peer_state = replacement.lookup_session(peer_ready.session_key)
    assert peer_state is not None
    assert peer_state.owner_instance == peer.instance_id
    running_state = replacement.lookup_session(running.session_key)
    assert running_state is not None
    assert running_state.state is OperationState.INDETERMINATE


def test_capacity_compacts_heavy_rows_but_stable_identity_never_reopens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    clock = {"now": 100.0}
    monkeypatch.setattr(ledger_module.time, "time", lambda: clock["now"])
    ledger = WebhookOperationLedger(
        tmp_path / "state.db",
        max_records=1,
        terminal_retention_seconds=10,
    )
    accepted = ledger.admit(
        _envelope(delivery_id="old", trace_id="old-trace", provider="svix")
    )
    assert accepted.authority is not None
    assert ledger.settle_no_effect(accepted.authority, "filtered")

    clock["now"] = 105.0
    assert (
        ledger.admit(
            _envelope(delivery_id="new", trace_id="new-trace", provider="svix")
        ).disposition
        is AdmitDisposition.ACCEPTED
    )
    assert ledger.count() == 1
    assert ledger.tombstone_count() == 1
    ancient_retry = ledger.admit(
        _envelope(delivery_id="old", trace_id="ancient-retry", provider="svix")
    )
    assert ancient_retry.disposition is AdmitDisposition.DUPLICATE
    assert ancient_retry.authority is None
    assert ancient_retry.tombstone is not None
    assert ancient_retry.tombstone.operation_id == "old-trace"
    ancient_conflict = ledger.admit(
        _envelope(
            delivery_id="old",
            trace_id="ancient-conflict",
            payload={"event_type": "push", "value": 999},
            provider="svix",
        )
    )
    assert ancient_conflict.disposition is AdmitDisposition.CONFLICT
    assert ancient_conflict.tombstone == ancient_retry.tombstone


def test_remote_body_only_settlement_replay_fence_never_reopens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    clock = {"now": 100.0}
    monkeypatch.setattr(ledger_module.time, "time", lambda: clock["now"])
    ledger = WebhookOperationLedger(
        tmp_path / "state.db",
        local_bypass_replay_retention_seconds=10,
        terminal_retention_seconds=1,
    )
    payload = {"event_type": "push", "value": "identical"}
    first = ledger.admit(
        _envelope(
            delivery_id="observed-a",
            trace_id="body-a",
            payload=payload,
            provider="github",
        )
    )
    assert first.authority is not None
    assert ledger.settle_no_effect(first.authority, "complete")

    clock["now"] = 102.0
    assert ledger.prune() == 1
    assert ledger.count() == 0
    assert ledger.tombstone_count() == 1

    clock["now"] = 109.0
    assert (
        ledger.admit(
            _envelope(
                delivery_id="observed-b",
                trace_id="body-b",
                payload=payload,
                provider="github",
            )
        ).disposition
        is AdmitDisposition.DUPLICATE
    )

    clock["now"] = 110.0
    permanent_retry = ledger.admit(
        _envelope(
            delivery_id="observed-c",
            trace_id="body-c",
            payload=payload,
            provider="github",
        )
    )
    assert permanent_retry.disposition is AdmitDisposition.DUPLICATE


def test_local_bypass_settlement_replay_fence_expires_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    clock = {"now": 100.0}
    monkeypatch.setattr(ledger_module.time, "time", lambda: clock["now"])
    ledger = WebhookOperationLedger(
        tmp_path / "state.db",
        local_bypass_replay_retention_seconds=10,
        terminal_retention_seconds=1,
    )
    payload = {"event_type": "push", "value": "local-test"}
    first = ledger.admit(
        _envelope(
            delivery_id="observed-a",
            trace_id="local-a",
            payload=payload,
            local_bypass=True,
        )
    )
    assert first.authority is not None
    assert ledger.settle_no_effect(first.authority, "complete")

    clock["now"] = 102.0
    assert ledger.prune() == 1
    assert ledger.count() == 0
    assert ledger.tombstone_count() == 1

    clock["now"] = 109.0
    assert (
        ledger.admit(
            _envelope(
                delivery_id="observed-b",
                trace_id="local-b",
                payload=payload,
                local_bypass=True,
            )
        ).disposition
        is AdmitDisposition.DUPLICATE
    )

    clock["now"] = 110.0
    reopened = ledger.admit(
        _envelope(
            delivery_id="observed-c",
            trace_id="local-c",
            payload=payload,
            local_bypass=True,
        )
    )
    assert reopened.disposition is AdmitDisposition.ACCEPTED
    assert reopened.authority is not None
    assert reopened.authority.operation_id == "local-c"


def test_one_route_cannot_consume_the_shared_capacity_reserve(tmp_path: Path):
    ledger = WebhookOperationLedger(tmp_path / "state.db", max_records=4)
    for index in range(3):
        admitted = ledger.admit(
            _envelope(
                delivery_id=f"route-a-{index}",
                trace_id=f"route-a-trace-{index}",
                route_name="route-a",
                provider="svix",
            )
        )
        assert admitted.disposition is AdmitDisposition.ACCEPTED

    assert (
        ledger.admit(
            _envelope(
                delivery_id="route-a-overflow",
                trace_id="route-a-overflow-trace",
                route_name="route-a",
                provider="svix",
            )
        ).disposition
        is AdmitDisposition.SATURATED
    )
    assert (
        ledger.admit(
            _envelope(
                delivery_id="route-b-reserved",
                trace_id="route-b-reserved-trace",
                route_name="route-b",
                provider="svix",
            )
        ).disposition
        is AdmitDisposition.ACCEPTED
    )


def test_indeterminate_authority_preserves_evidence_without_exhausting_live_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    clock = {"now": 100.0}
    monkeypatch.setattr(ledger_module.time, "time", lambda: clock["now"])
    ledger = WebhookOperationLedger(
        tmp_path / "state.db",
        max_records=1,
        terminal_retention_seconds=1,
    )
    accepted = ledger.admit(_envelope(delivery_id="unknown", trace_id="unknown-trace"))
    assert accepted.authority is not None
    assert ledger.mark_indeterminate(accepted.authority, "unknown postcondition")

    clock["now"] = 10_000.0
    assert ledger.prune() == 0
    assert (
        ledger.admit(_envelope(delivery_id="later", trace_id="later-trace")).disposition
        is AdmitDisposition.ACCEPTED
    )
    retained = ledger.lookup_session(accepted.authority.session_key)
    assert retained is not None
    assert retained.state is OperationState.INDETERMINATE
    assert ledger.tombstone_count() == 0
    assert (
        ledger.admit(
            _envelope(delivery_id="unknown", trace_id="retry-trace")
        ).disposition
        is AdmitDisposition.INDETERMINATE
    )


def test_sqlite_write_failure_propagates_without_in_memory_fallback(tmp_path: Path):
    db_path = tmp_path / "state.db"
    ledger = WebhookOperationLedger(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TRIGGER reject_webhook_admit
               BEFORE INSERT ON webhook_operations
               BEGIN SELECT RAISE(ABORT, 'injected admission failure'); END"""
        )

    with pytest.raises(WebhookLedgerError, match="SQLITE_CONSTRAINT_TRIGGER"):
        ledger.admit(_envelope(delivery_id="failure", trace_id="failure-trace"))
    assert ledger.count() == 0


@pytest.mark.parametrize(
    "corrupt_json",
    [
        '{"toolsets":["safe"],"toolsets":["unsafe"]}',
        '{"score":NaN}',
        '{ "toolsets": ["webhook"] }',
    ],
)
def test_corrupted_authority_json_fails_closed(tmp_path: Path, corrupt_json: str):
    db_path = tmp_path / "state.db"
    ledger = WebhookOperationLedger(db_path)
    prepared = _admit_and_prepare(
        ledger,
        delivery_id="corrupt-json",
        trace_id="corrupt-json-trace",
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE webhook_operations SET grant_json=? WHERE operation_id=?",
            (corrupt_json, prepared.operation_id),
        )

    with pytest.raises(WebhookLedgerCorruptionError):
        ledger.lookup_session(prepared.session_key)


def test_staged_delivery_digest_corruption_fails_closed(tmp_path: Path):
    db_path = tmp_path / "state.db"
    ledger = WebhookOperationLedger(db_path)
    prepared = _admit_and_prepare(
        ledger,
        delivery_id="corrupt-delivery",
        trace_id="corrupt-delivery-trace",
    )
    staged = _stage(ledger, prepared, content="original")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """UPDATE webhook_targets SET delivery_json=?
               WHERE operation_id=?""",
            (
                '{"carrier":{"channel":"C123","delivery_type":"slack"},'
                '"content":"mutated"}',
                staged.operation_id,
            ),
        )

    with pytest.raises(WebhookLedgerCorruptionError, match="does not match"):
        ledger.lookup_session(staged.session_key)


def test_staged_delivery_carrier_digest_corruption_fails_closed(tmp_path: Path):
    db_path = tmp_path / "state.db"
    ledger = WebhookOperationLedger(db_path)
    prepared = _admit_and_prepare(
        ledger,
        delivery_id="corrupt-carrier",
        trace_id="corrupt-carrier-trace",
    )
    staged = _stage(ledger, prepared, content="original")
    with sqlite3.connect(db_path) as conn:
        original = conn.execute(
            """SELECT delivery_json FROM webhook_targets
               WHERE operation_id=?""",
            (staged.operation_id,),
        ).fetchone()[0]
        decoded = json.loads(original)
        decoded["carrier"]["channel"] = "C999"
        mutated = json.dumps(decoded, separators=(",", ":"), sort_keys=True)
        conn.execute(
            """UPDATE webhook_targets SET delivery_json=?
               WHERE operation_id=?""",
            (mutated, staged.operation_id),
        )

    with pytest.raises(WebhookLedgerCorruptionError, match="carrier does not match"):
        ledger.lookup_session(staged.session_key)


def test_incompatible_uniqueness_schema_fails_closed(tmp_path: Path):
    db_path = tmp_path / "state.db"
    WebhookOperationLedger(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP INDEX idx_webhook_operations_replay_identity")
        conn.execute(
            """CREATE INDEX idx_webhook_operations_replay_identity
               ON webhook_operations(profile, route, provider, replay_id)"""
        )

    with pytest.raises(
        WebhookLedgerCorruptionError,
        match="replay-identity uniqueness is unavailable",
    ):
        WebhookOperationLedger(db_path)
