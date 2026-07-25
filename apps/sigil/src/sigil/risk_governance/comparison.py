"""Deterministic comparison of governed risk packages."""

from __future__ import annotations

from .models import GovernedRiskPackage, RiskComparison


def compare_risk_packages(
    before: GovernedRiskPackage,
    after: GovernedRiskPackage,
) -> RiskComparison:
    before_breaches = set(before.breached_limits)
    after_breaches = set(after.breached_limits)

    severity_change = None
    if before.severity is not after.severity:
        severity_change = (before.severity.value, after.severity.value)

    disposition_change = None
    if before.disposition is not after.disposition:
        disposition_change = (
            before.disposition.value,
            after.disposition.value,
        )

    return RiskComparison(
        before_identity=before.package_identity,
        after_identity=after.package_identity,
        risk_score_change=(before.risk_score, after.risk_score),
        severity_change=severity_change,
        disposition_change=disposition_change,
        added_breaches=tuple(sorted(after_breaches - before_breaches)),
        resolved_breaches=tuple(sorted(before_breaches - after_breaches)),
    )
