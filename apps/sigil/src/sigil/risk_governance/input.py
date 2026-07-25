"""Input contracts for governed risk analysis."""

from __future__ import annotations

from dataclasses import dataclass, field

from sigil.accounting.models import canonical_digest

from .models import RiskPosition, RiskValidationError


@dataclass(frozen=True, slots=True)
class GovernedRiskRequest:
    request_id: str
    portfolio_id: str
    as_of: str
    equity_value: str
    positions: tuple[RiskPosition, ...]
    policy_identity: str
    upstream_package_identities: tuple[str, ...] = ()
    request_identity: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("request_id", "portfolio_id", "as_of", "equity_value", "policy_identity"):
            if not getattr(self, name):
                raise RiskValidationError(f"{name} must not be empty")
        if not self.positions:
            raise RiskValidationError("positions must not be empty")
        object.__setattr__(
            self,
            "positions",
            tuple(sorted(self.positions, key=lambda item: item.position_identity)),
        )
        object.__setattr__(
            self,
            "upstream_package_identities",
            tuple(sorted(set(self.upstream_package_identities))),
        )
        object.__setattr__(
            self,
            "request_identity",
            canonical_digest(
                {
                    name: getattr(self, name)
                    for name in self.__dataclass_fields__
                    if name != "request_identity"
                }
            ),
        )
