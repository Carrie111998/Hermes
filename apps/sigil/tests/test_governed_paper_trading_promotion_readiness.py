from decimal import Decimal

import pytest

from sigil.order_execution import (
    PaperPerformanceEvaluation,
    PaperPerformanceRecommendation,
    PaperPromotionReadinessPolicy,
    PaperPromotionReadinessStatus,
    assess_paper_trading_promotion_readiness,
)


def _evaluation(
    *,
    number: int,
    recommendation: PaperPerformanceRecommendation = PaperPerformanceRecommendation.PASS,
    net_pnl: Decimal = Decimal("10"),
    compliance_score: Decimal = Decimal("1"),
    evidence: tuple[str, ...] = ("evidence",),
) -> PaperPerformanceEvaluation:
    return PaperPerformanceEvaluation(
        evaluation_id=f"evaluation-{number}",
        session_id=f"session-{number}",
        evaluated_at=f"2026-07-2{number}T12:00:00Z",
        evaluator_identity="evaluator-1",
        recommendation=recommendation,
        trade_count=3,
        winning_trade_count=2,
        losing_trade_count=1,
        breakeven_trade_count=0,
        win_rate=Decimal("0.6666666667"),
        net_pnl=net_pnl,
        gross_profit=Decimal("20"),
        gross_loss=Decimal("10"),
        profit_factor=Decimal("2"),
        maximum_drawdown=Decimal(number),
        ending_equity=Decimal("1010"),
        fees=Decimal("1"),
        compliance_score=compliance_score,
        passed_checks=("session_certified",),
        failed_checks=(),
        evidence_references=evidence,
    )


def test_paper_ready_when_all_requirements_pass() -> None:
    assessment = assess_paper_trading_promotion_readiness(
        (_evaluation(number=1), _evaluation(number=2), _evaluation(number=3)),
        assessed_at="2026-07-25T13:00:00Z",
        assessor_identity="assessor-1",
        policy=PaperPromotionReadinessPolicy(
            minimum_evaluations=3,
            minimum_passed_evaluations=3,
            minimum_pass_rate=Decimal("1"),
            minimum_total_net_pnl=Decimal("25"),
            minimum_average_compliance_score=Decimal("1"),
        ),
        evidence_references=("assessment-evidence",),
    )

    assert assessment.status is PaperPromotionReadinessStatus.PAPER_READY
    assert assessment.evaluation_count == 3
    assert assessment.passed_evaluation_count == 3
    assert assessment.pass_rate == Decimal("1")
    assert assessment.total_net_pnl == Decimal("30")
    assert assessment.average_net_pnl == Decimal("10")
    assert assessment.maximum_drawdown == Decimal("3")
    assert not assessment.failed_checks


def test_review_required_when_history_is_insufficient() -> None:
    assessment = assess_paper_trading_promotion_readiness(
        (_evaluation(number=1), _evaluation(number=2)),
        assessed_at="2026-07-25T13:00:00Z",
        assessor_identity="assessor-1",
        policy=PaperPromotionReadinessPolicy(
            minimum_evaluations=3,
            minimum_passed_evaluations=2,
            minimum_pass_rate=Decimal("1"),
        ),
    )

    assert assessment.status is PaperPromotionReadinessStatus.REVIEW_REQUIRED
    assert "minimum_evaluations" in assessment.failed_checks


def test_not_ready_when_failed_evaluation_exists() -> None:
    assessment = assess_paper_trading_promotion_readiness(
        (
            _evaluation(number=1),
            _evaluation(
                number=2,
                recommendation=PaperPerformanceRecommendation.FAIL,
            ),
            _evaluation(number=3),
        ),
        assessed_at="2026-07-25T13:00:00Z",
        assessor_identity="assessor-1",
        policy=PaperPromotionReadinessPolicy(
            minimum_evaluations=3,
            minimum_passed_evaluations=2,
            minimum_pass_rate=Decimal("0.60"),
            maximum_failed_evaluations=0,
        ),
    )

    assert assessment.status is PaperPromotionReadinessStatus.NOT_READY
    assert "maximum_failed_evaluations" in assessment.failed_checks


def test_not_ready_when_evidence_is_incomplete() -> None:
    assessment = assess_paper_trading_promotion_readiness(
        (
            _evaluation(number=1),
            _evaluation(number=2, evidence=()),
            _evaluation(number=3),
        ),
        assessed_at="2026-07-25T13:00:00Z",
        assessor_identity="assessor-1",
    )

    assert assessment.status is PaperPromotionReadinessStatus.NOT_READY
    assert "complete_evidence" in assessment.failed_checks


def test_duplicate_evaluation_ids_are_rejected() -> None:
    evaluation = _evaluation(number=1)
    with pytest.raises(ValueError, match="evaluation_id values must be unique"):
        assess_paper_trading_promotion_readiness(
            (evaluation, evaluation),
            assessed_at="2026-07-25T13:00:00Z",
            assessor_identity="assessor-1",
        )


def test_duplicate_sessions_fail_readiness_integrity() -> None:
    first = _evaluation(number=1)
    second = PaperPerformanceEvaluation(
        evaluation_id="evaluation-2",
        session_id=first.session_id,
        evaluated_at="2026-07-22T12:00:00Z",
        evaluator_identity="evaluator-1",
        recommendation=PaperPerformanceRecommendation.PASS,
        trade_count=3,
        winning_trade_count=2,
        losing_trade_count=1,
        breakeven_trade_count=0,
        win_rate=Decimal("0.66"),
        net_pnl=Decimal("10"),
        gross_profit=Decimal("20"),
        gross_loss=Decimal("10"),
        profit_factor=Decimal("2"),
        maximum_drawdown=Decimal("2"),
        ending_equity=Decimal("1010"),
        fees=Decimal("1"),
        compliance_score=Decimal("1"),
        passed_checks=("session_certified",),
        failed_checks=(),
        evidence_references=("evidence-2",),
    )

    assessment = assess_paper_trading_promotion_readiness(
        (first, second, _evaluation(number=3)),
        assessed_at="2026-07-25T13:00:00Z",
        assessor_identity="assessor-1",
        policy=PaperPromotionReadinessPolicy(
            minimum_evaluations=3,
            minimum_passed_evaluations=3,
        ),
    )

    assert assessment.status is PaperPromotionReadinessStatus.NOT_READY
    assert "unique_sessions" in assessment.failed_checks


def test_assessment_identity_is_deterministic_and_order_independent() -> None:
    evaluations = (
        _evaluation(number=1),
        _evaluation(number=2),
        _evaluation(number=3),
    )
    first = assess_paper_trading_promotion_readiness(
        evaluations,
        assessed_at="2026-07-25T13:00:00Z",
        assessor_identity="assessor-1",
    )
    second = assess_paper_trading_promotion_readiness(
        tuple(reversed(evaluations)),
        assessed_at="2026-07-25T13:00:00Z",
        assessor_identity="assessor-1",
    )

    assert first.assessment_id == second.assessment_id
