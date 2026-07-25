"""Governance policy for corporate-action normalization."""

from __future__ import annotations

from dataclasses import dataclass, field

from sigil.accounting.models import canonical_digest

from .models import CorporateActionKind, CorporateActionValidationError


@dataclass(frozen=True, slots=True)
class GovernedCorporateActionsPolicy:
    permitted_kinds: tuple[CorporateActionKind, ...] = tuple(
        CorporateActionKind
    )
    permitted_sources: tuple[str, ...] = ()
    require_evidence_references: bool = True
    reject_duplicate_action_ids: bool = True
    reject_conflicting_effective_events: bool = False
    require_currency_for_cash_actions: bool = True
    require_ratio_for_ratio_actions: bool = True
    policy_identity: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.permitted_kinds:
            raise CorporateActionValidationError(
                "permitted_kinds must not be empty"
            )
        object.__setattr__(
            self,
            "permitted_kinds",
            tuple(sorted(set(self.permitted_kinds), key=lambda item: item.value)),
        )
        object.__setattr__(
            self,
            "permitted_sources",
            tuple(sorted(set(self.permitted_sources))),
        )
        object.__setattr__(
            self,
            "policy_identity",
            canonical_digest(
                {
                    name: getattr(self, name)
                    for name in self.__dataclass_fields__
                    if name != "policy_identity"
                }
            ),
        )

    def permits_kind(self, kind: CorporateActionKind) -> bool:
        return kind in self.permitted_kinds

    def permits_source(self, source_id: str) -> bool:
        return not self.permitted_sources or source_id in self.permitted_sources
