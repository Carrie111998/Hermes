from decimal import Decimal

import pytest

from sigil.order_execution import (
    LiveTradingEligibilityPolicy,
    LiveTradingEligibilityRequest,
    LiveTradingEligibilityStatus,
    PaperPromotionReadinessAssessment,
    PaperPromotionReadinessStatus,
    review_live_trading_eligibility,
)


def _readiness(
    status: PaperPromotionReadinessStatus =
        PaperPromotionReadinessStatus.PAPER_READY,
) -> PaperPromotionReadinessAssessment:
    return PaperPromotionReadinessAssessment(
        assessment_id="assessment-1",
        assessed_at="2026-07-25T13:00:00Z",
        assessor_identity="assessor-1",
        status=status,
        evaluation_count=3,
        passed_evaluation_count=3,
        review_evaluation_count=0,
        failed_evaluation_count=0,
        pass_rate=Decimal("1"),
        total_net_pnl=Decimal("30"),
        average_net_pnl=Decimal("10"),
        average_compliance_score=Decimal("1"),
        minimum_compliance_score=Decimal("1"),
        maximum_drawdown=Decimal("3"),
        session_ids=("session-1", "session-2", "session-3"),
        passed_checks=("all",),
        failed_checks=(),
        evidence_references=("readiness-evidence",),
        readiness_reasons=("all promotion readiness checks passed",),
    )


def _request(**overrides) -> LiveTradingEligibilityRequest:
    values = dict(
        request_id="request-1",
        requested_at_epoch=1000,
        operator_identity="operator-1",
        operator_approved=True,
        broker_name="paper-broker",
        account_identifier="account-1",
        credentials_attested=True,
        legal_acknowledgment_reference="legal-ack-1",
        jurisdiction="US-SC",
        proposed_initial_capital=Decimal("25"),
        proposed_maximum_order_notional=Decimal("5"),
        kill_switch_reference="kill-switch-1",
        rollback_plan_reference="rollback-1",
        unresolved_blockers=(),
        evidence_references=("request-evidence",),
    )
    values.update(overrides)
    return LiveTradingEligibilityRequest(**values)


def test_eligible_for_live_certification_when_all_checks_pass() -> None:
    review = review_live_trading_eligibility(
        _readiness(),
        _request(),
        reviewed_at_epoch=1200,
        reviewer_identity="reviewer-1",
        policy=LiveTradingEligibilityPolicy(),
        evidence_references=("review-evidence",),
    )

    assert (
        review.status
        is LiveTradingEligibilityStatus.ELIGIBLE_FOR_LIVE_CERTIFICATION
    )
    assert not review.failed_checks
    assert "readiness-evidence" in review.evidence_references


def test_ineligible_when_paper_readiness_is_not_ready() -> None:
    review = review_live_trading_eligibility(
        _readiness(PaperPromotionReadinessStatus.REVIEW_REQUIRED),
        _request(),
        reviewed_at_epoch=1200,
        reviewer_identity="reviewer-1",
    )

    assert review.status is LiveTradingEligibilityStatus.INELIGIBLE
    assert "paper_ready" in review.failed_checks


def test_conditional_when_capital_limit_is_exceeded() -> None:
    review = review_live_trading_eligibility(
        _readiness(),
        _request(proposed_initial_capital=Decimal("30")),
        reviewed_at_epoch=1200,
        reviewer_identity="reviewer-1",
    )

    assert review.status is LiveTradingEligibilityStatus.CONDITIONAL
    assert "initial_capital_limit" in review.failed_checks


def test_ineligible_when_kill_switch_is_missing() -> None:
    review = review_live_trading_eligibility(
        _readiness(),
        _request(kill_switch_reference=""),
        reviewed_at_epoch=1200,
        reviewer_identity="reviewer-1",
    )

    assert review.status is LiveTradingEligibilityStatus.INELIGIBLE
    assert "kill_switch" in review.failed_checks


def test_ineligible_when_unresolved_blockers_exist() -> None:
    review = review_live_trading_eligibility(
        _readiness(),
        _request(unresolved_blockers=("broker approval pending",)),
        reviewed_at_epoch=1200,
        reviewer_identity="reviewer-1",
    )

    assert review.status is LiveTradingEligibilityStatus.INELIGIBLE
    assert "no_unresolved_blockers" in review.failed_checks


def test_stale_request_is_conditional() -> None:
    review = review_live_trading_eligibility(
        _readiness(),
        _request(requested_at_epoch=0),
        reviewed_at_epoch=100,
        reviewer_identity="reviewer-1",
        policy=LiveTradingEligibilityPolicy(
            maximum_readiness_age_seconds=10,
        ),
    )

    assert review.status is LiveTradingEligibilityStatus.CONDITIONAL
    assert "readiness_current" in review.failed_checks


def test_future_request_is_rejected() -> None:
    with pytest.raises(ValueError, match="request cannot be from the future"):
        review_live_trading_eligibility(
            _readiness(),
            _request(requested_at_epoch=2000),
            reviewed_at_epoch=1000,
            reviewer_identity="reviewer-1",
        )


def test_review_identity_is_deterministic() -> None:
    kwargs = dict(
        readiness=_readiness(),
        request=_request(),
        reviewed_at_epoch=1200,
        reviewer_identity="reviewer-1",
    )
    first = review_live_trading_eligibility(**kwargs)
    second = review_live_trading_eligibility(**kwargs)

    assert first.review_id == second.review_id
