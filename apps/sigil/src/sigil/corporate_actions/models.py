"""Immutable models for governed corporate-action processing."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from sigil.accounting.models import canonical_digest


class CorporateActionValidationError(ValueError):
    """Raised when corporate-action input violates governance requirements."""


class CorporateActionKind(str, Enum):
    CASH_DIVIDEND = "cash_dividend"
    STOCK_DIVIDEND = "stock_dividend"
    FORWARD_SPLIT = "forward_split"
    REVERSE_SPLIT = "reverse_split"
    MERGER = "merger"
    ACQUISITION = "acquisition"
    SPIN_OFF = "spin_off"
    SYMBOL_CHANGE = "symbol_change"
    TENDER_OFFER = "tender_offer"
    EXCHANGE_OFFER = "exchange_offer"
    RIGHTS_OFFERING = "rights_offering"
    DELISTING = "delisting"
    LIQUIDATION = "liquidation"


class CorporateActionStatus(str, Enum):
    ANNOUNCED = "announced"
    PENDING = "pending"
    EFFECTIVE = "effective"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class CorporateActionQuality(str, Enum):
    VERIFIED = "verified"
    ACCEPTABLE = "acceptable"
    DEGRADED = "degraded"
    REJECTED = "rejected"


class CorporateActionDisposition(str, Enum):
    INFORMATIONAL = "informational"
    REVIEW_REQUIRED = "review_required"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class CorporateActionEvent:
    action_id: str
    instrument_id: str
    kind: CorporateActionKind
    status: CorporateActionStatus
    announced_at: str
    effective_at: str
    source_id: str
    evidence_references: tuple[str, ...]
    record_at: str | None = None
    ex_at: str | None = None
    payment_at: str | None = None
    target_instrument_id: str | None = None
    cash_amount: str | None = None
    currency: str | None = None
    ratio_numerator: str | None = None
    ratio_denominator: str | None = None
    new_symbol: str | None = None
    notes: tuple[str, ...] = ()
    event_identity: str = field(init=False)

    def __post_init__(self) -> None:
        required_text = {
            "action_id": self.action_id,
            "instrument_id": self.instrument_id,
            "announced_at": self.announced_at,
            "effective_at": self.effective_at,
            "source_id": self.source_id,
        }
        for name, value in required_text.items():
            if not value:
                raise CorporateActionValidationError(f"{name} must not be empty")

        if not isinstance(self.kind, CorporateActionKind):
            raise CorporateActionValidationError("kind must be CorporateActionKind")
        if not isinstance(self.status, CorporateActionStatus):
            raise CorporateActionValidationError(
                "status must be CorporateActionStatus"
            )

        object.__setattr__(
            self,
            "evidence_references",
            tuple(sorted(set(self.evidence_references))),
        )
        object.__setattr__(self, "notes", tuple(self.notes))
        object.__setattr__(
            self,
            "event_identity",
            canonical_digest(
                {
                    name: getattr(self, name)
                    for name in self.__dataclass_fields__
                    if name != "event_identity"
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class CorporateActionProvenance:
    request_identity: str
    policy_identity: str
    source_ids: tuple[str, ...]
    input_event_identities: tuple[str, ...]
    upstream_package_identities: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PositionAdjustmentInstruction:
    action_id: str
    instrument_id: str
    kind: CorporateActionKind
    effective_at: str
    adjustment_type: str
    ratio_numerator: str | None = None
    ratio_denominator: str | None = None
    cash_amount: str | None = None
    currency: str | None = None
    target_instrument_id: str | None = None
    new_symbol: str | None = None
    analytical_only: bool = True
    requires_human_review: bool = True


@dataclass(frozen=True, slots=True)
class GovernedCorporateActionsPackage:
    instrument_id: str
    as_of: str
    events: tuple[CorporateActionEvent, ...]
    adjustment_instructions: tuple[PositionAdjustmentInstruction, ...]
    quality: CorporateActionQuality
    disposition: CorporateActionDisposition
    quality_reasons: tuple[str, ...]
    readiness_blockers: tuple[str, ...]
    conflict_action_ids: tuple[str, ...]
    provenance: CorporateActionProvenance
    analytical_only: bool = True
    authorizes_trading: bool = False
    mutates_positions: bool = False
    package_identity: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "package_identity",
            canonical_digest(
                {
                    name: getattr(self, name)
                    for name in self.__dataclass_fields__
                    if name != "package_identity"
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class CorporateActionsComparison:
    before_identity: str
    after_identity: str
    added_action_ids: tuple[str, ...]
    removed_action_ids: tuple[str, ...]
    changed_action_ids: tuple[str, ...]
    quality_change: tuple[str, str] | None
    disposition_change: tuple[str, str] | None


def event_material(event: CorporateActionEvent) -> dict[str, Any]:
    """Return canonical event material for deterministic inspection."""

    return {
        name: getattr(event, name)
        for name in event.__dataclass_fields__
        if name != "event_identity"
    }
