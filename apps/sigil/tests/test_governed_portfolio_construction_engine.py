from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from sigil.portfolio_construction import (
    AllocationMethod,
    CandidateAsset,
    ConstructionPolicy,
    ConstructionStatus,
    compare_packages,
    construct_portfolio,
    verify_package_identity,
)


def asset(
    symbol: str,
    score: str,
    *,
    issuer: str | None = None,
    sector: str = "Technology",
    liquidity: str = "0.90",
    volatility: str = "0.25",
    approved: bool = True,
    references: bool = True,
) -> CandidateAsset:
    return CandidateAsset(
        symbol=symbol,
        score=Decimal(score),
        price=Decimal("100"),
        liquidity_score=Decimal(liquidity),
        volatility=Decimal(volatility),
        issuer=issuer or symbol,
        sector=sector,
        approved=approved,
        thesis_reference=f"thesis:{symbol}" if references else None,
        valuation_reference=f"valuation:{symbol}" if references else None,
        risk_reference=f"risk:{symbol}" if references else None,
    )


def permissive_policy(**changes: object) -> ConstructionPolicy:
    defaults: dict[str, object] = {
        "min_positions": 1,
        "max_positions": 20,
        "max_position_weight": Decimal("1"),
        "max_issuer_weight": Decimal("1"),
        "max_sector_weight": Decimal("1"),
        "max_gross_exposure": Decimal("1"),
        "min_liquidity_score": Decimal("0"),
        "max_asset_volatility": Decimal("10"),
        "require_evidence_references": True,
    }
    defaults.update(changes)
    return ConstructionPolicy(**defaults)


def test_equal_weight_is_deterministic() -> None:
    candidates = [asset("BBB", "1"), asset("AAA", "1")]
    package = construct_portfolio(
        candidates,
        capital=Decimal("10000"),
        method=AllocationMethod.EQUAL_WEIGHT,
        policy=permissive_policy(),
    )
    assert package.status is ConstructionStatus.READY
    assert [position.symbol for position in package.positions] == ["AAA", "BBB"]
    assert [position.target_weight for position in package.positions] == [
        Decimal("0.500000"),
        Decimal("0.500000"),
    ]
    assert package.execution_authority is False
    assert package.analytical_only is True


def test_score_weighted_allocation() -> None:
    package = construct_portfolio(
        [asset("AAA", "3"), asset("BBB", "1")],
        capital=Decimal("1000"),
        method=AllocationMethod.SCORE_WEIGHTED,
        policy=permissive_policy(),
    )
    weights = {position.symbol: position.target_weight for position in package.positions}
    assert weights == {
        "AAA": Decimal("0.750000"),
        "BBB": Decimal("0.250000"),
    }


def test_low_liquidity_candidate_is_excluded() -> None:
    package = construct_portfolio(
        [asset("AAA", "2", liquidity="0.20"), asset("BBB", "1")],
        capital=Decimal("1000"),
        method=AllocationMethod.EQUAL_WEIGHT,
        policy=permissive_policy(min_liquidity_score=Decimal("0.50")),
    )
    assert [position.symbol for position in package.positions] == ["BBB"]
    assert package.exclusions[0].symbol == "AAA"
    assert "liquidity score is below policy minimum" in package.exclusions[0].reasons


def test_unapproved_candidate_is_excluded() -> None:
    package = construct_portfolio(
        [asset("AAA", "2", approved=False), asset("BBB", "1")],
        capital=Decimal("1000"),
        method=AllocationMethod.EQUAL_WEIGHT,
        policy=permissive_policy(),
    )
    assert [position.symbol for position in package.positions] == ["BBB"]


def test_missing_evidence_is_excluded() -> None:
    package = construct_portfolio(
        [asset("AAA", "1", references=False)],
        capital=Decimal("1000"),
        method=AllocationMethod.EQUAL_WEIGHT,
        policy=permissive_policy(),
    )
    assert package.status is ConstructionStatus.BLOCKED
    assert len(package.exclusions[0].reasons) == 3


def test_duplicate_symbols_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate candidate symbol"):
        construct_portfolio(
            [asset("AAA", "1"), asset("AAA", "2")],
            capital=Decimal("1000"),
            method=AllocationMethod.EQUAL_WEIGHT,
            policy=permissive_policy(),
        )


def test_nonpositive_capital_is_rejected() -> None:
    with pytest.raises(ValueError, match="capital must be positive"):
        construct_portfolio(
            [asset("AAA", "1")],
            capital=Decimal("0"),
            method=AllocationMethod.EQUAL_WEIGHT,
            policy=permissive_policy(),
        )


def test_constrained_method_enforces_position_cap() -> None:
    package = construct_portfolio(
        [asset("AAA", "9"), asset("BBB", "1")],
        capital=Decimal("1000"),
        method=AllocationMethod.CONSTRAINED_SCORE_WEIGHTED,
        policy=permissive_policy(max_position_weight=Decimal("0.60")),
    )
    weights = {position.symbol: position.target_weight for position in package.positions}
    assert weights["AAA"] == Decimal("0.600000")
    assert all(result.passed for result in package.constraints)


def test_constrained_method_enforces_sector_cap() -> None:
    package = construct_portfolio(
        [
            asset("AAA", "3", sector="Technology"),
            asset("BBB", "2", sector="Technology"),
            asset("CCC", "1", sector="Healthcare"),
        ],
        capital=Decimal("1000"),
        method=AllocationMethod.CONSTRAINED_SCORE_WEIGHTED,
        policy=permissive_policy(max_sector_weight=Decimal("0.50")),
    )
    technology = sum(
        position.target_weight
        for position in package.positions
        if position.sector == "Technology"
    )
    assert technology <= Decimal("0.50")


def test_constrained_method_enforces_issuer_cap() -> None:
    package = construct_portfolio(
        [
            asset("AAA", "3", issuer="Shared"),
            asset("BBB", "2", issuer="Shared"),
            asset("CCC", "1", issuer="Other"),
        ],
        capital=Decimal("1000"),
        method=AllocationMethod.CONSTRAINED_SCORE_WEIGHTED,
        policy=permissive_policy(max_issuer_weight=Decimal("0.40")),
    )
    shared = sum(
        position.target_weight
        for position in package.positions
        if position.issuer == "Shared"
    )
    assert shared <= Decimal("0.40")


def test_cash_reserve_is_preserved() -> None:
    package = construct_portfolio(
        [asset("AAA", "1"), asset("BBB", "1")],
        capital=Decimal("1000"),
        method=AllocationMethod.EQUAL_WEIGHT,
        policy=permissive_policy(cash_reserve_weight=Decimal("0.20")),
    )
    assert package.invested_weight == Decimal("0.800000")
    assert package.cash_weight == Decimal("0.200000")


def test_minimum_position_count_blocks_package() -> None:
    package = construct_portfolio(
        [asset("AAA", "1")],
        capital=Decimal("1000"),
        method=AllocationMethod.EQUAL_WEIGHT,
        policy=permissive_policy(min_positions=2),
    )
    assert package.status is ConstructionStatus.BLOCKED
    assert "position_count" in package.blockers


def test_package_identity_is_deterministic() -> None:
    kwargs = {
        "candidates": [asset("AAA", "2"), asset("BBB", "1")],
        "capital": Decimal("1000"),
        "method": AllocationMethod.SCORE_WEIGHTED,
        "policy": permissive_policy(),
    }
    first = construct_portfolio(**kwargs)
    second = construct_portfolio(**kwargs)
    assert first.package_id == second.package_id


def test_package_identity_detects_tampering() -> None:
    package = construct_portfolio(
        [asset("AAA", "1")],
        capital=Decimal("1000"),
        method=AllocationMethod.EQUAL_WEIGHT,
        policy=permissive_policy(),
    )
    unsigned = replace(package, package_id="")
    assert verify_package_identity(unsigned, package.package_id)
    tampered = replace(unsigned, capital=Decimal("2000"))
    assert not verify_package_identity(tampered, package.package_id)


def test_comparison_reports_additions_removals_and_changes() -> None:
    left = construct_portfolio(
        [asset("AAA", "1"), asset("BBB", "1")],
        capital=Decimal("1000"),
        method=AllocationMethod.EQUAL_WEIGHT,
        policy=permissive_policy(),
    )
    right = construct_portfolio(
        [asset("AAA", "3"), asset("CCC", "1")],
        capital=Decimal("1000"),
        method=AllocationMethod.SCORE_WEIGHTED,
        policy=permissive_policy(),
    )
    comparison = compare_packages(left, right)
    assert comparison.added_symbols == ("CCC",)
    assert comparison.removed_symbols == ("BBB",)
    assert comparison.weight_changes["AAA"] == Decimal("0.250000")


def test_evidence_references_are_deduplicated_and_sorted() -> None:
    package = construct_portfolio(
        [asset("BBB", "1"), asset("AAA", "1")],
        capital=Decimal("1000"),
        method=AllocationMethod.EQUAL_WEIGHT,
        policy=permissive_policy(),
    )
    assert package.evidence_references == tuple(sorted(package.evidence_references))
    assert len(package.evidence_references) == 6


def test_target_values_and_shares_are_proposals() -> None:
    package = construct_portfolio(
        [asset("AAA", "1")],
        capital=Decimal("1000"),
        method=AllocationMethod.EQUAL_WEIGHT,
        policy=permissive_policy(),
    )
    position = package.positions[0]
    assert position.target_value == Decimal("1000.00")
    assert position.estimated_shares == Decimal("10.000000")
    assert "not an execution instruction" in position.rationale[-1]


def test_max_positions_selects_highest_scores() -> None:
    package = construct_portfolio(
        [asset("AAA", "1"), asset("BBB", "3"), asset("CCC", "2")],
        capital=Decimal("1000"),
        method=AllocationMethod.EQUAL_WEIGHT,
        policy=permissive_policy(max_positions=2),
    )
    assert {position.symbol for position in package.positions} == {"BBB", "CCC"}


def test_invalid_policy_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_positions"):
        ConstructionPolicy(min_positions=3, max_positions=2)


def test_invalid_candidate_is_rejected() -> None:
    with pytest.raises(ValueError, match="price must be positive"):
        CandidateAsset(
            symbol="AAA",
            score=Decimal("1"),
            price=Decimal("0"),
            liquidity_score=Decimal("1"),
            volatility=Decimal("0.1"),
            issuer="AAA",
            sector="Technology",
        )
