from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from sigil.portfolio_rebalancing import (
    CurrentPosition,
    RebalanceAction,
    RebalancingPolicy,
    RebalanceStatus,
    TargetPosition,
    build_rebalance_package,
    compare_rebalance_packages,
    verify_package_identity,
)


def current(
    symbol: str,
    quantity: str,
    *,
    price: str = "100",
    issuer: str | None = None,
    sector: str = "Technology",
) -> CurrentPosition:
    return CurrentPosition(
        symbol=symbol,
        quantity=Decimal(quantity),
        price=Decimal(price),
        issuer=issuer or symbol,
        sector=sector,
    )


def target(
    symbol: str,
    weight: str,
    *,
    price: str = "100",
    issuer: str | None = None,
    sector: str = "Technology",
) -> TargetPosition:
    return TargetPosition(
        symbol=symbol,
        target_weight=Decimal(weight),
        reference_price=Decimal(price),
        issuer=issuer or symbol,
        sector=sector,
    )


def policy(**changes: object) -> RebalancingPolicy:
    defaults: dict[str, object] = {
        "max_turnover_weight": Decimal("1"),
        "minimum_trade_value": Decimal("0"),
        "drift_tolerance": Decimal("0"),
        "max_single_trade_weight": Decimal("1"),
    }
    defaults.update(changes)
    return RebalancingPolicy(**defaults)


def build(
    currents: list[CurrentPosition],
    targets: list[TargetPosition],
    **kwargs: object,
):
    return build_rebalance_package(
        currents,
        targets,
        source_target_package_id="pcp-test",
        target_package_approved=True,
        policy=policy(),
        **kwargs,
    )


def test_buy_proposal_for_underweight_position() -> None:
    package = build(
        [current("AAA", "4"), current("BBB", "6")],
        [target("AAA", "0.60"), target("BBB", "0.40")],
    )
    proposal = {item.symbol: item for item in package.proposals}["AAA"]
    assert proposal.action is RebalanceAction.BUY
    assert proposal.proposed_weight == Decimal("0.200000")
    assert proposal.proposed_value == Decimal("200.00")
    assert proposal.proposed_quantity == Decimal("2.000000")


def test_sell_proposal_for_overweight_position() -> None:
    package = build(
        [current("AAA", "8"), current("BBB", "2")],
        [target("AAA", "0.50"), target("BBB", "0.50")],
    )
    proposal = {item.symbol: item for item in package.proposals}["AAA"]
    assert proposal.action is RebalanceAction.SELL
    assert proposal.proposed_weight == Decimal("-0.300000")


def test_new_position_uses_reference_price() -> None:
    package = build(
        [current("AAA", "10")],
        [target("AAA", "0.80"), target("BBB", "0.20", price="50")],
    )
    proposal = {item.symbol: item for item in package.proposals}["BBB"]
    assert proposal.action is RebalanceAction.BUY
    assert proposal.price == Decimal("50")
    assert proposal.proposed_quantity == Decimal("4.000000")


def test_full_exit_generates_sell() -> None:
    package = build(
        [current("AAA", "5"), current("BBB", "5")],
        [target("AAA", "1.00")],
    )
    proposal = {item.symbol: item for item in package.proposals}["BBB"]
    assert proposal.action is RebalanceAction.SELL
    assert proposal.target_weight == Decimal("0.000000")


def test_drift_tolerance_suppresses_small_trade() -> None:
    package = build_rebalance_package(
        [current("AAA", "51"), current("BBB", "49")],
        [target("AAA", "0.50"), target("BBB", "0.50")],
        source_target_package_id="pcp-test",
        target_package_approved=True,
        policy=policy(drift_tolerance=Decimal("0.02")),
    )
    assert package.status is RebalanceStatus.NO_ACTION
    assert not package.proposals


def test_minimum_trade_value_suppresses_small_trade() -> None:
    package = build_rebalance_package(
        [current("AAA", "51"), current("BBB", "49")],
        [target("AAA", "0.50"), target("BBB", "0.50")],
        source_target_package_id="pcp-test",
        target_package_approved=True,
        policy=policy(minimum_trade_value=Decimal("101")),
    )
    assert package.status is RebalanceStatus.NO_ACTION
    assert any("below minimum value" in warning for warning in package.warnings)


def test_single_trade_cap_bounds_proposal() -> None:
    package = build_rebalance_package(
        [current("AAA", "10")],
        [target("AAA", "0.20"), target("BBB", "0.80")],
        source_target_package_id="pcp-test",
        target_package_approved=True,
        policy=policy(max_single_trade_weight=Decimal("0.10")),
    )
    assert all(
        abs(proposal.proposed_weight) <= Decimal("0.10")
        for proposal in package.proposals
    )


def test_turnover_limit_blocks_package() -> None:
    package = build_rebalance_package(
        [current("AAA", "10")],
        [target("BBB", "1")],
        source_target_package_id="pcp-test",
        target_package_approved=True,
        policy=policy(
            max_single_trade_weight=Decimal("1"),
            max_turnover_weight=Decimal("0.50"),
        ),
    )
    assert package.status is RebalanceStatus.BLOCKED
    assert "turnover" in package.blockers


def test_unapproved_target_blocks_package() -> None:
    package = build_rebalance_package(
        [current("AAA", "10")],
        [target("AAA", "1")],
        source_target_package_id="pcp-test",
        target_package_approved=False,
        policy=policy(),
    )
    assert package.status is RebalanceStatus.BLOCKED
    assert "target portfolio package is not approved" in package.blockers


def test_new_positions_can_be_disabled() -> None:
    package = build_rebalance_package(
        [current("AAA", "10")],
        [target("AAA", "0.80"), target("BBB", "0.20")],
        source_target_package_id="pcp-test",
        target_package_approved=True,
        policy=policy(allow_new_positions=False),
    )
    assert all(proposal.symbol != "BBB" for proposal in package.proposals)
    assert any("new positions are disabled" in warning for warning in package.warnings)


def test_full_exits_can_be_disabled() -> None:
    package = build_rebalance_package(
        [current("AAA", "5"), current("BBB", "5")],
        [target("AAA", "1")],
        source_target_package_id="pcp-test",
        target_package_approved=True,
        policy=policy(allow_full_exits=False),
    )
    assert all(proposal.symbol != "BBB" for proposal in package.proposals)
    assert any("full exits are disabled" in warning for warning in package.warnings)


def test_cash_is_included_in_portfolio_value() -> None:
    package = build(
        [current("AAA", "5")],
        [target("AAA", "0.50")],
        cash_value=Decimal("500"),
    )
    assert package.portfolio_value == Decimal("1000")
    assert package.status is RebalanceStatus.NO_ACTION


def test_duplicate_current_symbols_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate current symbol"):
        build([current("AAA", "1"), current("AAA", "2")], [target("AAA", "1")])


def test_duplicate_target_symbols_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate target symbol"):
        build([current("AAA", "1")], [target("AAA", "0.5"), target("AAA", "0.5")])


def test_target_weights_above_one_are_rejected() -> None:
    with pytest.raises(ValueError, match="must not exceed 1"):
        build([current("AAA", "1")], [target("AAA", "0.8"), target("BBB", "0.3")])


def test_empty_target_package_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        build_rebalance_package(
            [current("AAA", "1")],
            [target("AAA", "1")],
            source_target_package_id="",
            target_package_approved=True,
        )


def test_zero_portfolio_value_is_rejected() -> None:
    with pytest.raises(ValueError, match="portfolio value must be positive"):
        build_rebalance_package(
            [],
            [],
            source_target_package_id="pcp-test",
            target_package_approved=True,
        )


def test_package_identity_is_deterministic() -> None:
    first = build(
        [current("AAA", "5"), current("BBB", "5")],
        [target("AAA", "0.60"), target("BBB", "0.40")],
    )
    second = build(
        [current("BBB", "5"), current("AAA", "5")],
        [target("BBB", "0.40"), target("AAA", "0.60")],
    )
    assert first.package_id == second.package_id


def test_package_identity_detects_tampering() -> None:
    package = build(
        [current("AAA", "5"), current("BBB", "5")],
        [target("AAA", "0.60"), target("BBB", "0.40")],
    )
    unsigned = replace(package, package_id="")
    assert verify_package_identity(unsigned, package.package_id)
    tampered = replace(unsigned, portfolio_value=Decimal("9999"))
    assert not verify_package_identity(tampered, package.package_id)


def test_comparison_reports_trade_changes() -> None:
    left = build(
        [current("AAA", "5"), current("BBB", "5")],
        [target("AAA", "0.60"), target("BBB", "0.40")],
    )
    right = build(
        [current("AAA", "5"), current("BBB", "5")],
        [target("AAA", "0.50"), target("BBB", "0.30"), target("CCC", "0.20")],
    )
    comparison = compare_rebalance_packages(left, right)
    assert "CCC" in comparison.added_trade_symbols
    assert "AAA" in comparison.removed_trade_symbols


def test_evidence_references_are_sorted_and_deduplicated() -> None:
    package = build_rebalance_package(
        [current("AAA", "5"), current("BBB", "5")],
        [target("AAA", "0.60"), target("BBB", "0.40")],
        source_target_package_id="pcp-test",
        target_package_approved=True,
        policy=policy(),
        evidence_references=("risk:1", "target:1", "risk:1"),
    )
    assert package.evidence_references == ("risk:1", "target:1")


def test_package_has_no_execution_authority() -> None:
    package = build(
        [current("AAA", "5"), current("BBB", "5")],
        [target("AAA", "0.60"), target("BBB", "0.40")],
    )
    assert package.analytical_only is True
    assert package.execution_authority is False
    assert all(
        "requires downstream approval" in proposal.rationale[-1]
        for proposal in package.proposals
    )
