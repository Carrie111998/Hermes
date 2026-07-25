"""Deterministic, non-directional valuation package comparison."""

from __future__ import annotations

from sigil.integrations.providers.models import FinancialDataValidationError

from .models import ValuationComparison, ValuationPackage


def _changes(
    before: tuple[object, ...],
    after: tuple[object, ...],
    id_field: str,
    identity_field: str,
    prefix: str,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    old = {getattr(item, id_field): getattr(item, identity_field) for item in before}
    new = {getattr(item, id_field): getattr(item, identity_field) for item in after}

    return (
        (f"added_{prefix}", tuple(sorted(new.keys() - old.keys()))),
        (f"removed_{prefix}", tuple(sorted(old.keys() - new.keys()))),
        (
            f"changed_{prefix}",
            tuple(sorted(key for key in old.keys() & new.keys() if old[key] != new[key])),
        ),
    )


def compare_valuation_packages(
    before: ValuationPackage,
    after: ValuationPackage,
) -> ValuationComparison:
    if before.issuer_id != after.issuer_id:
        raise FinancialDataValidationError("valuation comparison requires the same issuer")
    if before.security_id != after.security_id:
        raise FinancialDataValidationError("valuation comparison requires the same security")

    changes: list[tuple[str, tuple[str, ...]]] = []

    groups = (
        _changes(
            before.observations,
            after.observations,
            "observation_id",
            "observation_identity",
            "observations",
        ),
        _changes(
            before.assumptions,
            after.assumptions,
            "assumption_id",
            "assumption_identity",
            "assumptions",
        ),
        _changes(
            before.scenarios,
            after.scenarios,
            "scenario_id",
            "scenario_identity",
            "scenarios",
        ),
        _changes(
            before.sensitivity_points,
            after.sensitivity_points,
            "sensitivity_id",
            "sensitivity_identity",
            "sensitivity_points",
        ),
    )

    for group in groups:
        changes.extend(group)

    return ValuationComparison(
        before_identity=before.package_identity,
        after_identity=after.package_identity,
        changes=tuple(changes),
        confidence_change=(
            None
            if before.confidence == after.confidence
            else (before.confidence.value, after.confidence.value)
        ),
        completeness_change=(
            None
            if before.completeness == after.completeness
            else (before.completeness.value, after.completeness.value)
        ),
        readiness_change=(
            None
            if before.readiness == after.readiness
            else (before.readiness.value, after.readiness.value)
        ),
    )
