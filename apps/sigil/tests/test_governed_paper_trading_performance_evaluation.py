from decimal import Decimal

import pytest

from sigil.order_execution import (
    PaperPerformancePolicy,
    PaperPerformanceRecommendation,
    PaperTradeOutcome,
    certify_paper_trading_session,
    evaluate_paper_trading_performance,
    prepare_paper_trading_session,
    record_paper_trading_activity,
    request_paper_trading_session_close,
    start_paper_trading_session,
)


def _certified_session(*, pnl: Decimal = Decimal("15")):
    session = prepare_paper_trading_session(
        provider="paper",
        account_id="paper-account",
        operator_identity="operator-1",
        prepared_at="2026-07-25T12:00:00Z",
        evidence_references=("session-config",),
    )
    session = start_paper_trading_session(
        session,
        started_at="2026-07-25T12:01:00Z",
        actor_identity="operator-1",
    )
    session = record_paper_trading_activity(
        session,
        submitted_orders=3,
        reconciled_orders=3,
        open_orders=0,
        gross_notional_delta=Decimal("300"),
        realized_pnl_delta=pnl,
        fees_delta=Decimal("1"),
        evidence_references=("session-execution",),
    )
    session = request_paper_trading_session_close(
        session,
        requested_at="2026-07-25T12:10:00Z",
        actor_identity="operator-1",
        reason="Session complete",
    )
    return certify_paper_trading_session(
        session,
        certified_at="2026-07-25T12:11:00Z",
        actor_identity="operator-1",
        evidence_references=("session-certification",),
    )


def _trades():
    return (
        PaperTradeOutcome(
            trade_id="trade-1",
            realized_pnl=Decimal("20"),
            gross_profit=Decimal("20"),
            evidence_references=("trade-1-evidence",),
        ),
        PaperTradeOutcome(
            trade_id="trade-2",
            realized_pnl=Decimal("-10"),
            gross_loss=Decimal("10"),
            evidence_references=("trade-2-evidence",),
        ),
        PaperTradeOutcome(
            trade_id="trade-3",
            realized_pnl=Decimal("5"),
            gross_profit=Decimal("5"),
            evidence_references=("trade-3-evidence",),
        ),
    )


def test_passes_compliant_certified_session() -> None:
    result = evaluate_paper_trading_performance(
        _certified_session(),
        trades=_trades(),
        equity_curve=(
            Decimal("1000"),
            Decimal("1020"),
            Decimal("1010"),
            Decimal("1015"),
        ),
        evaluated_at="2026-07-25T12:12:00Z",
        evaluator_identity="evaluator-1",
        policy=PaperPerformancePolicy(
            minimum_trades=3,
            minimum_net_pnl=Decimal("10"),
            minimum_win_rate=Decimal("0.60"),
            minimum_profit_factor=Decimal("2"),
            maximum_drawdown=Decimal("15"),
            maximum_fees=Decimal("5"),
        ),
        evidence_references=("evaluation-evidence",),
    )

    assert result.recommendation is PaperPerformanceRecommendation.PASS
    assert result.trade_count == 3
    assert result.winning_trade_count == 2
    assert result.losing_trade_count == 1
    assert result.win_rate == Decimal(2) / Decimal(3)
    assert result.net_pnl == Decimal("15")
    assert result.gross_profit == Decimal("25")
    assert result.gross_loss == Decimal("10")
    assert result.profit_factor == Decimal("2.5")
    assert result.maximum_drawdown == Decimal("10")
    assert result.ending_equity == Decimal("1015")
    assert result.compliance_score == Decimal("1")
    assert not result.failed_checks


def test_review_when_only_performance_thresholds_fail() -> None:
    result = evaluate_paper_trading_performance(
        _certified_session(),
        trades=_trades(),
        equity_curve=(
            Decimal("1000"),
            Decimal("1020"),
            Decimal("900"),
            Decimal("1015"),
        ),
        evaluated_at="2026-07-25T12:12:00Z",
        evaluator_identity="evaluator-1",
        policy=PaperPerformancePolicy(
            minimum_trades=5,
            minimum_net_pnl=Decimal("20"),
            minimum_win_rate=Decimal("0.90"),
            minimum_profit_factor=Decimal("3"),
            maximum_drawdown=Decimal("50"),
        ),
    )

    assert result.recommendation is PaperPerformanceRecommendation.REVIEW
    assert "minimum_trades" in result.failed_checks
    assert "maximum_drawdown" in result.failed_checks


def test_fail_when_session_pnl_does_not_match_trades() -> None:
    result = evaluate_paper_trading_performance(
        _certified_session(pnl=Decimal("99")),
        trades=_trades(),
        equity_curve=(Decimal("1000"), Decimal("1015")),
        evaluated_at="2026-07-25T12:12:00Z",
        evaluator_identity="evaluator-1",
    )

    assert result.recommendation is PaperPerformanceRecommendation.FAIL
    assert "session_pnl_matches_trades" in result.failed_checks


def test_profit_factor_is_none_without_losses() -> None:
    result = evaluate_paper_trading_performance(
        _certified_session(pnl=Decimal("15")),
        trades=(
            PaperTradeOutcome(
                trade_id="trade-1",
                realized_pnl=Decimal("15"),
                gross_profit=Decimal("15"),
                evidence_references=("trade-evidence",),
            ),
        ),
        equity_curve=(Decimal("1000"), Decimal("1015")),
        evaluated_at="2026-07-25T12:12:00Z",
        evaluator_identity="evaluator-1",
        policy=PaperPerformancePolicy(minimum_profit_factor=Decimal("0")),
    )

    assert result.profit_factor is None


def test_duplicate_trade_ids_are_rejected() -> None:
    trade = PaperTradeOutcome(
        trade_id="duplicate",
        realized_pnl=Decimal("1"),
    )
    with pytest.raises(ValueError, match="trade_id values must be unique"):
        evaluate_paper_trading_performance(
            _certified_session(pnl=Decimal("2")),
            trades=(trade, trade),
            equity_curve=(Decimal("1000"), Decimal("1002")),
            evaluated_at="2026-07-25T12:12:00Z",
            evaluator_identity="evaluator-1",
        )


def test_empty_equity_curve_is_rejected() -> None:
    with pytest.raises(ValueError, match="equity_curve must not be empty"):
        evaluate_paper_trading_performance(
            _certified_session(),
            trades=_trades(),
            equity_curve=(),
            evaluated_at="2026-07-25T12:12:00Z",
            evaluator_identity="evaluator-1",
        )


def test_evaluation_identity_is_deterministic() -> None:
    kwargs = dict(
        session=_certified_session(),
        trades=_trades(),
        equity_curve=(Decimal("1000"), Decimal("1015")),
        evaluated_at="2026-07-25T12:12:00Z",
        evaluator_identity="evaluator-1",
    )
    first = evaluate_paper_trading_performance(**kwargs)
    second = evaluate_paper_trading_performance(**kwargs)
    assert first.evaluation_id == second.evaluation_id
