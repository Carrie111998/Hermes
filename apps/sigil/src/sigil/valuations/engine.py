"""Deterministic governed valuation construction."""

from __future__ import annotations

from decimal import Decimal, localcontext

from sigil.accounting.models import decimal_text
from sigil.integrations.providers.models import FinancialDataValidationError

from .input import DiscountedCashFlowInput
from .models import (
    ValuationAssumption,
    ValuationCase,
    ValuationCompletenessClassification,
    ValuationConfidenceClassification,
    ValuationMethod,
    ValuationObservation,
    ValuationPackage,
    ValuationProvenance,
    ValuationReadinessClassification,
    ValuationScenarioResult,
    ValuationSensitivityPoint,
)
from .policy import InvestmentValuationPolicy


def _decimal(value: str) -> Decimal:
    return Decimal(value)


def _text(value: Decimal, name: str) -> str:
    return decimal_text(
        format(value, "f"),
        name,
        nonnegative=False,
    )


def _validate_request(
    request: DiscountedCashFlowInput,
    policy: InvestmentValuationPolicy,
) -> None:
    if request.policy_identity != policy.policy_identity:
        raise FinancialDataValidationError("valuation request policy identity mismatch")

    if not policy.permits_method(ValuationMethod.DISCOUNTED_CASH_FLOW):
        raise FinancialDataValidationError("discounted cash flow method is not permitted")

    if not policy.permits_currency(request.currency):
        raise FinancialDataValidationError("valuation currency is not permitted")

    free_cash_flow = _decimal(request.base_free_cash_flow)
    shares = _decimal(request.diluted_shares)

    if free_cash_flow <= 0:
        raise FinancialDataValidationError("base_free_cash_flow must be greater than zero")
    if shares <= 0:
        raise FinancialDataValidationError("diluted_shares must be greater than zero")

    maximum_terminal_growth = _decimal(policy.maximum_terminal_growth_rate)
    minimum_discount_rate = _decimal(policy.minimum_discount_rate)

    for case in ("bear", "base", "bull"):
        discount_rate = _decimal(getattr(request, f"{case}_discount_rate"))
        terminal_growth_rate = _decimal(getattr(request, f"{case}_terminal_growth_rate"))

        if discount_rate < minimum_discount_rate:
            raise FinancialDataValidationError(f"{case} discount rate is below policy minimum")
        if terminal_growth_rate > maximum_terminal_growth:
            raise FinancialDataValidationError(
                f"{case} terminal growth rate exceeds policy maximum"
            )
        if discount_rate <= terminal_growth_rate:
            raise FinancialDataValidationError(
                f"{case} discount rate must exceed terminal growth rate"
            )

    if _decimal(request.sensitivity_rate_delta) <= 0:
        raise FinancialDataValidationError("sensitivity_rate_delta must be greater than zero")


def _case_parameters(
    request: DiscountedCashFlowInput,
    case: ValuationCase,
) -> tuple[Decimal, Decimal, Decimal]:
    prefix = case.value
    return (
        _decimal(getattr(request, f"{prefix}_growth_rate")),
        _decimal(getattr(request, f"{prefix}_discount_rate")),
        _decimal(getattr(request, f"{prefix}_terminal_growth_rate")),
    )


def _dcf_value(
    *,
    base_free_cash_flow: Decimal,
    growth_rate: Decimal,
    discount_rate: Decimal,
    terminal_growth_rate: Decimal,
    forecast_years: int,
    net_debt: Decimal,
    diluted_shares: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    with localcontext() as context:
        context.prec = 38

        present_value = Decimal("0")
        projected_cash_flow = base_free_cash_flow

        for year in range(1, forecast_years + 1):
            projected_cash_flow *= Decimal("1") + growth_rate
            discount_factor = (Decimal("1") + discount_rate) ** year
            present_value += projected_cash_flow / discount_factor

        terminal_cash_flow = projected_cash_flow * (Decimal("1") + terminal_growth_rate)
        terminal_value = terminal_cash_flow / (discount_rate - terminal_growth_rate)
        discounted_terminal_value = terminal_value / (
            (Decimal("1") + discount_rate) ** forecast_years
        )

        enterprise_value = present_value + discounted_terminal_value
        equity_value = enterprise_value - net_debt
        per_share_value = equity_value / diluted_shares

        return enterprise_value, equity_value, per_share_value


def _observations(
    request: DiscountedCashFlowInput,
) -> tuple[ValuationObservation, ...]:
    return (
        ValuationObservation(
            observation_id="observed-free-cash-flow",
            metric="free_cash_flow",
            value=request.base_free_cash_flow,
            unit=request.currency,
            as_of=request.as_of,
            source_identity=request.source_identity,
            source_claim_ids=("claim-free-cash-flow",),
        ),
        ValuationObservation(
            observation_id="observed-diluted-shares",
            metric="diluted_shares",
            value=request.diluted_shares,
            unit="shares",
            as_of=request.as_of,
            source_identity=request.source_identity,
            source_claim_ids=("claim-diluted-shares",),
        ),
        ValuationObservation(
            observation_id="observed-net-debt",
            metric="net_debt",
            value=request.net_debt,
            unit=request.currency,
            as_of=request.as_of,
            source_identity=request.source_identity,
            source_claim_ids=("claim-net-debt",),
        ),
    )


def _assumptions(
    request: DiscountedCashFlowInput,
) -> tuple[ValuationAssumption, ...]:
    assumptions: list[ValuationAssumption] = []

    for case in ValuationCase:
        prefix = case.value
        values = (
            (
                "growth-rate",
                "free cash flow growth rate",
                getattr(request, f"{prefix}_growth_rate"),
                "ratio",
            ),
            (
                "discount-rate",
                "discount rate",
                getattr(request, f"{prefix}_discount_rate"),
                "ratio",
            ),
            (
                "terminal-growth-rate",
                "terminal growth rate",
                getattr(request, f"{prefix}_terminal_growth_rate"),
                "ratio",
            ),
        )

        for suffix, name, value, unit in values:
            assumptions.append(
                ValuationAssumption(
                    assumption_id=f"assumption-{prefix}-{suffix}",
                    name=name,
                    value=value,
                    unit=unit,
                    case=case,
                    rationale=(
                        "Explicit caller-supplied governed valuation "
                        f"assumption for the {prefix} case."
                    ),
                    source_claim_ids=(),
                    valuation_dependency_ids=(f"valuation-dependency-{suffix}",),
                )
            )

    return tuple(assumptions)


def _scenario(
    request: DiscountedCashFlowInput,
    case: ValuationCase,
) -> ValuationScenarioResult:
    growth_rate, discount_rate, terminal_growth_rate = _case_parameters(
        request,
        case,
    )

    enterprise_value, equity_value, per_share_value = _dcf_value(
        base_free_cash_flow=_decimal(request.base_free_cash_flow),
        growth_rate=growth_rate,
        discount_rate=discount_rate,
        terminal_growth_rate=terminal_growth_rate,
        forecast_years=request.forecast_years,
        net_debt=_decimal(request.net_debt),
        diluted_shares=_decimal(request.diluted_shares),
    )

    if enterprise_value < 0:
        raise FinancialDataValidationError(f"{case.value} enterprise value cannot be negative")

    return ValuationScenarioResult(
        scenario_id=f"scenario-{case.value}",
        case=case,
        method=ValuationMethod.DISCOUNTED_CASH_FLOW,
        enterprise_value=_text(
            enterprise_value,
            f"{case.value} enterprise value",
        ),
        equity_value=_text(
            equity_value,
            f"{case.value} equity value",
        ),
        per_share_value=_text(
            per_share_value,
            f"{case.value} per-share value",
        ),
        currency=request.currency,
        assumption_ids=(
            f"assumption-{case.value}-growth-rate",
            f"assumption-{case.value}-discount-rate",
            f"assumption-{case.value}-terminal-growth-rate",
        ),
        observation_ids=(
            "observed-free-cash-flow",
            "observed-diluted-shares",
            "observed-net-debt",
        ),
        valuation_dependency_ids=(
            "valuation-dependency-growth-rate",
            "valuation-dependency-discount-rate",
            "valuation-dependency-terminal-growth-rate",
        ),
    )


def _sensitivity_points(
    request: DiscountedCashFlowInput,
) -> tuple[ValuationSensitivityPoint, ...]:
    growth_rate, discount_rate, terminal_growth_rate = _case_parameters(
        request,
        ValuationCase.BASE,
    )
    delta = _decimal(request.sensitivity_rate_delta)

    points: list[ValuationSensitivityPoint] = []

    for direction, adjusted_rate in (
        ("lower", discount_rate - delta),
        ("higher", discount_rate + delta),
    ):
        if adjusted_rate <= terminal_growth_rate:
            raise FinancialDataValidationError(
                "discount-rate sensitivity crosses terminal growth rate"
            )

        _, _, per_share_value = _dcf_value(
            base_free_cash_flow=_decimal(request.base_free_cash_flow),
            growth_rate=growth_rate,
            discount_rate=adjusted_rate,
            terminal_growth_rate=terminal_growth_rate,
            forecast_years=request.forecast_years,
            net_debt=_decimal(request.net_debt),
            diluted_shares=_decimal(request.diluted_shares),
        )

        points.append(
            ValuationSensitivityPoint(
                sensitivity_id=(f"sensitivity-base-discount-rate-{direction}"),
                scenario_id="scenario-base",
                changed_assumption_id=("assumption-base-discount-rate"),
                changed_value=_text(
                    adjusted_rate,
                    "adjusted discount rate",
                ),
                resulting_per_share_value=_text(
                    per_share_value,
                    "sensitivity per-share value",
                ),
            )
        )

    return tuple(points)


def construct_discounted_cash_flow_valuation(
    request: DiscountedCashFlowInput,
    *,
    policy: InvestmentValuationPolicy | None = None,
) -> ValuationPackage:
    """Construct a deterministic, immutable bear/base/bull DCF package."""

    policy = policy or InvestmentValuationPolicy()

    _validate_request(request, policy)

    observations = _observations(request)
    assumptions = _assumptions(request)
    scenarios = tuple(
        _scenario(request, case)
        for case in (
            ValuationCase.BEAR,
            ValuationCase.BASE,
            ValuationCase.BULL,
        )
    )
    sensitivity_points = _sensitivity_points(request)

    provenance = ValuationProvenance(
        thesis_package_identity=request.thesis_package_identity,
        policy_identity=policy.policy_identity,
        observation_identities=tuple(item.observation_identity for item in observations),
        assumption_identities=tuple(item.assumption_identity for item in assumptions),
    )

    return ValuationPackage(
        package_version="1",
        policy_identity=policy.policy_identity,
        thesis_package_identity=request.thesis_package_identity,
        issuer_id=request.issuer_id,
        security_id=request.security_id,
        constructed_at=request.as_of,
        currency=request.currency,
        observations=observations,
        assumptions=assumptions,
        scenarios=scenarios,
        sensitivity_points=sensitivity_points,
        confidence=ValuationConfidenceClassification.MODERATE,
        completeness=ValuationCompletenessClassification.COMPLETE,
        readiness=ValuationReadinessClassification.READY_FOR_REVIEW,
        unavailable_reasons=(),
        provenance=provenance,
        readiness_blockers=(),
        confidence_components=(
            ("input_quality", "moderate"),
            ("method_coverage", "single_method"),
            ("scenario_coverage", "bear_base_bull"),
            ("sensitivity_coverage", "discount_rate"),
        ),
    )
