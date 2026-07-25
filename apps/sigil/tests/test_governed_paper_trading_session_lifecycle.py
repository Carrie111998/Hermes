from decimal import Decimal

import pytest

from sigil.order_execution import (
    PaperTradingSessionPolicy,
    PaperTradingSessionStatus,
    PaperTradingSessionTransitionError,
    certify_paper_trading_session,
    fail_paper_trading_session,
    pause_paper_trading_session,
    prepare_paper_trading_session,
    record_paper_trading_activity,
    request_paper_trading_session_close,
    resume_paper_trading_session,
    start_paper_trading_session,
)


def _prepared():
    return prepare_paper_trading_session(
        provider="paper",
        account_id="paper-account",
        operator_identity="operator-1",
        prepared_at="2026-07-25T12:00:00Z",
        evidence_references=("config-evidence",),
    )


def test_full_governed_paper_session_lifecycle() -> None:
    prepared = _prepared()
    assert prepared.status is PaperTradingSessionStatus.PREPARED
    assert len(prepared.events) == 1

    active = start_paper_trading_session(
        prepared,
        started_at="2026-07-25T12:01:00Z",
        actor_identity="operator-1",
        evidence_references=("start-approval",),
    )
    active = record_paper_trading_activity(
        active,
        submitted_orders=2,
        reconciled_orders=2,
        open_orders=0,
        gross_notional_delta=Decimal("250"),
        realized_pnl_delta=Decimal("4.25"),
        fees_delta=Decimal("0.50"),
        evidence_references=("execution-package-1",),
    )
    paused = pause_paper_trading_session(
        active,
        paused_at="2026-07-25T12:05:00Z",
        actor_identity="operator-1",
        reason="Operator review",
    )
    resumed = resume_paper_trading_session(
        paused,
        resumed_at="2026-07-25T12:06:00Z",
        actor_identity="operator-1",
    )
    closing = request_paper_trading_session_close(
        resumed,
        requested_at="2026-07-25T12:10:00Z",
        actor_identity="operator-1",
        reason="Scheduled session complete",
    )
    certified = certify_paper_trading_session(
        closing,
        certified_at="2026-07-25T12:11:00Z",
        actor_identity="operator-1",
        evidence_references=("final-reconciliation",),
    )

    assert certified.status is PaperTradingSessionStatus.CERTIFIED
    assert certified.order_count == 2
    assert certified.reconciled_order_count == 2
    assert certified.open_order_count == 0
    assert certified.gross_notional == Decimal("250")
    assert certified.realized_pnl == Decimal("4.25")
    assert certified.fees == Decimal("0.50")
    assert certified.closed_at == "2026-07-25T12:11:00Z"
    assert len(certified.events) == 6


def test_session_identity_is_deterministic() -> None:
    assert _prepared().session_id == _prepared().session_id


@pytest.mark.parametrize("provider", ["live", "alpaca", "public"])
def test_non_paper_provider_is_rejected(provider: str) -> None:
    with pytest.raises(ValueError, match="provider='paper'"):
        prepare_paper_trading_session(
            provider=provider,
            account_id="account",
            operator_identity="operator",
            prepared_at="2026-07-25T12:00:00Z",
        )


def test_invalid_start_transition_is_blocked() -> None:
    active = start_paper_trading_session(
        _prepared(),
        started_at="2026-07-25T12:01:00Z",
        actor_identity="operator-1",
    )
    with pytest.raises(PaperTradingSessionTransitionError):
        start_paper_trading_session(
            active,
            started_at="2026-07-25T12:02:00Z",
            actor_identity="operator-1",
        )


def test_activity_limits_are_enforced() -> None:
    prepared = prepare_paper_trading_session(
        provider="paper",
        account_id="paper-account",
        operator_identity="operator-1",
        prepared_at="2026-07-25T12:00:00Z",
        policy=PaperTradingSessionPolicy(
            max_orders=1,
            max_gross_notional=Decimal("100"),
        ),
    )
    active = start_paper_trading_session(
        prepared,
        started_at="2026-07-25T12:01:00Z",
        actor_identity="operator-1",
    )

    with pytest.raises(
        PaperTradingSessionTransitionError,
        match="maximum order count",
    ):
        record_paper_trading_activity(
            active,
            submitted_orders=2,
            reconciled_orders=0,
            open_orders=2,
            gross_notional_delta=Decimal("50"),
        )

    with pytest.raises(
        PaperTradingSessionTransitionError,
        match="maximum gross notional",
    ):
        record_paper_trading_activity(
            active,
            submitted_orders=1,
            reconciled_orders=0,
            open_orders=1,
            gross_notional_delta=Decimal("101"),
        )


def test_certification_requires_no_open_orders() -> None:
    active = start_paper_trading_session(
        _prepared(),
        started_at="2026-07-25T12:01:00Z",
        actor_identity="operator-1",
    )
    active = record_paper_trading_activity(
        active,
        submitted_orders=1,
        reconciled_orders=0,
        open_orders=1,
        gross_notional_delta=Decimal("50"),
    )
    closing = request_paper_trading_session_close(
        active,
        requested_at="2026-07-25T12:02:00Z",
        actor_identity="operator-1",
        reason="Close requested",
    )

    with pytest.raises(
        PaperTradingSessionTransitionError,
        match="open orders",
    ):
        certify_paper_trading_session(
            closing,
            certified_at="2026-07-25T12:03:00Z",
            actor_identity="operator-1",
        )


def test_certification_requires_complete_reconciliation() -> None:
    active = start_paper_trading_session(
        _prepared(),
        started_at="2026-07-25T12:01:00Z",
        actor_identity="operator-1",
    )
    active = record_paper_trading_activity(
        active,
        submitted_orders=2,
        reconciled_orders=1,
        open_orders=0,
        gross_notional_delta=Decimal("100"),
    )
    closing = request_paper_trading_session_close(
        active,
        requested_at="2026-07-25T12:02:00Z",
        actor_identity="operator-1",
        reason="Close requested",
    )

    with pytest.raises(
        PaperTradingSessionTransitionError,
        match="all orders are reconciled",
    ):
        certify_paper_trading_session(
            closing,
            certified_at="2026-07-25T12:03:00Z",
            actor_identity="operator-1",
        )


def test_failed_session_is_terminal() -> None:
    failed = fail_paper_trading_session(
        _prepared(),
        failed_at="2026-07-25T12:01:00Z",
        actor_identity="operator-1",
        reason="Pre-flight evidence became invalid",
    )
    assert failed.status is PaperTradingSessionStatus.FAILED
    assert failed.failure_reason == "Pre-flight evidence became invalid"

    with pytest.raises(PaperTradingSessionTransitionError):
        fail_paper_trading_session(
            failed,
            failed_at="2026-07-25T12:02:00Z",
            actor_identity="operator-1",
            reason="Second failure",
        )
