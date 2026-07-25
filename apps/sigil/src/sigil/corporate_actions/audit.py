"""Read-only audit helpers for corporate-action packages."""

from __future__ import annotations

from sigil.accounting.models import canonical_digest

from .models import GovernedCorporateActionsPackage


def verify_package_identity(
    package: GovernedCorporateActionsPackage,
) -> bool:
    material = {
        field: getattr(package, field)
        for field in package.__dataclass_fields__
        if field != "package_identity"
    }
    return canonical_digest(material) == package.package_identity


def list_events(package: GovernedCorporateActionsPackage):
    return package.events


def list_adjustment_instructions(
    package: GovernedCorporateActionsPackage,
):
    return package.adjustment_instructions


def list_sources(
    package: GovernedCorporateActionsPackage,
) -> tuple[str, ...]:
    return package.provenance.source_ids


def list_conflicts(
    package: GovernedCorporateActionsPackage,
) -> tuple[str, ...]:
    return package.conflict_action_ids


def list_quality_reasons(
    package: GovernedCorporateActionsPackage,
) -> tuple[str, ...]:
    return package.quality_reasons


def list_readiness_blockers(
    package: GovernedCorporateActionsPackage,
) -> tuple[str, ...]:
    return package.readiness_blockers


def inspect_provenance(package: GovernedCorporateActionsPackage):
    return package.provenance
