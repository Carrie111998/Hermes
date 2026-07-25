"""Governance policy for deterministic investment valuation."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from sigil.accounting.models import (
    canonical_digest,
    decimal_text,
    reject_secret_bearing,
)
from sigil.integrations.providers.models import FinancialDataValidationError

from .models import ValuationMethod


@dataclass(frozen=True, slots=True)
class InvestmentValuationPolicy:
    """Fail-closed rules governing Step 16 valuation construction."""

    policy_version: str = "1"
    allowed_methods: tuple[ValuationMethod, ...] = (
        ValuationMethod.DISCOUNTED_CASH_FLOW,
        ValuationMethod.EXIT_MULTIPLE,
        ValuationMethod.MARKET_MULTIPLE,
        ValuationMethod.ASSET_BASED,
        ValuationMethod.DIVIDEND_DISCOUNT,
        ValuationMethod.RESIDUAL_INCOME,
    )
    allowed_currencies: tuple[str, ...] = ("USD",)
    required_cases: tuple[str, ...] = ("bear", "base", "bull")
    minimum_observations: int = 1
    minimum_assumptions: int = 1
    minimum_scenarios: int = 3
    require_thesis_ready_for_review: bool = True
    require_all_thesis_dependencies_mapped: bool = True
    require_sensitivity_analysis: bool = True
    maximum_terminal_growth_rate: str = "0.05"
    minimum_discount_rate: str = "0"
    permit_negative_enterprise_value: bool = False
    permit_negative_equity_value: bool = True
    permit_negative_per_share_value: bool = True
    policy_identity: str = ""

    def __post_init__(self) -> None:
        if not self.policy_version.strip():
            raise FinancialDataValidationError("valuation policy version is required")

        methods = tuple(sorted(set(self.allowed_methods), key=lambda item: item.value))
        if not methods:
            raise FinancialDataValidationError("at least one valuation method is required")
        object.__setattr__(self, "allowed_methods", methods)

        currencies = tuple(sorted(set(self.allowed_currencies)))
        if not currencies:
            raise FinancialDataValidationError("at least one valuation currency is required")
        if any(not value.strip() or value != value.upper() for value in currencies):
            raise FinancialDataValidationError("valuation currencies must be uppercase")
        object.__setattr__(self, "allowed_currencies", currencies)

        cases = tuple(sorted(set(self.required_cases)))
        supported_cases = {"bear", "base", "bull"}
        if not cases or any(value not in supported_cases for value in cases):
            raise FinancialDataValidationError("unsupported required valuation case")
        object.__setattr__(self, "required_cases", cases)

        for field in (
            "minimum_observations",
            "minimum_assumptions",
            "minimum_scenarios",
        ):
            value = getattr(self, field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise FinancialDataValidationError(f"{field} must be a nonnegative integer")

        terminal_growth = decimal_text(
            self.maximum_terminal_growth_rate,
            "maximum terminal growth rate",
            nonnegative=False,
        )
        discount_rate = decimal_text(
            self.minimum_discount_rate,
            "minimum discount rate",
            nonnegative=False,
        )
        object.__setattr__(self, "maximum_terminal_growth_rate", terminal_growth)
        object.__setattr__(self, "minimum_discount_rate", discount_rate)

        reject_secret_bearing(asdict(self))

        material = {key: value for key, value in asdict(self).items() if key != "policy_identity"}
        computed = canonical_digest(material)
        if self.policy_identity and self.policy_identity != computed:
            raise FinancialDataValidationError("policy_identity mismatch")
        object.__setattr__(self, "policy_identity", computed)

    def permits_method(self, method: ValuationMethod) -> bool:
        return method in self.allowed_methods

    def permits_currency(self, currency: str) -> bool:
        return currency in self.allowed_currencies
