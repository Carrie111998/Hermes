from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from .models import AccountCapacity, OrderType, TimeInForce


def normalize_capacity(
    *,
    available_buying_power: Decimal,
    sellable_quantities: Mapping[str, Decimal] | None = None,
) -> AccountCapacity:
    return AccountCapacity(
        available_buying_power=available_buying_power,
        sellable_quantities=sellable_quantities or {},
    )


def normalize_order_type(value: OrderType | str) -> OrderType:
    if isinstance(value, OrderType):
        return value

    try:
        return OrderType(value.strip().lower())
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"unsupported order type: {value!r}") from exc


def normalize_time_in_force(
    value: TimeInForce | str,
) -> TimeInForce:
    if isinstance(value, TimeInForce):
        return value

    try:
        return TimeInForce(value.strip().lower())
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"unsupported time in force: {value!r}") from exc


def normalize_limit_prices(
    prices: Mapping[str, Decimal] | None,
) -> dict[str, Decimal]:
    normalized: dict[str, Decimal] = {}

    for symbol, price in (prices or {}).items():
        clean_symbol = symbol.strip().upper()
        if not clean_symbol:
            raise ValueError("limit-price symbol must not be empty")
        if price <= 0:
            raise ValueError(
                f"limit price for {clean_symbol} must be positive"
            )
        if clean_symbol in normalized:
            raise ValueError(
                f"duplicate limit-price symbol: {clean_symbol}"
            )
        normalized[clean_symbol] = price

    return dict(sorted(normalized.items()))
