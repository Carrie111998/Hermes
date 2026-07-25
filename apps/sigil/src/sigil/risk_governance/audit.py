"""Read-only audit helpers for governed risk packages."""

from __future__ import annotations

from sigil.accounting.models import canonical_digest

from .models import GovernedRiskPackage


def verify_package_identity(package: GovernedRiskPackage) -> bool:
    expected = canonical_digest(
        {
            name: getattr(package, name)
            for name in package.__dataclass_fields__
            if name != "package_identity"
        }
    )
    return expected == package.package_identity


def breached_metric_ids(package: GovernedRiskPackage) -> tuple[str, ...]:
    return tuple(
        f"{metric.kind.value}:{metric.subject_id}"
        for metric in package.metrics
        if metric.breached
    )


def package_is_analytical_only(package: GovernedRiskPackage) -> bool:
    return (
        package.analytical_only
        and not package.authorizes_trading
        and not package.authorizes_capital_allocation
        and not package.mutates_positions
        and not package.submits_orders
    )
