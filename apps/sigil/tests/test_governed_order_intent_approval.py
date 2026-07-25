from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from sigil.order_intent import (
    AccountCapacity,
    ApprovalDecision,
    ApprovalStatus,
    OrderIntentPolicy,
    OrderIntentStatus,
    OrderSide,
    OrderType,
    TimeInForce,
    build_order_intent_package,
    compare_order_intent_packages,
    create_approval_request,
    decide_approval_request,
    expire_approval_request,
    normalize_limit_prices,
    normalize_order_type,
    normalize_time_in_force,
    verify_approval_record_identity,
    verify_approval_request_identity,
    verify_order_intent_identity,
    verify_order_intent_package_identity,
)
from sigil.portfolio_rebalancing.models import (
    RebalanceAction,
    RebalancePackage,
    RebalanceStatus,
    TradeProposal,
)


def _proposal(
    *,
    symbol: str = "AAPL",
    action: RebalanceAction = RebalanceAction.BUY,
    proposed_value: Decimal = Decimal("100.00"),
    proposed_quantity: Decimal = Decimal("0.500000"),
    price: Decimal = Decimal("200.00"),
) -> TradeProposal:
    return TradeProposal(
        symbol=symbol,
        action=action,
        current_weight=Decimal("0.10"),
        target_weight=Decimal("0.20"),
        drift_weight=Decimal("0.10"),
        proposed_weight=Decimal("0.10"),
        proposed_value=proposed_value,
        proposed_quantity=proposed_quantity,
        price=price,
        issuer=f"{symbol} issuer",
        sector="Technology",
        rationale=(
            "current_weight=0.10",
            "target_weight=0.20",
            "drift=0.10",
            "proposal is analytical and requires downstream approval",
        ),
    )


def _rebalance_package(
    *,
    status: RebalanceStatus = RebalanceStatus.READY,
    proposals: tuple[TradeProposal, ...] | None = None,
) -> RebalancePackage:
    return RebalancePackage(
        package_id="rebalance-package-001",
        source_target_package_id="target-package-001",
        status=status,
        portfolio_value=Decimal("1000.00"),
        proposed_turnover_weight=Decimal("0.10"),
        proposals=proposals if proposals is not None else (_proposal(),),
        constraints=(),
        blockers=(
            ("source rebalance blocked",)
            if status is RebalanceStatus.BLOCKED
            else ()
        ),
        warnings=(),
        policy_snapshot={"minimum_trade_value": "5.00"},
        evidence_references=("evidence://rebalance/001",),
    )


def _capacity(
    *,
    buying_power: Decimal = Decimal("1000.00"),
    sellable: dict[str, Decimal] | None = None,
) -> AccountCapacity:
    return AccountCapacity(
        available_buying_power=buying_power,
        sellable_quantities=sellable or {},
    )


def test_builds_ready_buy_order_intent_package() -> None:
    package = build_order_intent_package(
        _rebalance_package(),
        account_capacity=_capacity(),
    )

    assert package.status is OrderIntentStatus.READY_FOR_APPROVAL
    assert len(package.intents) == 1
    assert package.intents[0].symbol == "AAPL"
    assert package.intents[0].side is OrderSide.BUY
    assert package.intents[0].order_type is OrderType.MARKET
    assert package.intents[0].time_in_force is TimeInForce.DAY
    assert package.aggregate_buy_notional == Decimal("100.00")
    assert package.aggregate_sell_notional == Decimal("0")
    assert package.aggregate_turnover == Decimal("100.00")
    assert package.analytical_only is True
    assert package.execution_authority is False
    assert not package.blockers


def test_order_intent_and_package_identities_verify() -> None:
    package = build_order_intent_package(
        _rebalance_package(),
        account_capacity=_capacity(),
    )
    intent = package.intents[0]

    assert verify_order_intent_identity(intent, intent.intent_id)
    assert verify_order_intent_package_identity(
        package,
        package.package_id,
    )

    changed_intent = replace(
        intent,
        quantity=Decimal("0.600000"),
    )
    assert not verify_order_intent_identity(
        changed_intent,
        intent.intent_id,
    )


def test_limit_order_requires_limit_price() -> None:
    package = build_order_intent_package(
        _rebalance_package(),
        account_capacity=_capacity(),
        order_type=OrderType.LIMIT,
    )

    assert package.status is OrderIntentStatus.BLOCKED
    assert not package.intents
    assert "AAPL:limit_price_required" in package.blockers


def test_limit_order_accepts_normalized_limit_price() -> None:
    package = build_order_intent_package(
        _rebalance_package(),
        account_capacity=_capacity(),
        order_type="limit",
        limit_prices={"aapl": Decimal("199.50")},
    )

    assert package.status is OrderIntentStatus.READY_FOR_APPROVAL
    assert package.intents[0].order_type is OrderType.LIMIT
    assert package.intents[0].limit_price == Decimal("199.5000")


def test_insufficient_buying_power_blocks_package() -> None:
    package = build_order_intent_package(
        _rebalance_package(),
        account_capacity=_capacity(
            buying_power=Decimal("50.00"),
        ),
    )

    assert package.status is OrderIntentStatus.BLOCKED
    assert "buying_power" in package.blockers


def test_insufficient_sellable_quantity_blocks_package() -> None:
    sell_proposal = _proposal(
        action=RebalanceAction.SELL,
        proposed_quantity=Decimal("2.000000"),
        proposed_value=Decimal("400.00"),
    )

    package = build_order_intent_package(
        _rebalance_package(proposals=(sell_proposal,)),
        account_capacity=_capacity(
            sellable={"AAPL": Decimal("1.000000")},
        ),
    )

    assert package.status is OrderIntentStatus.BLOCKED
    assert package.intents[0].side is OrderSide.SELL
    assert (
        "AAPL:insufficient_sellable_quantity"
        in package.blockers
    )


def test_sufficient_sellable_quantity_allows_sell() -> None:
    sell_proposal = _proposal(
        action=RebalanceAction.SELL,
        proposed_quantity=Decimal("2.000000"),
        proposed_value=Decimal("400.00"),
    )

    package = build_order_intent_package(
        _rebalance_package(proposals=(sell_proposal,)),
        account_capacity=_capacity(
            sellable={"AAPL": Decimal("2.000000")},
        ),
    )

    assert package.status is OrderIntentStatus.READY_FOR_APPROVAL
    assert package.aggregate_sell_notional == Decimal("400.00")


def test_blocked_rebalance_package_remains_blocked() -> None:
    package = build_order_intent_package(
        _rebalance_package(
            status=RebalanceStatus.BLOCKED,
        ),
        account_capacity=_capacity(),
    )

    assert package.status is OrderIntentStatus.BLOCKED
    assert not package.intents
    assert (
        "source_rebalance_package_blocked"
        in package.blockers
    )


def test_no_action_rebalance_package_remains_no_action() -> None:
    package = build_order_intent_package(
        _rebalance_package(
            status=RebalanceStatus.NO_ACTION,
            proposals=(),
        ),
        account_capacity=_capacity(),
    )

    assert package.status is OrderIntentStatus.NO_ACTION
    assert not package.intents
    assert (
        "source_rebalance_package_has_no_action"
        in package.warnings
    )


def test_source_execution_authority_is_rejected() -> None:
    source = replace(
        _rebalance_package(),
        execution_authority=True,
    )

    with pytest.raises(
        ValueError,
        match="must not have execution authority",
    ):
        build_order_intent_package(
            source,
            account_capacity=_capacity(),
        )


def test_approval_request_and_approval_record() -> None:
    package = build_order_intent_package(
        _rebalance_package(),
        account_capacity=_capacity(),
    )

    request = create_approval_request(
        package,
        created_at="2026-07-25T20:00:00+00:00",
        expires_at="2026-07-26T20:00:00+00:00",
    )

    assert request.status is ApprovalStatus.PENDING
    assert request.order_intent_package_id == package.package_id
    assert verify_approval_request_identity(
        request,
        request.request_id,
    )

    record = decide_approval_request(
        request,
        decision=ApprovalDecision.APPROVE,
        approver_identity="portfolio-owner-001",
        decided_at="2026-07-25T20:05:00+00:00",
        reason="Reviewed governed constraints and evidence.",
    )

    assert record.status is ApprovalStatus.APPROVED
    assert record.decision is ApprovalDecision.APPROVE
    assert verify_approval_record_identity(
        record,
        record.record_id,
    )


def test_rejection_does_not_create_execution_authority() -> None:
    package = build_order_intent_package(
        _rebalance_package(),
        account_capacity=_capacity(),
    )
    request = create_approval_request(
        package,
        created_at="2026-07-25T20:00:00+00:00",
    )

    record = decide_approval_request(
        request,
        decision=ApprovalDecision.REJECT,
        approver_identity="portfolio-owner-001",
        decided_at="2026-07-25T20:05:00+00:00",
        reason="Rejected for additional review.",
    )

    assert record.status is ApprovalStatus.REJECTED
    assert package.execution_authority is False


def test_approval_request_can_expire() -> None:
    package = build_order_intent_package(
        _rebalance_package(),
        account_capacity=_capacity(),
    )
    request = create_approval_request(
        package,
        created_at="2026-07-25T20:00:00+00:00",
    )

    record = expire_approval_request(
        request,
        decided_at="2026-07-26T20:00:00+00:00",
    )

    assert record.status is ApprovalStatus.EXPIRED
    assert record.decision is ApprovalDecision.REJECT
    assert record.approver_identity == "system-expiration"


def test_blocked_package_cannot_request_approval() -> None:
    package = build_order_intent_package(
        _rebalance_package(),
        account_capacity=_capacity(
            buying_power=Decimal("1.00"),
        ),
    )

    with pytest.raises(
        ValueError,
        match="ready-for-approval",
    ):
        create_approval_request(
            package,
            created_at="2026-07-25T20:00:00+00:00",
        )


def test_comparison_reports_changed_order_intent() -> None:
    left = build_order_intent_package(
        _rebalance_package(),
        account_capacity=_capacity(),
    )

    right = build_order_intent_package(
        _rebalance_package(
            proposals=(
                _proposal(
                    proposed_value=Decimal("200.00"),
                    proposed_quantity=Decimal("1.000000"),
                ),
            ),
        ),
        account_capacity=_capacity(),
        order_type=OrderType.LIMIT,
        limit_prices={"AAPL": Decimal("199.00")},
    )

    comparison = compare_order_intent_packages(left, right)

    assert "AAPL" in comparison.changed_quantities
    assert "AAPL" in comparison.changed_notionals
    assert "AAPL" in comparison.changed_order_types
    assert "AAPL" in comparison.changed_limit_prices
    assert comparison.left_package_id == left.package_id
    assert comparison.right_package_id == right.package_id


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("market", OrderType.MARKET),
        (" LIMIT ", OrderType.LIMIT),
    ],
)
def test_normalizes_order_type(
    raw: str,
    expected: OrderType,
) -> None:
    assert normalize_order_type(raw) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("day", TimeInForce.DAY),
        (" GTC ", TimeInForce.GTC),
    ],
)
def test_normalizes_time_in_force(
    raw: str,
    expected: TimeInForce,
) -> None:
    assert normalize_time_in_force(raw) is expected


def test_normalizes_limit_price_symbols() -> None:
    prices = normalize_limit_prices(
        {
            " aapl ": Decimal("200.00"),
            "msft": Decimal("450.00"),
        }
    )

    assert prices == {
        "AAPL": Decimal("200.00"),
        "MSFT": Decimal("450.00"),
    }


def test_invalid_limit_price_is_rejected() -> None:
    with pytest.raises(
        ValueError,
        match="must be positive",
    ):
        normalize_limit_prices(
            {"AAPL": Decimal("0")},
        )


def test_policy_blocks_excessive_order_notional() -> None:
    policy = OrderIntentPolicy(
        max_order_notional=Decimal("50.00"),
    )

    package = build_order_intent_package(
        _rebalance_package(),
        account_capacity=_capacity(),
        policy=policy,
    )

    assert package.status is OrderIntentStatus.BLOCKED
    assert "maximum_order_notional" in package.blockers


def test_market_orders_can_be_disabled() -> None:
    policy = OrderIntentPolicy(
        allow_market_orders=False,
    )

    package = build_order_intent_package(
        _rebalance_package(),
        account_capacity=_capacity(),
        policy=policy,
    )

    assert package.status is OrderIntentStatus.BLOCKED
    assert "order_type_allowed" in package.blockers
