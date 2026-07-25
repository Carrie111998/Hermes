from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from .models import CandidateAsset


def normalize_candidates(candidates: Iterable[CandidateAsset]) -> tuple[CandidateAsset, ...]:
    by_symbol: dict[str, CandidateAsset] = {}
    for candidate in candidates:
        if candidate.symbol in by_symbol:
            raise ValueError(f"duplicate candidate symbol: {candidate.symbol}")
        by_symbol[candidate.symbol] = candidate
    return tuple(by_symbol[symbol] for symbol in sorted(by_symbol))


def validate_capital(capital: Decimal) -> Decimal:
    if capital <= 0:
        raise ValueError("capital must be positive")
    return capital
