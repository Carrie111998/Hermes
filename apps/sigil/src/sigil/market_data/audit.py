"""Read-only audit helpers for governed market-data packages."""

from __future__ import annotations

from sigil.accounting.models import canonical_digest

from .models import GovernedMarketDataPackage


def verify_package_identity(package: GovernedMarketDataPackage) -> bool:
    material = {
        field: getattr(package, field)
        for field in package.__dataclass_fields__
        if field != "package_identity"
    }
    return canonical_digest(material) == package.package_identity


def list_observations(
    package: GovernedMarketDataPackage,
):
    return package.observations


def list_sources(package: GovernedMarketDataPackage) -> tuple[str, ...]:
    return package.provenance.source_ids


def list_quality_reasons(package: GovernedMarketDataPackage) -> tuple[str, ...]:
    return package.quality_reasons


def list_readiness_blockers(
    package: GovernedMarketDataPackage,
) -> tuple[str, ...]:
    return package.readiness_blockers


def inspect_provenance(package: GovernedMarketDataPackage):
    return package.provenance
