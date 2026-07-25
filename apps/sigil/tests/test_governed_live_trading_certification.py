from decimal import Decimal

import pytest

from sigil.order_execution import (
    LiveTradingCertificationPolicy,
    LiveTradingCertificationRequest,
    LiveTradingCertificationStatus,
    LiveTradingEligibilityReview,
    LiveTradingEligibilityStatus,
    certify_live_trading,
    effective_certification_status,
    revoke_live_trading_certification,
)


def _eligibility(
    status: LiveTradingEligibilityStatus = (
        LiveTradingEligibilityStatus.ELIGIBLE_FOR_LIVE_CERTIFICATION
    ),
) -> LiveTradingEligibilityReview:
    return LiveTradingEligibilityReview(
        review_id="review-1",
        reviewed_at_epoch=1000,
        reviewer_identity="reviewer-1",
        status=status,
        readiness_assessment_id="readiness-1",
        request_id="eligibility-request-1",
        passed_checks=("all",),
        failed_checks=(),
        evidence_references=("eligibility-evidence",),
        conditions=(),
    )


def _request(**overrides) -> LiveTradingCertificationRequest:
    values = dict(
        request_id="cert-request-1",
        broker_name="public",
        account_identifier="account-1",
        asset_classes=("equity",),
        order_types=("limit",),
        symbols=("AAPL",),
        initial_capital=Decimal("25"),
        maximum_order_notional=Decimal("5"),
        valid_from_epoch=1100,
        valid_until_epoch=1200,
        kill_switch_reference="kill-switch-1",
        rollback_plan_reference="rollback-1",
        evidence_references=("request-evidence",),
    )
    values.update(overrides)
    return LiveTradingCertificationRequest(**values)


def test_certifies_when_all_checks_pass() -> None:
    certification = certify_live_trading(
        _eligibility(),
        _request(),
        certifier_identity="owner-1",
        evidence_references=("certifier-evidence",),
    )

    assert certification.status is LiveTradingCertificationStatus.CERTIFIED
    assert not certification.failed_checks
    assert certification.symbols == ("AAPL",)


def test_denies_when_eligibility_is_not_approved() -> None:
    certification = certify_live_trading(
        _eligibility(LiveTradingEligibilityStatus.CONDITIONAL),
        _request(),
        certifier_identity="owner-1",
    )

    assert certification.status is LiveTradingCertificationStatus.DENIED
    assert "eligible_for_live_certification" in certification.failed_checks


def test_denies_when_capital_limit_is_exceeded() -> None:
    certification = certify_live_trading(
        _eligibility(),
        _request(initial_capital=Decimal("30")),
        certifier_identity="owner-1",
    )

    assert certification.status is LiveTradingCertificationStatus.DENIED
    assert "initial_capital_limit" in certification.failed_checks


def test_denies_disallowed_order_type() -> None:
    certification = certify_live_trading(
        _eligibility(),
        _request(order_types=("stop",)),
        certifier_identity="owner-1",
        policy=LiveTradingCertificationPolicy(
            allowed_order_types=("limit", "market"),
        ),
    )

    assert certification.status is LiveTradingCertificationStatus.DENIED
    assert "order_types_allowed" in certification.failed_checks


def test_expiration_is_effective_after_validity_window() -> None:
    certification = certify_live_trading(
        _eligibility(),
        _request(),
        certifier_identity="owner-1",
    )

    assert (
        effective_certification_status(certification, at_epoch=1200)
        is LiveTradingCertificationStatus.EXPIRED
    )


def test_certification_can_be_revoked() -> None:
    certification = certify_live_trading(
        _eligibility(),
        _request(),
        certifier_identity="owner-1",
    )
    revoked = revoke_live_trading_certification(
        certification,
        revoked_at_epoch=1150,
        reason="operator emergency stop",
    )

    assert revoked.status is LiveTradingCertificationStatus.REVOKED
    assert revoked.revocation_reason == "operator emergency stop"


def test_denied_certification_cannot_be_revoked() -> None:
    denied = certify_live_trading(
        _eligibility(LiveTradingEligibilityStatus.INELIGIBLE),
        _request(),
        certifier_identity="owner-1",
    )

    with pytest.raises(
        ValueError,
        match="only certified certifications may be revoked",
    ):
        revoke_live_trading_certification(
            denied,
            revoked_at_epoch=1150,
            reason="not applicable",
        )


def test_certification_id_is_deterministic() -> None:
    kwargs = dict(
        eligibility=_eligibility(),
        request=_request(),
        certifier_identity="owner-1",
    )
    first = certify_live_trading(**kwargs)
    second = certify_live_trading(**kwargs)

    assert first.certification_id == second.certification_id
