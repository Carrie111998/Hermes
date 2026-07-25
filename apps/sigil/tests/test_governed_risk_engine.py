from dataclasses import FrozenInstanceError

import pytest

from sigil.risk_governance import (
    GovernedRiskRequest,
    PositionSide,
    RiskDisposition,
    RiskMetricKind,
    RiskPolicy,
    RiskPosition,
    RiskSeverity,
    RiskValidationError,
    breached_metric_ids,
    compare_risk_packages,
    construct_governed_risk_package,
    package_is_analytical_only,
    verify_package_identity,
)


def policy(**changes: object) -> RiskPolicy:
    values = {
        "policy_id": "risk-policy-v1",
        "max_gross_exposure": "1.50",
        "max_absolute_net_exposure": "1.00",
        "max_leverage": "1.50",
        "max_position_concentration": "0.20",
        "max_issuer_concentration": "0.25",
        "max_sector_concentration": "0.40",
        "max_days_to_liquidate": "5.00",
        "max_weighted_volatility": "0.45",
        "max_weighted_drawdown": "0.35",
    }
    values.update(changes)
    return RiskPolicy(**values)


def position(
    *,
    position_id: str = "p1",
    instrument_id: str = "AAPL",
    issuer_id: str = "apple",
    sector_id: str = "technology",
    side: PositionSide = PositionSide.LONG,
    market_value: str = "10000",
    adv: str = "1000000",
    volatility: str = "0.20",
    drawdown: str = "0.15",
    evidence: tuple[str, ...] = ("evidence://position/p1",),
) -> RiskPosition:
    return RiskPosition(
        position_id=position_id,
        instrument_id=instrument_id,
        issuer_id=issuer_id,
        sector_id=sector_id,
        side=side,
        market_value=market_value,
        average_daily_volume_value=adv,
        annualized_volatility=volatility,
        peak_to_trough_drawdown=drawdown,
        evidence_references=evidence,
        source_id="portfolio-ledger",
    )


def request(
    governed_policy: RiskPolicy,
    *positions: RiskPosition,
    equity: str = "100000",
) -> GovernedRiskRequest:
    return GovernedRiskRequest(
        request_id="risk-request-1",
        portfolio_id="portfolio-1",
        as_of="2026-07-24T22:00:00Z",
        equity_value=equity,
        positions=positions or (position(),),
        policy_identity=governed_policy.policy_identity,
        upstream_package_identities=("valuation-package-1",),
    )


def test_low_risk_package_is_acceptable() -> None:
    governed_policy = policy()
    package = construct_governed_risk_package(
        request(governed_policy, position()),
        governed_policy,
    )
    assert package.disposition is RiskDisposition.ACCEPTABLE
    assert not package.breached_limits
    assert package.severity in {RiskSeverity.LOW, RiskSeverity.MODERATE}


def test_large_position_requires_review_or_block() -> None:
    governed_policy = policy()
    package = construct_governed_risk_package(
        request(
            governed_policy,
            position(market_value="80000", adv="1000"),
        ),
        governed_policy,
    )
    assert package.disposition is RiskDisposition.BLOCKED
    assert package.breached_limits
    assert package.readiness_blockers


def test_gross_and_leverage_metrics_exist() -> None:
    governed_policy = policy()
    package = construct_governed_risk_package(
        request(
            governed_policy,
            position(position_id="long", market_value="50000"),
            position(
                position_id="short",
                instrument_id="TSLA",
                issuer_id="tesla",
                side=PositionSide.SHORT,
                market_value="25000",
            ),
        ),
        governed_policy,
    )
    kinds = {metric.kind for metric in package.metrics}
    assert RiskMetricKind.GROSS_EXPOSURE in kinds
    assert RiskMetricKind.NET_EXPOSURE in kinds
    assert RiskMetricKind.LEVERAGE in kinds
    assert RiskMetricKind.SHORT_EXPOSURE in kinds


def test_issuer_concentration_aggregates_positions() -> None:
    governed_policy = policy(max_issuer_concentration="0.15")
    package = construct_governed_risk_package(
        request(
            governed_policy,
            position(position_id="p1", market_value="10000"),
            position(
                position_id="p2",
                instrument_id="AAPL2",
                market_value="10000",
            ),
        ),
        governed_policy,
    )
    assert "issuer_concentration:apple" in package.breached_limits


def test_sector_concentration_aggregates_positions() -> None:
    governed_policy = policy(max_sector_concentration="0.15")
    package = construct_governed_risk_package(
        request(
            governed_policy,
            position(position_id="p1", market_value="10000"),
            position(
                position_id="p2",
                instrument_id="MSFT",
                issuer_id="microsoft",
                market_value="10000",
            ),
        ),
        governed_policy,
    )
    assert "sector_concentration:technology" in package.breached_limits


def test_missing_evidence_requires_review() -> None:
    governed_policy = policy(block_on_critical=False)
    package = construct_governed_risk_package(
        request(governed_policy, position(evidence=())),
        governed_policy,
    )
    assert package.disposition is RiskDisposition.REVIEW_REQUIRED
    assert "missing evidence: p1" in package.review_reasons


def test_policy_identity_mismatch_rejected() -> None:
    governed_policy = policy()
    other = policy(policy_id="other")
    with pytest.raises(RiskValidationError):
        construct_governed_risk_package(
            request(governed_policy, position()),
            other,
        )


def test_equity_must_be_positive() -> None:
    governed_policy = policy()
    with pytest.raises(RiskValidationError):
        construct_governed_risk_package(
            request(governed_policy, position(), equity="0"),
            governed_policy,
        )


def test_input_order_does_not_change_identity() -> None:
    governed_policy = policy()
    p1 = position(position_id="a")
    p2 = position(
        position_id="b",
        instrument_id="MSFT",
        issuer_id="microsoft",
    )
    first = construct_governed_risk_package(
        request(governed_policy, p1, p2),
        governed_policy,
    )
    second = construct_governed_risk_package(
        request(governed_policy, p2, p1),
        governed_policy,
    )
    assert first.package_identity == second.package_identity


def test_package_identity_verifies() -> None:
    governed_policy = policy()
    package = construct_governed_risk_package(
        request(governed_policy, position()),
        governed_policy,
    )
    assert verify_package_identity(package)


def test_tampering_breaks_identity_verification() -> None:
    governed_policy = policy()
    package = construct_governed_risk_package(
        request(governed_policy, position()),
        governed_policy,
    )
    object.__setattr__(package, "risk_score", 99)
    assert not verify_package_identity(package)


def test_package_is_analytical_only() -> None:
    governed_policy = policy()
    package = construct_governed_risk_package(
        request(governed_policy, position()),
        governed_policy,
    )
    assert package_is_analytical_only(package)
    assert not package.authorizes_trading
    assert not package.authorizes_capital_allocation
    assert not package.mutates_positions
    assert not package.submits_orders


def test_breached_metric_ids_match_package() -> None:
    governed_policy = policy()
    package = construct_governed_risk_package(
        request(governed_policy, position(market_value="90000", adv="100")),
        governed_policy,
    )
    assert breached_metric_ids(package) == package.breached_limits


def test_comparison_detects_added_and_resolved_breaches() -> None:
    governed_policy = policy()
    safe = construct_governed_risk_package(
        request(governed_policy, position()),
        governed_policy,
    )
    risky = construct_governed_risk_package(
        request(
            governed_policy,
            position(market_value="90000", adv="100"),
        ),
        governed_policy,
    )
    worsening = compare_risk_packages(safe, risky)
    improving = compare_risk_packages(risky, safe)
    assert worsening.added_breaches
    assert improving.resolved_breaches


def test_models_are_frozen() -> None:
    item = position()
    with pytest.raises(FrozenInstanceError):
        item.market_value = "1"  # type: ignore[misc]


def test_duplicate_evidence_is_normalized() -> None:
    item = position(evidence=("b", "a", "b"))
    assert item.evidence_references == ("a", "b")


def test_request_requires_positions() -> None:
    governed_policy = policy()
    with pytest.raises(RiskValidationError):
        GovernedRiskRequest(
            request_id="r",
            portfolio_id="p",
            as_of="now",
            equity_value="100",
            positions=(),
            policy_identity=governed_policy.policy_identity,
        )


def test_policy_rejects_negative_limits() -> None:
    with pytest.raises(RiskValidationError):
        policy(max_leverage="-1")


def test_invalid_position_side_rejected() -> None:
    with pytest.raises(RiskValidationError):
        RiskPosition(
            position_id="p",
            instrument_id="AAPL",
            issuer_id="apple",
            sector_id="technology",
            side="long",  # type: ignore[arg-type]
            market_value="1",
            average_daily_volume_value="1",
            annualized_volatility="0.1",
            peak_to_trough_drawdown="0.1",
            evidence_references=("e",),
            source_id="s",
        )


def test_zero_adv_becomes_critical_liquidity_risk() -> None:
    governed_policy = policy()
    package = construct_governed_risk_package(
        request(governed_policy, position(adv="0")),
        governed_policy,
    )
    liquidity = [
        metric
        for metric in package.metrics
        if metric.kind is RiskMetricKind.LIQUIDITY
    ]
    assert liquidity[0].severity is RiskSeverity.CRITICAL
    assert liquidity[0].breached
