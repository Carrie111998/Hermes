from __future__ import annotations

from collections.abc import Iterable

from .models import CurrentPosition, TargetPosition


def normalize_current_positions(
    positions: Iterable[CurrentPosition],
) -> tuple[CurrentPosition, ...]:
    by_symbol: dict[str, CurrentPosition] = {}
    for position in positions:
        if position.symbol in by_symbol:
            raise ValueError(f"duplicate current symbol: {position.symbol}")
        by_symbol[position.symbol] = position
    return tuple(by_symbol[symbol] for symbol in sorted(by_symbol))


def normalize_target_positions(
    positions: Iterable[TargetPosition],
) -> tuple[TargetPosition, ...]:
    by_symbol: dict[str, TargetPosition] = {}
    for position in positions:
        if position.symbol in by_symbol:
            raise ValueError(f"duplicate target symbol: {position.symbol}")
        by_symbol[position.symbol] = position
    total = sum(position.target_weight for position in by_symbol.values())
    if total > 1:
        raise ValueError("target weights must not exceed 1")
    return tuple(by_symbol[symbol] for symbol in sorted(by_symbol))
