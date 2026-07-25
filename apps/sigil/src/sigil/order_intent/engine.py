from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, ROUND_DOWN
from typing import Mapping

from sigil.portfolio_rebalancing.models import (
    RebalanceAction,
    RebalancePackage,
    RebalanceStatus,
    TradeProposal,
)

from .audit import (
    order_intent_identity,
    order_intent_package_identity,
    source_proposal_identity,
)
from .input import (
    normalize_limit_prices,
    normalize_order_type,
    normalize_time_in_force,
)
from .models import (
    AccountCapacity,
    OrderIntent,
    OrderIntentConstraint,
    OrderIntentPackage,
    OrderIntentPolicy,
    OrderIntentStatus,
    OrderSide,
    OrderType,
    TimeInForce,
)
from .policy import policy_snapshot


def _quantum(precision: int) -> Decimal:
    return Decimal("1").scaleb(-precision)


def _proposal_payload(
    *,
    rebalance_package_id: str,
    proposal: TradeProposal,
) -> dict[str, object]:
    return {
        "rebalance_package_id": rebalance_package_id,
        "symbol": proposal.symbol,
        "action": proposal.action.value,
        "proposed_value": proposal.proposed_value,
        "proposed_quantity": proposal.proposed_quantity,
        "price": proposal.price,
        "issuer": proposal.issuer,
        "sector": proposal.sector,
        "rationale": proposal.rationale,
    }


def _constraint(
    *,
    name: str,
    passed: bool,
    observed: object,
    limit: object,
) -> OrderIntentConstraint:
    return OrderIntentConstraint(
        name=name,
        passed=passed,
        observed=str(observed),
        limit=str(limit),
    )


def _build_intent(
    *,
    rebalance_package: RebalancePackage,
    proposal: TradeProposal,
    policy: OrderIntentPolicy,
    order_type: OrderType,
    time_in_force: TimeInForce,
    limit_prices: Mapping[str, Decimal],
    evidence_references: tuple[str, ...],
) -> OrderIntent:
    if proposal.action not in (
        RebalanceAction.BUY,
        RebalanceAction.SELL,
    ):
        raise ValueError(
            f"{proposal.symbol}: HOLD proposals cannot become order intents"
        )

    quantity_quantum = _quantum(policy.quantity_precision)
    price_quantum = _quantum(policy.price_precision)
    notional_quantum = _quantum(policy.notional_precision)

    quantity = proposal.proposed_quantity.quantize(
        quantity_quantum,
        rounding=ROUND_DOWN,
    )
    reference_price = proposal.price.quantize(
        price_quantum,
        rounding=ROUND_DOWN,
    )
    notional = proposal.proposed_value.quantize(
        notional_quantum,
        rounding=ROUND_DOWN,
    )

    limit_price = None
    if order_type is OrderType.LIMIT:
        supplied_limit = limit_prices.get(proposal.symbol)
        if supplied_limit is not None:
            limit_price = supplied_limit.quantize(
                price_quantum,
                rounding=ROUND_DOWN,
            )

    side = (
        OrderSide.BUY
        if proposal.action is RebalanceAction.BUY
        else OrderSide.SELL
    )

    source_payload = _proposal_payload(
        rebalance_package_id=rebalance_package.package_id,
        proposal=proposal,
    )
    source_id = source_proposal_identity(source_payload)

    blockers: list[str] = []
    warnings: list[str] = []
    constraints: list[OrderIntentConstraint] = []

    constraints.append(
        _constraint(
            name="minimum_order_notional",
            passed=notional >= policy.minimum_order_notional,
            observed=notional,
            limit=policy.minimum_order_notional,
        )
    )
    constraints.append(
        _constraint(
            name="maximum_order_notional",
            passed=notional <= policy.max_order_notional,
            observed=notional,
            limit=policy.max_order_notional,
        )
    )
    constraints.append(
        _constraint(
            name="positive_quantity",
            passed=quantity > 0,
            observed=quantity,
            limit="> 0",
        )
    )

    order_type_allowed = (
        policy.allow_market_orders
        if order_type is OrderType.MARKET
        else policy.allow_limit_orders
    )
    constraints.append(
        _constraint(
            name="order_type_allowed",
            passed=order_type_allowed,
            observed=order_type.value,
            limit="allowed by policy",
        )
    )

    time_in_force_allowed = (
        time_in_force in policy.allowed_time_in_force
    )
    constraints.append(
        _constraint(
            name="time_in_force_allowed",
            passed=time_in_force_allowed,
            observed=time_in_force.value,
            limit=",".join(
                item.value
                for item in policy.allowed_time_in_force
            ),
        )
    )

    limit_price_valid = not (
        order_type is OrderType.LIMIT
        and policy.require_limit_price_for_limit_orders
        and limit_price is None
    )
    constraints.append(
        _constraint(
            name="limit_price_present",
            passed=limit_price_valid,
            observed=limit_price,
            limit=(
                "required"
                if order_type is OrderType.LIMIT
                else "not applicable"
            ),
        )
    )

    evidence_present = bool(evidence_references)
    constraints.append(
        _constraint(
            name="evidence_present",
            passed=(
                evidence_present
                or not policy.require_evidence
            ),
            observed=len(evidence_references),
            limit=(
                "at least 1"
                if policy.require_evidence
                else "optional"
            ),
        )
    )

    blockers.extend(
        result.name
        for result in constraints
        if not result.passed
    )

    if (
        not policy.allow_fractional_shares
        and quantity != quantity.to_integral_value()
    ):
        blockers.append("fractional_shares_not_allowed")

    if quantity <= 0:
        blockers.append("quantity_rounds_to_zero")

    if notional < policy.minimum_order_notional:
        warnings.append(
            f"{proposal.symbol}: order is below minimum notional"
        )

    unsigned = OrderIntent(
        intent_id="",
        source_proposal_id=source_id,
        source_rebalance_package_id=rebalance_package.package_id,
        symbol=proposal.symbol,
        side=side,
        order_type=order_type,
        time_in_force=time_in_force,
        quantity=quantity,
        reference_price=reference_price,
        notional=notional,
        limit_price=limit_price,
        issuer=proposal.issuer,
        sector=proposal.sector,
        rationale=proposal.rationale
        + (
            "intent is analytical and cannot execute an order",
            "human approval authorizes downstream consideration only",
        ),
        evidence_references=evidence_references,
        constraints=tuple(constraints),
        blockers=tuple(sorted(set(blockers))),
        warnings=tuple(sorted(set(warnings))),
    )

    return replace(
        unsigned,
        intent_id=order_intent_identity(unsigned),
    )


def build_order_intent_package(
    rebalance_package: RebalancePackage,
    *,
    account_capacity: AccountCapacity,
    order_type: OrderType | str = OrderType.MARKET,
    time_in_force: TimeInForce | str = TimeInForce.DAY,
    limit_prices: Mapping[str, Decimal] | None = None,
    policy: OrderIntentPolicy | None = None,
    evidence_references: tuple[str, ...] = (),
) -> OrderIntentPackage:
    active_policy = policy or OrderIntentPolicy()
    normalized_order_type = normalize_order_type(order_type)
    normalized_time_in_force = normalize_time_in_force(
        time_in_force
    )
    normalized_limit_prices = normalize_limit_prices(limit_prices)

    if not rebalance_package.package_id.strip():
        raise ValueError(
            "rebalance package must have a verified package identity"
        )
    if not rebalance_package.source_target_package_id.strip():
        raise ValueError(
            "source_target_package_id must not be empty"
        )
    if not rebalance_package.analytical_only:
        raise ValueError(
            "source rebalance package must remain analytical only"
        )
    if rebalance_package.execution_authority:
        raise ValueError(
            "source rebalance package must not have execution authority"
        )

    blockers: list[str] = []
    warnings: list[str] = []

    if rebalance_package.status is RebalanceStatus.BLOCKED:
        blockers.append("source_rebalance_package_blocked")
    elif rebalance_package.status is RebalanceStatus.NO_ACTION:
        warnings.append("source_rebalance_package_has_no_action")

    combined_evidence = tuple(
        sorted(
            set(rebalance_package.evidence_references)
            | set(evidence_references)
        )
    )

    intents: list[OrderIntent] = []
    if rebalance_package.status is RebalanceStatus.READY:
        for proposal in rebalance_package.proposals:
            if (
                normalized_order_type is OrderType.LIMIT
                and active_policy.require_limit_price_for_limit_orders
                and proposal.symbol not in normalized_limit_prices
            ):
                blockers.append(
                    f"{proposal.symbol}:limit_price_required"
                )
                continue

            intent = _build_intent(
                rebalance_package=rebalance_package,
                proposal=proposal,
                policy=active_policy,
                order_type=normalized_order_type,
                time_in_force=normalized_time_in_force,
                limit_prices=normalized_limit_prices,
                evidence_references=combined_evidence,
            )
            intents.append(intent)
            blockers.extend(intent.blockers)
            warnings.extend(intent.warnings)

    aggregate_buy_notional = sum(
        (
            intent.notional
            for intent in intents
            if intent.side is OrderSide.BUY
        ),
        Decimal("0"),
    )
    aggregate_sell_notional = sum(
        (
            intent.notional
            for intent in intents
            if intent.side is OrderSide.SELL
        ),
        Decimal("0"),
    )
    aggregate_turnover = (
        aggregate_buy_notional + aggregate_sell_notional
    )

    constraints = (
        _constraint(
            name="maximum_intent_count",
            passed=len(intents) <= active_policy.max_intents,
            observed=len(intents),
            limit=active_policy.max_intents,
        ),
        _constraint(
            name="aggregate_buy_notional",
            passed=(
                aggregate_buy_notional
                <= active_policy.max_aggregate_buy_notional
            ),
            observed=aggregate_buy_notional,
            limit=active_policy.max_aggregate_buy_notional,
        ),
        _constraint(
            name="aggregate_sell_notional",
            passed=(
                aggregate_sell_notional
                <= active_policy.max_aggregate_sell_notional
            ),
            observed=aggregate_sell_notional,
            limit=active_policy.max_aggregate_sell_notional,
        ),
        _constraint(
            name="aggregate_turnover",
            passed=(
                aggregate_turnover
                <= active_policy.max_aggregate_turnover
            ),
            observed=aggregate_turnover,
            limit=active_policy.max_aggregate_turnover,
        ),
        _constraint(
            name="buying_power",
            passed=(
                aggregate_buy_notional
                <= account_capacity.available_buying_power
            ),
            observed=aggregate_buy_notional,
            limit=account_capacity.available_buying_power,
        ),
    )

    blockers.extend(
        result.name
        for result in constraints
        if not result.passed
    )

    for intent in intents:
        if intent.side is not OrderSide.SELL:
            continue

        available_quantity = account_capacity.sellable_quantities.get(
            intent.symbol,
            Decimal("0"),
        )
        if intent.quantity > available_quantity:
            blockers.append(
                f"{intent.symbol}:insufficient_sellable_quantity"
            )

    if blockers:
        status = OrderIntentStatus.BLOCKED
    elif not intents:
        status = OrderIntentStatus.NO_ACTION
    else:
        status = OrderIntentStatus.READY_FOR_APPROVAL

    unsigned = OrderIntentPackage(
        package_id="",
        source_rebalance_package_id=rebalance_package.package_id,
        source_target_package_id=(
            rebalance_package.source_target_package_id
        ),
        status=status,
        intents=tuple(intents),
        aggregate_buy_notional=aggregate_buy_notional,
        aggregate_sell_notional=aggregate_sell_notional,
        aggregate_turnover=aggregate_turnover,
        constraints=constraints,
        blockers=tuple(sorted(set(blockers))),
        warnings=tuple(sorted(set(warnings))),
        policy_snapshot=policy_snapshot(active_policy),
        evidence_references=combined_evidence,
    )

    return replace(
        unsigned,
        package_id=order_intent_package_identity(unsigned),
    )
