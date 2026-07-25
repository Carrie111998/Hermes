"""Governance policy for deterministic portfolio-risk analysis."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from sigil.accounting.models import canonical_digest

from .models import RiskValidationError


def _decimal(value: str, name: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise RiskValidationError(f"{name} must be decimal-compatible") from exc
    if parsed < 0:
        raise RiskValidationError(f"{name} must not be negative")
    return parsed


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    policy_id: str
    max_gross_exposure: str = "1.50"
    max_absolute_net_exposure: str = "1.00"
    max_leverage: str = "1.50"
    max_position_concentration: str = "0.20"
    max_issuer_concentration: str = "0.25"
    max_sector_concentration: str = "0.40"
    max_days_to_liquidate: str = "5.00"
    max_weighted_volatility: str = "0.45"
    max_weighted_drawdown: str = "0.35"
    block_on_critical: bool = True
    require_evidence: bool = True
    policy_identity: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.policy_id:
            raise RiskValidationError("policy_id must not be empty")
        for name in (
            "max_gross_exposure",
            "max_absolute_net_exposure",
            "max_leverage",
            "max_position_concentration",
            "max_issuer_concentration",
            "max_sector_concentration",
            "max_days_to_liquidate",
            "max_weighted_volatility",
            "max_weighted_drawdown",
        ):
            _decimal(getattr(self, name), name)
        object.__setattr__(
            self,
            "policy_identity",
            canonical_digest(
                {
                    name: getattr(self, name)
                    for name in self.__dataclass_fields__
                    if name != "policy_identity"
                }
            ),
        )
