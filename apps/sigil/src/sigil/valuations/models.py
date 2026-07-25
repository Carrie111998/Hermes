"""Immutable models for governed investment valuation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sigil.accounting.models import (
    canonical_digest,
    decimal_text,
    digest,
    identifier,
    reject_secret_bearing,
    timestamp,
)
from sigil.integrations.providers.models import FinancialDataValidationError


def _identity(instance: object, field_name: str) -> None:
    current = getattr(instance, field_name)
    payload = {key: value for key, value in asdict(instance).items() if key != field_name}
    computed = canonical_digest(payload)
    if current and current != computed:
        raise FinancialDataValidationError(f"{field_name} mismatch")
    object.__setattr__(instance, field_name, computed)


def _ids(
    values: tuple[str, ...],
    field_name: str,
    *,
    required: bool = False,
) -> tuple[str, ...]:
    normalized = tuple(sorted(set(values)))
    if required and not normalized:
        raise FinancialDataValidationError(f"{field_name} is required")
    for value in normalized:
        identifier(value, field_name)
    return normalized


def _objects(
    values: tuple[Any, ...],
    identity_field: str,
    field_name: str,
) -> tuple[Any, ...]:
    identities = [getattr(value, identity_field) for value in values]
    if len(identities) != len(set(identities)):
        raise FinancialDataValidationError(f"duplicate {field_name}")
    return tuple(sorted(values, key=lambda value: getattr(value, identity_field)))


class ValuationMethod(StrEnum):
    DISCOUNTED_CASH_FLOW = "discounted_cash_flow"
    EXIT_MULTIPLE = "exit_multiple"
    MARKET_MULTIPLE = "market_multiple"
    ASSET_BASED = "asset_based"
    DIVIDEND_DISCOUNT = "dividend_discount"
    RESIDUAL_INCOME = "residual_income"


class ValuationCase(StrEnum):
    BEAR = "bear"
    BASE = "base"
    BULL = "bull"


class ValuationConfidenceClassification(StrEnum):
    UNAVAILABLE = "unavailable"
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"


class ValuationCompletenessClassification(StrEnum):
    UNAVAILABLE = "unavailable"
    PARTIAL = "partial"
    COMPLETE = "complete"


class ValuationReadinessClassification(StrEnum):
    BLOCKED = "blocked"
    NEEDS_REVIEW = "needs_review"
    READY_FOR_REVIEW = "ready_for_review"


class ValuationUnavailableReason(StrEnum):
    INVALID_THESIS_PACKAGE = "invalid_thesis_package"
    THESIS_NOT_READY = "thesis_not_ready"
    MISSING_REQUIRED_ASSUMPTION = "missing_required_assumption"
    MISSING_REQUIRED_OBSERVATION = "missing_required_observation"
    UNSUPPORTED_METHOD = "unsupported_method"
    INVALID_TIME_HORIZON = "invalid_time_horizon"
    NON_POSITIVE_DENOMINATOR = "non_positive_denominator"
    UNRESOLVED_DEPENDENCY = "unresolved_dependency"
    STALE_INPUT = "stale_input"
    INSUFFICIENT_CASE_COVERAGE = "insufficient_case_coverage"


@dataclass(frozen=True, slots=True)
class ValuationObservation:
    observation_id: str
    metric: str
    value: str
    unit: str
    as_of: datetime
    source_identity: str
    source_claim_ids: tuple[str, ...] = ()
    observation_identity: str = ""

    def __post_init__(self) -> None:
        identifier(self.observation_id, "valuation observation")
        if not self.metric.strip():
            raise FinancialDataValidationError("valuation metric is required")
        object.__setattr__(
            self,
            "value",
            decimal_text(self.value, "valuation observation value", nonnegative=False),
        )
        if not self.unit.strip():
            raise FinancialDataValidationError("valuation observation unit is required")
        timestamp(self.as_of, "valuation observation as_of")
        digest(self.source_identity, "valuation observation source_identity")
        object.__setattr__(
            self,
            "source_claim_ids",
            _ids(self.source_claim_ids, "source claim ids"),
        )
        reject_secret_bearing(asdict(self))
        _identity(self, "observation_identity")


@dataclass(frozen=True, slots=True)
class ValuationAssumption:
    assumption_id: str
    name: str
    value: str
    unit: str
    case: ValuationCase
    rationale: str
    source_claim_ids: tuple[str, ...] = ()
    valuation_dependency_ids: tuple[str, ...] = ()
    assumption_identity: str = ""

    def __post_init__(self) -> None:
        identifier(self.assumption_id, "valuation assumption")
        if not self.name.strip():
            raise FinancialDataValidationError("valuation assumption name is required")
        object.__setattr__(
            self,
            "value",
            decimal_text(self.value, "valuation assumption value", nonnegative=False),
        )
        if not self.unit.strip():
            raise FinancialDataValidationError("valuation assumption unit is required")
        if not self.rationale.strip():
            raise FinancialDataValidationError("valuation assumption rationale is required")
        object.__setattr__(
            self,
            "source_claim_ids",
            _ids(self.source_claim_ids, "source claim ids"),
        )
        object.__setattr__(
            self,
            "valuation_dependency_ids",
            _ids(self.valuation_dependency_ids, "valuation dependency ids"),
        )
        reject_secret_bearing(asdict(self))
        _identity(self, "assumption_identity")


@dataclass(frozen=True, slots=True)
class ValuationScenarioResult:
    scenario_id: str
    case: ValuationCase
    method: ValuationMethod
    enterprise_value: str | None
    equity_value: str | None
    per_share_value: str | None
    currency: str
    assumption_ids: tuple[str, ...]
    observation_ids: tuple[str, ...]
    valuation_dependency_ids: tuple[str, ...]
    unavailable_reasons: tuple[ValuationUnavailableReason, ...] = ()
    scenario_identity: str = ""

    def __post_init__(self) -> None:
        identifier(self.scenario_id, "valuation scenario")
        for field in ("enterprise_value", "equity_value", "per_share_value"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(
                    self,
                    field,
                    decimal_text(value, field.replace("_", " "), nonnegative=False),
                )
        if not self.currency.strip():
            raise FinancialDataValidationError("valuation currency is required")
        for field in (
            "assumption_ids",
            "observation_ids",
            "valuation_dependency_ids",
        ):
            object.__setattr__(
                self,
                field,
                _ids(getattr(self, field), field, required=True),
            )
        object.__setattr__(
            self,
            "unavailable_reasons",
            tuple(sorted(set(self.unavailable_reasons), key=lambda item: item.value)),
        )
        if self.unavailable_reasons and any(
            value is not None
            for value in (self.enterprise_value, self.equity_value, self.per_share_value)
        ):
            raise FinancialDataValidationError(
                "unavailable valuation scenario cannot contain calculated values"
            )
        if not self.unavailable_reasons and all(
            value is None
            for value in (self.enterprise_value, self.equity_value, self.per_share_value)
        ):
            raise FinancialDataValidationError(
                "available valuation scenario requires at least one calculated value"
            )
        _identity(self, "scenario_identity")


@dataclass(frozen=True, slots=True)
class ValuationSensitivityPoint:
    sensitivity_id: str
    scenario_id: str
    changed_assumption_id: str
    changed_value: str
    resulting_per_share_value: str | None
    unavailable_reason: ValuationUnavailableReason | None = None
    sensitivity_identity: str = ""

    def __post_init__(self) -> None:
        identifier(self.sensitivity_id, "valuation sensitivity")
        identifier(self.scenario_id, "valuation scenario")
        identifier(self.changed_assumption_id, "valuation assumption")
        object.__setattr__(
            self,
            "changed_value",
            decimal_text(self.changed_value, "changed valuation assumption", nonnegative=False),
        )
        if self.resulting_per_share_value is not None:
            object.__setattr__(
                self,
                "resulting_per_share_value",
                decimal_text(
                    self.resulting_per_share_value,
                    "resulting per share value",
                    nonnegative=False,
                ),
            )
        if self.unavailable_reason is not None and self.resulting_per_share_value is not None:
            raise FinancialDataValidationError(
                "unavailable sensitivity point cannot contain a result"
            )
        if self.unavailable_reason is None and self.resulting_per_share_value is None:
            raise FinancialDataValidationError("available sensitivity point requires a result")
        _identity(self, "sensitivity_identity")


@dataclass(frozen=True, slots=True)
class ValuationProvenance:
    thesis_package_identity: str
    policy_identity: str
    observation_identities: tuple[str, ...]
    assumption_identities: tuple[str, ...]
    provenance_identity: str = ""

    def __post_init__(self) -> None:
        digest(self.thesis_package_identity, "thesis_package_identity")
        digest(self.policy_identity, "policy_identity")
        for field in ("observation_identities", "assumption_identities"):
            values = tuple(sorted(set(getattr(self, field))))
            if not values:
                raise FinancialDataValidationError(f"{field} is required")
            for value in values:
                digest(value, field)
            object.__setattr__(self, field, values)
        _identity(self, "provenance_identity")


@dataclass(frozen=True, slots=True)
class ValuationPackage:
    package_version: str
    policy_identity: str
    thesis_package_identity: str
    issuer_id: str
    security_id: str
    constructed_at: datetime
    currency: str
    observations: tuple[ValuationObservation, ...]
    assumptions: tuple[ValuationAssumption, ...]
    scenarios: tuple[ValuationScenarioResult, ...]
    sensitivity_points: tuple[ValuationSensitivityPoint, ...]
    confidence: ValuationConfidenceClassification
    completeness: ValuationCompletenessClassification
    readiness: ValuationReadinessClassification
    unavailable_reasons: tuple[ValuationUnavailableReason, ...]
    provenance: ValuationProvenance
    readiness_blockers: tuple[str, ...] = ()
    confidence_components: tuple[tuple[str, str], ...] = ()
    package_identity: str = ""

    def __post_init__(self) -> None:
        digest(self.policy_identity, "policy_identity")
        digest(self.thesis_package_identity, "thesis_package_identity")
        identifier(self.issuer_id, "issuer_id")
        identifier(self.security_id, "security_id")
        timestamp(self.constructed_at, "constructed_at")
        if not self.package_version.strip():
            raise FinancialDataValidationError("valuation package version is required")
        if not self.currency.strip():
            raise FinancialDataValidationError("valuation currency is required")

        fields = (
            ("observations", "observation_identity"),
            ("assumptions", "assumption_identity"),
            ("scenarios", "scenario_identity"),
            ("sensitivity_points", "sensitivity_identity"),
        )
        for field, identity_field in fields:
            object.__setattr__(
                self,
                field,
                _objects(getattr(self, field), identity_field, field),
            )

        if (
            self.thesis_package_identity != self.provenance.thesis_package_identity
            or self.policy_identity != self.provenance.policy_identity
        ):
            raise FinancialDataValidationError("valuation provenance mismatch")

        object.__setattr__(
            self,
            "unavailable_reasons",
            tuple(sorted(set(self.unavailable_reasons), key=lambda item: item.value)),
        )
        object.__setattr__(
            self,
            "readiness_blockers",
            tuple(sorted(set(self.readiness_blockers))),
        )
        object.__setattr__(
            self,
            "confidence_components",
            tuple(sorted(self.confidence_components)),
        )

        reject_secret_bearing(asdict(self))
        _identity(self, "package_identity")


@dataclass(frozen=True, slots=True)
class ValuationComparison:
    before_identity: str
    after_identity: str
    changes: tuple[tuple[str, tuple[str, ...]], ...]
    confidence_change: tuple[str, str] | None
    completeness_change: tuple[str, str] | None
    readiness_change: tuple[str, str] | None
    comparison_identity: str = ""

    def __post_init__(self) -> None:
        digest(self.before_identity, "before_identity")
        digest(self.after_identity, "after_identity")
        object.__setattr__(self, "changes", tuple(sorted(self.changes)))
        _identity(self, "comparison_identity")
