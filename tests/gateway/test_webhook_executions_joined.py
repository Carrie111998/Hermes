"""Joined authority regressions for webhook status and cancellation."""

from __future__ import annotations

from dataclasses import fields
import hashlib
import sqlite3
from pathlib import Path

import pytest

from gateway.platforms.webhook_executions import (
    CancelDisposition,
    CancellationState,
    ExecutionPhase,
    ExecutionStatus,
    WebhookExecutionProjection,
)
from gateway.platforms.webhook_ledger import (
    AdmitDisposition,
    OperationState,
    Settlement,
    SettlementKind,
    TargetAttemptDisposition,
    TargetState,
    WebhookLedgerCorruptionError,
    WebhookOperationLedger,
)
from tests.gateway.test_webhook_ledger import _envelope, _snapshots


def _admit(
    ledger: WebhookOperationLedger,
    *,
    delivery_id: str = "execution-delivery",
    profile: str = "default",
    route: str = "events",
):
    result = ledger.admit(
        _envelope(
            delivery_id=delivery_id,
            trace_id=f"{delivery_id}-trace",
            profile=profile,
            route_name=route,
        )
    )
    assert result.disposition is AdmitDisposition.ACCEPTED
    assert result.authority is not None
    return result.authority


def _issue(
    tmp_path: Path,
    *,
    delivery_id: str = "execution-delivery",
    profile: str = "default",
    route: str = "events",
    **projection_options,
):
    db_path = tmp_path / "state.db"
    ledger = WebhookOperationLedger(db_path, instance_id="execution-owner")
    authority = _admit(
        ledger,
        delivery_id=delivery_id,
        profile=profile,
        route=route,
    )
    projection = WebhookExecutionProjection(ledger, **projection_options)
    issued = projection.issue(authority, now=100.0)
    assert issued.access_token is not None
    return db_path, ledger, authority, projection, issued


def test_projection_schema_is_joined_idempotent_and_stores_only_token_hash(
    tmp_path: Path,
) -> None:
    db_path, ledger, authority, projection, issued = _issue(tmp_path)
    assert issued.created
    assert issued.expires_at == 3700.0

    repeated = projection.issue(authority, now=101.0)
    assert repeated.execution_id == issued.execution_id
    assert repeated.access_token is None
    assert not repeated.created
    assert repeated.expires_at == issued.expires_at

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        stored = conn.execute("SELECT * FROM webhook_execution_capabilities").fetchone()
        assert stored is not None
        assert stored["operation_id"] == authority.operation_id
        assert stored["issued_generation"] == authority.generation
        assert (
            stored["token_hash"]
            == hashlib.sha256(issued.access_token.encode("utf-8")).hexdigest()
        )
        assert issued.access_token not in tuple(str(value) for value in stored)
        foreign_keys = conn.execute(
            "PRAGMA foreign_key_list(webhook_execution_capabilities)"
        ).fetchall()
        assert any(
            row[2] == "webhook_operations"
            and row[3] == "operation_id"
            and row[4] == "operation_id"
            and row[6].upper() == "CASCADE"
            for row in foreign_keys
        )

        with pytest.raises(
            sqlite3.IntegrityError,
            match="webhook_execution_capability_authority_immutable",
        ):
            conn.execute(
                """UPDATE webhook_execution_capabilities
                      SET token_hash=? WHERE execution_id=?""",
                ("0" * 64, issued.execution_id),
            )

    # Extra joined tables do not shadow or invalidate the canonical ledger, and
    # migration remains idempotent across independently constructed shards.
    peer_ledger = WebhookOperationLedger(db_path, instance_id="peer-owner")
    peer_projection = WebhookExecutionProjection(peer_ledger)
    status = peer_projection.status(
        issued.execution_id,
        issued.access_token,
        profile=authority.profile,
        route=authority.route,
        now=102.0,
    )
    assert status is not None
    assert status.operation_state is OperationState.PREPARING


def test_status_is_a_redacted_live_projection_of_operation_and_target(
    tmp_path: Path,
) -> None:
    _, ledger, authority, projection, issued = _issue(tmp_path)
    assert issued.access_token is not None

    accepted = projection.status(
        issued.execution_id,
        issued.access_token,
        profile=authority.profile,
        route=authority.route,
        now=101.0,
    )
    assert accepted is not None
    assert accepted.phase is ExecutionPhase.ACCEPTED
    assert accepted.operation_state is OperationState.PREPARING
    assert accepted.target_state is None
    assert accepted.can_cancel
    assert not accepted.effects_possible
    assert not accepted.needs_attention
    assert {field.name for field in fields(ExecutionStatus)} == {
        "execution_id",
        "phase",
        "operation_state",
        "target_state",
        "generation",
        "cancellation",
        "can_cancel",
        "effects_possible",
        "created_at",
        "updated_at",
        "settled_at",
        "needs_attention",
    }

    prepared = ledger.prepare(authority, **_snapshots("projection"))
    assert ledger.mark_running(prepared)
    running = projection.status(
        issued.execution_id,
        issued.access_token,
        profile=authority.profile,
        route=authority.route,
        now=102.0,
    )
    assert running is not None
    assert running.phase is ExecutionPhase.RUNNING
    assert running.operation_state is OperationState.RUNNING
    assert running.target_state is TargetState.PENDING
    assert running.effects_possible

    staged = ledger.stage_delivery(
        prepared,
        content="durable response",
        carrier_snapshot={"delivery_type": "log", "profile": "default"},
    )
    delivery = projection.status(
        issued.execution_id,
        issued.access_token,
        profile=authority.profile,
        route=authority.route,
        now=103.0,
    )
    assert delivery is not None
    assert delivery.phase is ExecutionPhase.DELIVERY
    assert delivery.operation_state is OperationState.DELIVERY_READY
    assert delivery.target_state is TargetState.PENDING

    attempt = ledger.begin_target(staged)
    assert attempt.disposition is TargetAttemptDisposition.STARTED
    assert ledger.settle_target(
        attempt,
        Settlement(SettlementKind.CONFIRMED, external_id="delivered"),
    )
    settled = projection.status(
        issued.execution_id,
        issued.access_token,
        profile=authority.profile,
        route=authority.route,
        now=104.0,
    )
    assert settled is not None
    assert settled.phase is ExecutionPhase.SETTLED
    assert settled.operation_state is OperationState.SETTLED
    assert settled.target_state is TargetState.CONFIRMED
    assert not settled.can_cancel
    assert settled.settled_at is not None

    assert (
        projection.status(
            issued.execution_id,
            "wrong-token",
            profile=authority.profile,
            route=authority.route,
            now=105.0,
        )
        is None
    )
    assert (
        projection.status(
            issued.execution_id,
            issued.access_token,
            profile="other-profile",
            route=authority.route,
            now=106.0,
        )
        is None
    )


def test_cancel_request_survives_restart_and_closes_pre_bind_race(
    tmp_path: Path,
) -> None:
    db_path, _, authority, projection, issued = _issue(tmp_path)
    assert issued.access_token is not None

    requested = projection.request_cancel(
        issued.execution_id,
        issued.access_token,
        profile=authority.profile,
        route=authority.route,
        now=101.0,
    )
    assert requested is not None
    assert requested.disposition is CancelDisposition.REQUESTED
    assert requested.status.cancellation is CancellationState.REQUESTED
    assert requested.status.operation_state is OperationState.PREPARING
    assert not projection.claim_cancel(
        authority.operation_id,
        authority.generation + 1,
        now=102.0,
    )

    restarted_ledger = WebhookOperationLedger(db_path, instance_id="new-shard")
    restarted = WebhookExecutionProjection(restarted_ledger)
    assert restarted.claim_cancel(
        authority.operation_id,
        authority.generation,
        now=103.0,
    )
    assert restarted.claim_cancel(
        authority.operation_id,
        authority.generation,
        now=104.0,
    )
    observed = restarted.request_cancel(
        issued.execution_id,
        issued.access_token,
        profile=authority.profile,
        route=authority.route,
        now=105.0,
    )
    assert observed is not None
    assert observed.disposition is CancelDisposition.OBSERVED
    assert observed.status.cancellation is CancellationState.OBSERVED


def test_cancel_is_too_late_after_target_attempt_and_terminal_after_settlement(
    tmp_path: Path,
) -> None:
    _, ledger, authority, projection, issued = _issue(tmp_path)
    assert issued.access_token is not None
    prepared = ledger.prepare(authority, **_snapshots("too-late"))
    assert ledger.mark_running(prepared)
    staged = ledger.stage_delivery(
        prepared,
        content="durable response",
        carrier_snapshot={"delivery_type": "log", "profile": "default"},
    )
    attempt = ledger.begin_target(staged)
    assert attempt.disposition is TargetAttemptDisposition.STARTED

    too_late = projection.request_cancel(
        issued.execution_id,
        issued.access_token,
        profile=authority.profile,
        route=authority.route,
        now=101.0,
    )
    assert too_late is not None
    assert too_late.disposition is CancelDisposition.TOO_LATE
    assert too_late.status.operation_state is OperationState.DELIVERING
    assert too_late.status.cancellation is CancellationState.NONE
    assert not too_late.status.can_cancel
    assert not projection.claim_cancel(
        authority.operation_id,
        authority.generation,
        now=102.0,
    )

    assert ledger.settle_target(
        attempt,
        Settlement(SettlementKind.CONFIRMED, external_id="delivered"),
    )
    terminal = projection.request_cancel(
        issued.execution_id,
        issued.access_token,
        profile=authority.profile,
        route=authority.route,
        now=103.0,
    )
    assert terminal is not None
    assert terminal.disposition is CancelDisposition.TERMINAL
    assert terminal.status.phase is ExecutionPhase.SETTLED


def test_auth_attempt_limit_is_durable_and_shared_by_status_and_cancel(
    tmp_path: Path,
) -> None:
    db_path, _, authority, projection, issued = _issue(
        tmp_path,
        auth_window_seconds=10,
        auth_max_attempts=2,
    )
    assert issued.access_token is not None

    assert (
        projection.status(
            issued.execution_id,
            "wrong-token",
            profile=authority.profile,
            route=authority.route,
            now=101.0,
        )
        is None
    )
    assert (
        projection.request_cancel(
            issued.execution_id,
            issued.access_token,
            profile=authority.profile,
            route="wrong-route",
            now=102.0,
        )
        is None
    )
    assert (
        projection.status(
            issued.execution_id,
            issued.access_token,
            profile=authority.profile,
            route=authority.route,
            now=103.0,
        )
        is None
    )

    restarted = WebhookExecutionProjection(
        WebhookOperationLedger(db_path, instance_id="rate-limit-peer"),
        auth_window_seconds=10,
        auth_max_attempts=2,
    )
    assert (
        restarted.request_cancel(
            issued.execution_id,
            issued.access_token,
            profile=authority.profile,
            route=authority.route,
            now=109.0,
        )
        is None
    )
    after_window = restarted.status(
        issued.execution_id,
        issued.access_token,
        profile=authority.profile,
        route=authority.route,
        now=111.0,
    )
    assert after_window is not None


def test_expiration_prunes_only_capability_and_never_canonical_operation(
    tmp_path: Path,
) -> None:
    _, ledger, authority, projection, issued = _issue(
        tmp_path,
        capability_ttl_seconds=60,
    )
    assert issued.access_token is not None

    assert projection.prune_expired(now=159.0) == 0
    assert projection.prune_expired(now=160.0) == 1
    assert (
        projection.status(
            issued.execution_id,
            issued.access_token,
            profile=authority.profile,
            route=authority.route,
            now=161.0,
        )
        is None
    )
    canonical = ledger.lookup_session(authority.session_key)
    assert canonical is not None
    assert canonical.operation_id == authority.operation_id


def test_incomplete_projection_schema_fails_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    ledger = WebhookOperationLedger(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE webhook_execution_projection_meta (
                   schema_name TEXT PRIMARY KEY,
                   schema_version INTEGER NOT NULL
               )"""
        )
        conn.execute(
            """INSERT INTO webhook_execution_projection_meta
                   (schema_name, schema_version) VALUES (?, ?)""",
            ("webhook_execution_projection", 1),
        )

    with pytest.raises(
        WebhookLedgerCorruptionError,
        match="projection schema is incomplete",
    ):
        WebhookExecutionProjection(ledger)
