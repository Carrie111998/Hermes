"""Read-only audit helpers for immutable valuation packages."""

from __future__ import annotations

from sigil.accounting.models import canonical_digest

from .models import ValuationPackage


def verify_package_identity(package: ValuationPackage) -> bool:
    material = {
        field: getattr(package, field)
        for field in package.__dataclass_fields__
        if field != "package_identity"
    }
    return canonical_digest(material) == package.package_identity


def list_observations(package: ValuationPackage):
    return package.observations


def list_assumptions(package: ValuationPackage):
    return package.assumptions


def list_scenarios(package: ValuationPackage):
    return package.scenarios


def list_sensitivity_points(package: ValuationPackage):
    return package.sensitivity_points


def list_readiness_blockers(package: ValuationPackage) -> tuple[str, ...]:
    return package.readiness_blockers


def confidence_component_summary(
    package: ValuationPackage,
) -> tuple[tuple[str, str], ...]:
    return package.confidence_components


def inspect_provenance(package: ValuationPackage):
    return package.provenance
