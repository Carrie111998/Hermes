from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime

import pytest

from sigil.integrations.providers.models import FinancialDataValidationError
from sigil.valuations import (
    InvestmentValuationPolicy,
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
    compare_valuation_packages,
    verify_package_identity,
)


DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
NOW = datetime(2026, 7, 25, 0, 0, tzinfo=UTC)


def make_policy() -> InvestmentValuationPolicy:
    return InvestmentValuationPolicy()


def make_observation(value: str = "100") -> ValuationObservation:
    return ValuationObservation(
        observation_id="obs-revenue",
        metric="revenue",
        value=value,
        unit="USD",
        as_of=NOW,
        source_identity=DIGEST_A,
        source_claim_ids=("claim-revenue",),
    )


def make_assumption(value: str = "0.10") -> ValuationAssumption:
    return ValuationAssumption(
        assumption_id="assumption-growth",
        name="revenue growth",
        value=value,
        unit="ratio",
        case=ValuationCase.BASE,
        rationale="Explicit caller-supplied base-case growth assumption.",
        source_claim_ids=("claim-revenue",),
        valuation_dependency_ids=("dependency-growth",),
    )


def make_scenario(
    case: ValuationCase = ValuationCase.BASE,
    value: str = "42",
) -> ValuationScenarioResult:
    return ValuationScenarioResult(
        scenario_id=f"scenario-{case.value}",
        case=case,
        method=ValuationMethod.DISCOUNTED_CASH_FLOW,
        enterprise_value="1000",
        equity_value="900",
        per_share_value=value,
        currency="USD",
        assumption_ids=("assumption-growth",),
        observation_ids=("obs-revenue",),
        valuation_dependency_ids=("dependency-growth",),
    )


def make_sensitivity(value: str = "40") -> ValuationSensitivityPoint:
    return ValuationSensitivityPoint(
        sensitivity_id="sensitivity-growth-down",
        scenario_id="scenario-base",
        changed_assumption_id="assumption-growth",
        changed_value="0.08",
        resulting_per_share_value=value,
    )


def make_package(
    *,
    observation: ValuationObservation | None = None,
    assumption: ValuationAssumption | None = None,
    scenario: ValuationScenarioResult | None = None,
    sensitivity: ValuationSensitivityPoint | None = None,
    issuer_id: str = "issuer-firecat",
    security_id: str = "security-firecat-common",
) -> ValuationPackage:
    policy = make_policy()
    observation = observation or make_observation()
    assumption = assumption or make_assumption()
    scenario = scenario or make_scenario()
    sensitivity = sensitivity or make_sensitivity()

    provenance = ValuationProvenance(
        thesis_package_identity=DIGEST_B,
        policy_identity=policy.policy_identity,
        observation_identities=(observation.observation_identity,),
        assumption_identities=(assumption.assumption_identity,),
    )

    return ValuationPackage(
        package_version="1",
        policy_identity=policy.policy_identity,
        thesis_package_identity=DIGEST_B,
        issuer_id=issuer_id,
        security_id=security_id,
        constructed_at=NOW,
        currency="USD",
        observations=(observation,),
        assumptions=(assumption,),
        scenarios=(scenario,),
        sensitivity_points=(sensitivity,),
        confidence=ValuationConfidenceClassification.MODERATE,
        completeness=ValuationCompletenessClassification.COMPLETE,
        readiness=ValuationReadinessClassification.READY_FOR_REVIEW,
        unavailable_reasons=(),
        provenance=provenance,
        readiness_blockers=(),
        confidence_components=(("input_quality", "moderate"),),
    )


def test_policy_is_deterministic_and_fail_closed() -> None:
    first = make_policy()
    second = make_policy()

    assert first.policy_identity == second.policy_identity
    assert first.permits_method(ValuationMethod.DISCOUNTED_CASH_FLOW)
    assert first.permits_currency("USD")
    assert not first.permits_currency("EUR")


def test_policy_rejects_invalid_configuration() -> None:
    with pytest.raises(
        FinancialDataValidationError,
        match="at least one valuation method",
    ):
        InvestmentValuationPolicy(allowed_methods=())

    with pytest.raises(
        FinancialDataValidationError,
        match="unsupported required valuation case",
    ):
        InvestmentValuationPolicy(required_cases=("optimistic",))


def test_models_generate_deterministic_identities() -> None:
    assert make_observation().observation_identity == make_observation().observation_identity
    assert make_assumption().assumption_identity == make_assumption().assumption_identity
    assert make_scenario().scenario_identity == make_scenario().scenario_identity
    assert make_sensitivity().sensitivity_identity == make_sensitivity().sensitivity_identity


def test_identity_changes_when_material_input_changes() -> None:
    assert (
        make_observation("100").observation_identity != make_observation("101").observation_identity
    )
    assert (
        make_assumption("0.10").assumption_identity != make_assumption("0.11").assumption_identity
    )
    assert (
        make_scenario(value="42").scenario_identity != make_scenario(value="43").scenario_identity
    )


def test_package_is_immutable_and_auditable() -> None:
    package = make_package()

    assert verify_package_identity(package)

    with pytest.raises(FrozenInstanceError):
        package.currency = "EUR"  # type: ignore[misc]


def test_package_rejects_provenance_mismatch() -> None:
    package = make_package()
    bad_provenance = replace(
        package.provenance,
        thesis_package_identity=DIGEST_A,
        provenance_identity="",
    )

    with pytest.raises(
        FinancialDataValidationError,
        match="valuation provenance mismatch",
    ):
        replace(
            package,
            provenance=bad_provenance,
            package_identity="",
        )


def test_comparison_detects_changed_observation() -> None:
    before = make_package()
    after = make_package(observation=make_observation("125"))

    comparison = compare_valuation_packages(before, after)

    changes = dict(comparison.changes)
    assert changes["changed_observations"] == ("obs-revenue",)
    assert comparison.before_identity == before.package_identity
    assert comparison.after_identity == after.package_identity


def test_comparison_rejects_different_security() -> None:
    before = make_package()
    after = make_package(security_id="security-other")

    with pytest.raises(
        FinancialDataValidationError,
        match="same security",
    ):
        compare_valuation_packages(before, after)


def test_unavailable_scenario_cannot_contain_calculated_values() -> None:
    from sigil.valuations import ValuationUnavailableReason

    with pytest.raises(
        FinancialDataValidationError,
        match="cannot contain calculated values",
    ):
        ValuationScenarioResult(
            scenario_id="scenario-unavailable",
            case=ValuationCase.BASE,
            method=ValuationMethod.DISCOUNTED_CASH_FLOW,
            enterprise_value="100",
            equity_value=None,
            per_share_value=None,
            currency="USD",
            assumption_ids=("assumption-growth",),
            observation_ids=("obs-revenue",),
            valuation_dependency_ids=("dependency-growth",),
            unavailable_reasons=(ValuationUnavailableReason.MISSING_REQUIRED_ASSUMPTION,),
        )


def make_dcf_request(
    *,
    policy: InvestmentValuationPolicy | None = None,
    base_free_cash_flow: str = "100",
    diluted_shares: str = "50",
    base_discount_rate: str = "0.10",
    base_terminal_growth_rate: str = "0.03",
):
    from sigil.valuations import DiscountedCashFlowInput

    policy = policy or InvestmentValuationPolicy()

    return DiscountedCashFlowInput(
        issuer_id="issuer-firecat",
        security_id="security-firecat-common",
        thesis_package_identity=DIGEST_B,
        source_identity=DIGEST_A,
        policy_identity=policy.policy_identity,
        as_of=NOW,
        currency="USD",
        base_free_cash_flow=base_free_cash_flow,
        diluted_shares=diluted_shares,
        net_debt="100",
        bear_growth_rate="0.02",
        base_growth_rate="0.05",
        bull_growth_rate="0.08",
        bear_discount_rate="0.12",
        base_discount_rate=base_discount_rate,
        bull_discount_rate="0.09",
        bear_terminal_growth_rate="0.01",
        base_terminal_growth_rate=base_terminal_growth_rate,
        bull_terminal_growth_rate="0.04",
        forecast_years=5,
        sensitivity_rate_delta="0.01",
    )


def test_dcf_request_identity_is_deterministic() -> None:
    assert make_dcf_request().request_identity == make_dcf_request().request_identity


def test_dcf_engine_constructs_complete_package() -> None:
    from sigil.valuations import construct_discounted_cash_flow_valuation

    package = construct_discounted_cash_flow_valuation(make_dcf_request())

    assert len(package.observations) == 3
    assert len(package.assumptions) == 9
    assert len(package.scenarios) == 3
    assert len(package.sensitivity_points) == 2
    assert package.readiness is ValuationReadinessClassification.READY_FOR_REVIEW
    assert package.completeness is ValuationCompletenessClassification.COMPLETE
    assert verify_package_identity(package)


def test_dcf_engine_is_deterministic() -> None:
    from sigil.valuations import construct_discounted_cash_flow_valuation

    first = construct_discounted_cash_flow_valuation(make_dcf_request())
    second = construct_discounted_cash_flow_valuation(make_dcf_request())

    assert first.package_identity == second.package_identity


def test_dcf_scenarios_are_ordered_bear_base_bull() -> None:
    from decimal import Decimal

    from sigil.valuations import construct_discounted_cash_flow_valuation

    package = construct_discounted_cash_flow_valuation(make_dcf_request())
    values = {scenario.case: Decimal(scenario.per_share_value) for scenario in package.scenarios}

    assert values[ValuationCase.BEAR] < values[ValuationCase.BASE]
    assert values[ValuationCase.BASE] < values[ValuationCase.BULL]


def test_dcf_engine_rejects_nonpositive_shares() -> None:
    from sigil.valuations import construct_discounted_cash_flow_valuation

    with pytest.raises(
        FinancialDataValidationError,
        match="diluted_shares must be greater than zero",
    ):
        construct_discounted_cash_flow_valuation(make_dcf_request(diluted_shares="0"))


def test_dcf_engine_rejects_policy_identity_mismatch() -> None:
    from sigil.valuations import construct_discounted_cash_flow_valuation

    request = make_dcf_request()
    different_policy = InvestmentValuationPolicy(maximum_terminal_growth_rate="0.04")

    with pytest.raises(
        FinancialDataValidationError,
        match="policy identity mismatch",
    ):
        construct_discounted_cash_flow_valuation(
            request,
            policy=different_policy,
        )


def test_dcf_engine_rejects_terminal_growth_above_discount_rate() -> None:
    from sigil.valuations import construct_discounted_cash_flow_valuation

    with pytest.raises(
        FinancialDataValidationError,
        match="discount rate must exceed terminal growth rate",
    ):
        construct_discounted_cash_flow_valuation(
            make_dcf_request(
                base_discount_rate="0.03",
                base_terminal_growth_rate="0.03",
            )
        )
