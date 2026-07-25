"""Deterministic governed corporate-action normalization."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .input import GovernedCorporateActionsInput
from .models import (
    CorporateActionDisposition,
    CorporateActionEvent,
    CorporateActionKind,
    CorporateActionProvenance,
    CorporateActionQuality,
    CorporateActionStatus,
    CorporateActionValidationError,
    GovernedCorporateActionsPackage,
    PositionAdjustmentInstruction,
)
from .policy import GovernedCorporateActionsPolicy


_RATIO_KINDS = {
    CorporateActionKind.STOCK_DIVIDEND,
    CorporateActionKind.FORWARD_SPLIT,
    CorporateActionKind.REVERSE_SPLIT,
    CorporateActionKind.SPIN_OFF,
    CorporateActionKind.EXCHANGE_OFFER,
    CorporateActionKind.RIGHTS_OFFERING,
}
_CASH_KINDS = {
    CorporateActionKind.CASH_DIVIDEND,
    CorporateActionKind.TENDER_OFFER,
    CorporateActionKind.LIQUIDATION,
}
_TARGET_KINDS = {
    CorporateActionKind.MERGER,
    CorporateActionKind.ACQUISITION,
    CorporateActionKind.SPIN_OFF,
    CorporateActionKind.EXCHANGE_OFFER,
}
_SYMBOL_KINDS = {CorporateActionKind.SYMBOL_CHANGE}


def _epoch_seconds(value: str, name: str) -> int:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CorporateActionValidationError(
            f"{name} must be ISO-8601"
        ) from exc
    if parsed.tzinfo is None:
        raise CorporateActionValidationError(
            f"{name} must include a timezone"
        )
    return int(parsed.astimezone(timezone.utc).timestamp())


def _positive_decimal(value: str | None, name: str) -> Decimal:
    if value is None:
        raise CorporateActionValidationError(f"{name} is required")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise CorporateActionValidationError(
            f"{name} must be numeric"
        ) from exc
    if parsed <= 0:
        raise CorporateActionValidationError(
            f"{name} must be greater than zero"
        )
    return parsed


def _validate_event(
    event: CorporateActionEvent,
    request: GovernedCorporateActionsInput,
    policy: GovernedCorporateActionsPolicy,
) -> None:
    if event.instrument_id != request.instrument_id:
        raise CorporateActionValidationError(
            "event instrument does not match request"
        )
    if not policy.permits_kind(event.kind):
        raise CorporateActionValidationError(
            f"corporate-action kind is not permitted: {event.kind.value}"
        )
    if not policy.permits_source(event.source_id):
        raise CorporateActionValidationError(
            f"corporate-action source is not permitted: {event.source_id}"
        )
    if policy.require_evidence_references and not event.evidence_references:
        raise CorporateActionValidationError(
            "event evidence references are required"
        )

    announced = _epoch_seconds(event.announced_at, "event.announced_at")
    effective = _epoch_seconds(event.effective_at, "event.effective_at")
    if effective < announced:
        raise CorporateActionValidationError(
            "effective_at cannot precede announced_at"
        )
    if announced > request.as_of_epoch_seconds:
        raise CorporateActionValidationError(
            "announced_at cannot be after the request as_of time"
        )

    for name, value in (
        ("record_at", event.record_at),
        ("ex_at", event.ex_at),
        ("payment_at", event.payment_at),
    ):
        if value is not None:
            _epoch_seconds(value, f"event.{name}")

    if event.kind in _RATIO_KINDS and policy.require_ratio_for_ratio_actions:
        _positive_decimal(event.ratio_numerator, "ratio_numerator")
        _positive_decimal(event.ratio_denominator, "ratio_denominator")

    if event.kind in _CASH_KINDS:
        _positive_decimal(event.cash_amount, "cash_amount")
        if policy.require_currency_for_cash_actions and not event.currency:
            raise CorporateActionValidationError(
                "currency is required for cash corporate actions"
            )

    if event.kind in _TARGET_KINDS and not event.target_instrument_id:
        raise CorporateActionValidationError(
            "target_instrument_id is required for this corporate action"
        )

    if event.kind in _SYMBOL_KINDS and not event.new_symbol:
        raise CorporateActionValidationError(
            "new_symbol is required for symbol changes"
        )


def _conflict_material(
    event: CorporateActionEvent,
) -> tuple[object, ...]:
    """Return the economic terms used to detect conflicting events."""

    return (
        event.instrument_id,
        event.kind.value,
        event.status.value,
        event.effective_at,
        event.record_at,
        event.ex_at,
        event.payment_at,
        event.target_instrument_id,
        event.cash_amount,
        event.currency,
        event.ratio_numerator,
        event.ratio_denominator,
        event.new_symbol,
    )


def _conflicts(
    events: tuple[CorporateActionEvent, ...],
) -> tuple[str, ...]:
    groups: dict[tuple[str, str], list[CorporateActionEvent]] = {}
    for event in events:
        if event.status in {
            CorporateActionStatus.CANCELLED,
            CorporateActionStatus.SUPERSEDED,
        }:
            continue
        key = (event.kind.value, event.effective_at)
        groups.setdefault(key, []).append(event)

    conflict_ids: set[str] = set()
    for grouped in groups.values():
        economic_terms = {
            _conflict_material(item)
            for item in grouped
        }
        if len(grouped) > 1 and len(economic_terms) > 1:
            conflict_ids.update(item.action_id for item in grouped)

    return tuple(sorted(conflict_ids))


def _instruction(
    event: CorporateActionEvent,
) -> PositionAdjustmentInstruction | None:
    if event.status in {
        CorporateActionStatus.CANCELLED,
        CorporateActionStatus.SUPERSEDED,
    }:
        return None

    adjustment_type = {
        CorporateActionKind.CASH_DIVIDEND: "cash_entitlement",
        CorporateActionKind.STOCK_DIVIDEND: "share_ratio_adjustment",
        CorporateActionKind.FORWARD_SPLIT: "share_ratio_adjustment",
        CorporateActionKind.REVERSE_SPLIT: "share_ratio_adjustment",
        CorporateActionKind.MERGER: "security_conversion_review",
        CorporateActionKind.ACQUISITION: "security_conversion_review",
        CorporateActionKind.SPIN_OFF: "new_security_entitlement",
        CorporateActionKind.SYMBOL_CHANGE: "identifier_change",
        CorporateActionKind.TENDER_OFFER: "voluntary_election_review",
        CorporateActionKind.EXCHANGE_OFFER: "voluntary_election_review",
        CorporateActionKind.RIGHTS_OFFERING: "rights_entitlement_review",
        CorporateActionKind.DELISTING: "trading_eligibility_review",
        CorporateActionKind.LIQUIDATION: "cash_entitlement",
    }[event.kind]

    return PositionAdjustmentInstruction(
        action_id=event.action_id,
        instrument_id=event.instrument_id,
        kind=event.kind,
        effective_at=event.effective_at,
        adjustment_type=adjustment_type,
        ratio_numerator=event.ratio_numerator,
        ratio_denominator=event.ratio_denominator,
        cash_amount=event.cash_amount,
        currency=event.currency,
        target_instrument_id=event.target_instrument_id,
        new_symbol=event.new_symbol,
    )


def construct_governed_corporate_actions_package(
    request: GovernedCorporateActionsInput,
    policy: GovernedCorporateActionsPolicy,
) -> GovernedCorporateActionsPackage:
    """Normalize explicit events without browsing, trading, or mutation."""

    if request.policy_identity != policy.policy_identity:
        raise CorporateActionValidationError(
            "corporate-action request policy mismatch"
        )

    seen: set[str] = set()
    for event in request.events:
        _validate_event(event, request, policy)
        if policy.reject_duplicate_action_ids:
            if event.action_id in seen:
                raise CorporateActionValidationError(
                    "duplicate action_id is not permitted"
                )
            seen.add(event.action_id)

    events = tuple(
        sorted(
            request.events,
            key=lambda item: (
                item.effective_at,
                item.kind.value,
                item.source_id,
                item.action_id,
            ),
        )
    )
    conflicts = _conflicts(events)
    if conflicts and policy.reject_conflicting_effective_events:
        raise CorporateActionValidationError(
            "conflicting effective corporate actions are not permitted"
        )

    active = tuple(
        event
        for event in events
        if event.status
        not in {
            CorporateActionStatus.CANCELLED,
            CorporateActionStatus.SUPERSEDED,
        }
    )
    distinct_sources = tuple(sorted({event.source_id for event in active}))
    evidence_complete = all(event.evidence_references for event in active)

    reasons: list[str] = []
    blockers: list[str] = []

    if conflicts:
        reasons.append(
            "conflicting corporate actions: " + ", ".join(conflicts)
        )
        blockers.extend(f"conflicting-action:{item}" for item in conflicts)

    if not active:
        reasons.append("no active corporate actions")
        blockers.append("no-active-corporate-actions")

    if active and len(distinct_sources) == 1:
        reasons.append("single-source corporate-action set")

    if not evidence_complete:
        reasons.append("incomplete evidence references")
        blockers.append("incomplete-evidence")

    if conflicts or not active or not evidence_complete:
        quality = CorporateActionQuality.REJECTED
        disposition = CorporateActionDisposition.BLOCKED
    elif len(distinct_sources) >= 2:
        quality = CorporateActionQuality.VERIFIED
        disposition = CorporateActionDisposition.REVIEW_REQUIRED
    else:
        quality = CorporateActionQuality.ACCEPTABLE
        disposition = CorporateActionDisposition.REVIEW_REQUIRED

    instructions = tuple(
        instruction
        for event in events
        if (instruction := _instruction(event)) is not None
    )

    provenance = CorporateActionProvenance(
        request_identity=request.request_identity,
        policy_identity=policy.policy_identity,
        source_ids=distinct_sources,
        input_event_identities=tuple(
            event.event_identity for event in events
        ),
        upstream_package_identities=request.upstream_package_identities,
    )

    return GovernedCorporateActionsPackage(
        instrument_id=request.instrument_id,
        as_of=request.as_of,
        events=events,
        adjustment_instructions=instructions,
        quality=quality,
        disposition=disposition,
        quality_reasons=tuple(reasons),
        readiness_blockers=tuple(sorted(blockers)),
        conflict_action_ids=conflicts,
        provenance=provenance,
    )
