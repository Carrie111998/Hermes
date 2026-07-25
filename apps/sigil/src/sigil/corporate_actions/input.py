"""Caller-supplied governed corporate-action request."""

from __future__ import annotations

from dataclasses import dataclass, field

from sigil.accounting.models import canonical_digest

from .models import CorporateActionEvent, CorporateActionValidationError


@dataclass(frozen=True, slots=True)
class GovernedCorporateActionsInput:
    instrument_id: str
    as_of: str
    as_of_epoch_seconds: int
    events: tuple[CorporateActionEvent, ...]
    policy_identity: str
    upstream_package_identities: tuple[str, ...] = ()
    request_identity: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.instrument_id:
            raise CorporateActionValidationError(
                "instrument_id must not be empty"
            )
        if not self.as_of:
            raise CorporateActionValidationError("as_of must not be empty")
        if self.as_of_epoch_seconds < 0:
            raise CorporateActionValidationError(
                "as_of_epoch_seconds must be nonnegative"
            )
        if not self.events:
            raise CorporateActionValidationError("events must not be empty")
        if not self.policy_identity:
            raise CorporateActionValidationError(
                "policy_identity must not be empty"
            )

        canonical_events = tuple(
            sorted(
                self.events,
                key=lambda item: (
                    item.effective_at,
                    item.kind.value,
                    item.source_id,
                    item.action_id,
                ),
            )
        )
        canonical_upstream = tuple(sorted(set(self.upstream_package_identities)))
        object.__setattr__(
            self,
            "upstream_package_identities",
            canonical_upstream,
        )
        object.__setattr__(
            self,
            "request_identity",
            canonical_digest(
                {
                    "instrument_id": self.instrument_id,
                    "as_of": self.as_of,
                    "as_of_epoch_seconds": self.as_of_epoch_seconds,
                    "events": canonical_events,
                    "policy_identity": self.policy_identity,
                    "upstream_package_identities": canonical_upstream,
                }
            ),
        )
