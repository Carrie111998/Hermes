"""Deterministic comparison of corporate-action packages."""

from __future__ import annotations

from .models import (
    CorporateActionsComparison,
    CorporateActionValidationError,
    GovernedCorporateActionsPackage,
)


def compare_corporate_actions_packages(
    before: GovernedCorporateActionsPackage,
    after: GovernedCorporateActionsPackage,
) -> CorporateActionsComparison:
    if before.instrument_id != after.instrument_id:
        raise CorporateActionValidationError(
            "corporate-action comparison requires the same instrument"
        )

    old = {item.action_id: item.event_identity for item in before.events}
    new = {item.action_id: item.event_identity for item in after.events}

    return CorporateActionsComparison(
        before_identity=before.package_identity,
        after_identity=after.package_identity,
        added_action_ids=tuple(sorted(new.keys() - old.keys())),
        removed_action_ids=tuple(sorted(old.keys() - new.keys())),
        changed_action_ids=tuple(
            sorted(
                key
                for key in old.keys() & new.keys()
                if old[key] != new[key]
            )
        ),
        quality_change=(
            None
            if before.quality == after.quality
            else (before.quality.value, after.quality.value)
        ),
        disposition_change=(
            None
            if before.disposition == after.disposition
            else (
                before.disposition.value,
                after.disposition.value,
            )
        ),
    )
