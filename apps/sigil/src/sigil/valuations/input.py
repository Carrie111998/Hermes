"""Normalized caller input for governed investment valuation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

from sigil.accounting.models import (
    canonical_digest,
    decimal_text,
    digest,
    identifier,
    reject_secret_bearing,
    timestamp,
)
from sigil.integrations.providers.models import FinancialDataValidationError


@dataclass(frozen=True, slots=True)
class DiscountedCashFlowInput:
    """Explicit, auditable inputs for a deterministic DCF valuation."""

    issuer_id: str
    security_id: str
    thesis_package_identity: str
    source_identity: str
    policy_identity: str
    as_of: datetime
    currency: str
    base_free_cash_flow: str
    diluted_shares: str
    net_debt: str
    bear_growth_rate: str
    base_growth_rate: str
    bull_growth_rate: str
    bear_discount_rate: str
    base_discount_rate: str
    bull_discount_rate: str
    bear_terminal_growth_rate: str
    base_terminal_growth_rate: str
    bull_terminal_growth_rate: str
    forecast_years: int = 5
    sensitivity_rate_delta: str = "0.01"
    request_identity: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "issuer_id", identifier(self.issuer_id, "issuer_id"))
        object.__setattr__(
            self,
            "security_id",
            identifier(self.security_id, "security_id"),
        )

        digest(self.thesis_package_identity, "thesis_package_identity")
        digest(self.source_identity, "source_identity")
        digest(self.policy_identity, "policy_identity")

        object.__setattr__(self, "as_of", timestamp(self.as_of, "as_of"))

        currency = self.currency.strip().upper()
        if currency != "USD":
            raise FinancialDataValidationError("governed valuation currently supports USD only")
        object.__setattr__(self, "currency", currency)

        decimal_fields = (
            "base_free_cash_flow",
            "diluted_shares",
            "net_debt",
            "bear_growth_rate",
            "base_growth_rate",
            "bull_growth_rate",
            "bear_discount_rate",
            "base_discount_rate",
            "bull_discount_rate",
            "bear_terminal_growth_rate",
            "base_terminal_growth_rate",
            "bull_terminal_growth_rate",
            "sensitivity_rate_delta",
        )

        for field_name in decimal_fields:
            normalized = decimal_text(
                getattr(self, field_name),
                field_name,
                nonnegative=False,
            )
            object.__setattr__(self, field_name, normalized)

        if not isinstance(self.forecast_years, int) or isinstance(
            self.forecast_years,
            bool,
        ):
            raise FinancialDataValidationError("forecast_years must be an integer")
        if self.forecast_years < 1 or self.forecast_years > 20:
            raise FinancialDataValidationError("forecast_years must be between 1 and 20")

        reject_secret_bearing(asdict(self))

        material = {key: value for key, value in asdict(self).items() if key != "request_identity"}
        computed = canonical_digest(material)

        if self.request_identity and self.request_identity != computed:
            raise FinancialDataValidationError("request_identity mismatch")

        object.__setattr__(self, "request_identity", computed)
