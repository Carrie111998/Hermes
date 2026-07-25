from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from .audit import deterministic_identifier
from .live_execution_handoff import (
    LiveExecutionHandoff,
    LiveExecutionHandoffStatus,
)


class GovernedBrokerSubmissionStatus(StrEnum):
    REJECTED = "rejected"
    BLOCKED = "blocked"
    SUBMITTED = "submitted"
    BROKER_REJECTED = "broker_rejected"
    OUTCOME_UNCERTAIN = "outcome_uncertain"


class BrokerSubmissionOutcomeUncertainError(RuntimeError):
    """Raised when the broker may have received an order but no result is known."""


@dataclass(frozen=True, slots=True)
class GovernedBrokerSubmissionPolicy:
    maximum_handoff_age_seconds: int = 15
    maximum_launch_notional: Decimal = Decimal(25)
    require_owner_confirmation: bool = True
    require_launch_certification: bool = True
    prohibit_automatic_retry: bool = True

    def __post_init__(self) -> None:
        if self.maximum_handoff_age_seconds < 0:
            raise ValueError("maximum_handoff_age_seconds must be non-negative")
        if self.maximum_launch_notional <= 0:
            raise ValueError("maximum_launch_notional must be positive")
        if not self.prohibit_automatic_retry:
            raise ValueError("automatic broker-submission retry cannot be enabled")


@dataclass(frozen=True, slots=True)
class GovernedBrokerSubmissionRequest:
    request_id: str
    handoff_prepared_at_epoch: int
    requested_at_epoch: int
    owner_identity: str
    owner_confirmation_reference: str
    launch_certification_reference: str
    kill_switch_active: bool
    policy_version: str
    evidence_references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("request_id", "owner_identity", "policy_version"):
            value = getattr(self, field_name).strip()
            if not value:
                raise ValueError(f"{field_name} must not be empty")
            object.__setattr__(self, field_name, value)
        for field_name in (
            "owner_confirmation_reference",
            "launch_certification_reference",
        ):
            object.__setattr__(self, field_name, getattr(self, field_name).strip())
        if self.handoff_prepared_at_epoch < 0 or self.requested_at_epoch < 0:
            raise ValueError("epoch values must be non-negative")
        object.__setattr__(
            self,
            "evidence_references",
            _deduplicate(self.evidence_references),
        )


@dataclass(frozen=True, slots=True)
class BrokerSubmissionResponse:
    accepted: bool
    broker_order_id: str
    broker_status: str
    response_reference: str
    evidence_references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "broker_order_id", self.broker_order_id.strip())
        object.__setattr__(self, "broker_status", self.broker_status.strip().lower())
        object.__setattr__(
            self,
            "response_reference",
            self.response_reference.strip(),
        )
        object.__setattr__(
            self,
            "evidence_references",
            _deduplicate(self.evidence_references),
        )
        if self.accepted and not self.broker_order_id:
            raise ValueError("accepted broker response requires broker_order_id")
        if not self.broker_status:
            raise ValueError("broker_status must not be empty")
        if not self.response_reference:
            raise ValueError("response_reference must not be empty")


@dataclass(frozen=True, slots=True)
class GovernedBrokerSubmissionReceipt:
    receipt_id: str
    request_id: str
    handoff_id: str
    envelope_id: str | None
    status: GovernedBrokerSubmissionStatus
    broker_order_id: str | None
    broker_status: str | None
    response_reference: str | None
    handoff_age_seconds: int
    retry_permitted: bool
    evidence_references: tuple[str, ...]
    passed_checks: tuple[str, ...]
    failed_checks: tuple[str, ...]


BrokerSubmitter = Callable[[object], BrokerSubmissionResponse]


def _deduplicate(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({value.strip() for value in values if value.strip()}))


def submit_governed_broker_order(
    handoff: LiveExecutionHandoff,
    request: GovernedBrokerSubmissionRequest,
    submitter: BrokerSubmitter,
    *,
    prior_envelope_ids: Iterable[str] = (),
    policy: GovernedBrokerSubmissionPolicy | None = None,
) -> GovernedBrokerSubmissionReceipt:
    submission_policy = policy or GovernedBrokerSubmissionPolicy()
    age = request.requested_at_epoch - request.handoff_prepared_at_epoch
    envelope = handoff.envelope
    envelope_id = envelope.envelope_id if envelope else None
    known_envelope_ids = {
        value.strip() for value in prior_envelope_ids if value.strip()
    }
    duplicate = bool(envelope_id and envelope_id in known_envelope_ids)
    notional = (
        Decimal(str(envelope.estimated_notional))
        if envelope is not None
        else Decimal(0)
    )
    evidence = _deduplicate(
        (*handoff.evidence_references, *request.evidence_references)
    )

    checks = (
        ("handoff_ready", handoff.status is LiveExecutionHandoffStatus.READY),
        ("envelope_present", envelope is not None),
        ("handoff_not_from_future", age >= 0),
        (
            "handoff_fresh",
            0 <= age <= submission_policy.maximum_handoff_age_seconds,
        ),
        (
            "owner_confirmation",
            not submission_policy.require_owner_confirmation
            or bool(request.owner_confirmation_reference),
        ),
        (
            "launch_certification",
            not submission_policy.require_launch_certification
            or bool(request.launch_certification_reference),
        ),
        ("kill_switch_clear", not request.kill_switch_active),
        (
            "launch_notional_within_limit",
            Decimal(0) < notional <= submission_policy.maximum_launch_notional,
        ),
        ("one_time_envelope", not duplicate),
        ("evidence_present", bool(evidence)),
    )
    passed = tuple(name for name, ok in checks if ok)
    failed = tuple(name for name, ok in checks if not ok)

    response: BrokerSubmissionResponse | None = None
    if failed:
        status = (
            GovernedBrokerSubmissionStatus.BLOCKED
            if request.kill_switch_active or duplicate
            else GovernedBrokerSubmissionStatus.REJECTED
        )
    else:
        try:
            response = submitter(envelope)
        except BrokerSubmissionOutcomeUncertainError:
            status = GovernedBrokerSubmissionStatus.OUTCOME_UNCERTAIN
        else:
            status = (
                GovernedBrokerSubmissionStatus.SUBMITTED
                if response.accepted
                else GovernedBrokerSubmissionStatus.BROKER_REJECTED
            )

    response_evidence = response.evidence_references if response else ()
    all_evidence = _deduplicate((*evidence, *response_evidence))
    broker_order_id = response.broker_order_id or None if response else None
    broker_status = response.broker_status if response else None
    response_reference = response.response_reference if response else None
    retry_permitted = False

    receipt_id = deterministic_identifier(
        "governed-broker-submission-receipt",
        request.request_id,
        handoff.handoff_id,
        envelope_id or "no-envelope",
        status,
        broker_order_id or "no-broker-order",
        broker_status or "no-broker-status",
        response_reference or "no-response",
        request.owner_identity,
        request.owner_confirmation_reference,
        request.launch_certification_reference,
        request.policy_version,
        request.requested_at_epoch,
        *failed,
        *all_evidence,
    )

    return GovernedBrokerSubmissionReceipt(
        receipt_id=receipt_id,
        request_id=request.request_id,
        handoff_id=handoff.handoff_id,
        envelope_id=envelope_id,
        status=status,
        broker_order_id=broker_order_id,
        broker_status=broker_status,
        response_reference=response_reference,
        handoff_age_seconds=age,
        retry_permitted=retry_permitted,
        evidence_references=all_evidence,
        passed_checks=passed,
        failed_checks=failed,
    )
