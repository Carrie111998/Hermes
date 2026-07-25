from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from .audit import deterministic_identifier
from .live_order_admission import LiveOrderAdmission, LiveOrderAdmissionStatus


class LiveExecutionHandoffStatus(StrEnum):
    REJECTED = "rejected"
    READY = "ready"
    DUPLICATE = "duplicate"
    EXPIRED = "expired"


def _deduplicate(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({value.strip() for value in values if value.strip()}))


@dataclass(frozen=True, slots=True)
class LiveExecutionHandoffPolicy:
    maximum_admission_age_seconds: int = 15
    require_operator_authorization: bool = True
    require_evidence: bool = True
    reject_duplicate_admission_ids: bool = True

    def __post_init__(self) -> None:
        if self.maximum_admission_age_seconds < 0:
            raise ValueError("maximum_admission_age_seconds must be non-negative")


@dataclass(frozen=True, slots=True)
class LiveExecutionHandoffRequest:
    request_id: str
    execution_adapter_name: str
    execution_environment: str
    admission_evaluated_at_epoch: int
    requested_at_epoch: int
    operator_identity: str
    operator_authorization_reference: str
    policy_version: str
    evidence_references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "request_id",
            "execution_adapter_name",
            "execution_environment",
            "operator_identity",
            "policy_version",
        ):
            value = getattr(self, field_name).strip()
            if not value:
                raise ValueError(f"{field_name} must not be empty")
            object.__setattr__(self, field_name, value)

        object.__setattr__(
            self,
            "operator_authorization_reference",
            self.operator_authorization_reference.strip(),
        )
        object.__setattr__(
            self,
            "evidence_references",
            _deduplicate(self.evidence_references),
        )
        object.__setattr__(
            self,
            "execution_environment",
            self.execution_environment.lower(),
        )

        if self.execution_environment != "live":
            raise ValueError("execution_environment must be live")
        if self.admission_evaluated_at_epoch < 0:
            raise ValueError("admission_evaluated_at_epoch must be non-negative")
        if self.requested_at_epoch < 0:
            raise ValueError("requested_at_epoch must be non-negative")


@dataclass(frozen=True, slots=True)
class LiveExecutionEnvelope:
    envelope_id: str
    admission_id: str
    client_order_id: str
    broker_name: str
    account_identifier: str
    asset_class: str
    symbol: str
    order_type: str
    side: str
    quantity: object
    limit_price: object | None
    estimated_notional: object
    execution_adapter_name: str
    execution_environment: str
    operator_identity: str
    operator_authorization_reference: str
    policy_version: str
    evidence_references: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LiveExecutionHandoff:
    handoff_id: str
    request_id: str
    admission_id: str
    status: LiveExecutionHandoffStatus
    admission_age_seconds: int
    envelope: LiveExecutionEnvelope | None
    evidence_references: tuple[str, ...]
    passed_checks: tuple[str, ...]
    failed_checks: tuple[str, ...]


def prepare_live_execution_handoff(
    admission: LiveOrderAdmission,
    request: LiveExecutionHandoffRequest,
    *,
    prior_admission_ids: Iterable[str] = (),
    policy: LiveExecutionHandoffPolicy | None = None,
) -> LiveExecutionHandoff:
    handoff_policy = policy or LiveExecutionHandoffPolicy()
    known_admission_ids = {
        value.strip() for value in prior_admission_ids if value.strip()
    }
    duplicate = admission.admission_id in known_admission_ids
    age = request.requested_at_epoch - request.admission_evaluated_at_epoch
    evidence = _deduplicate(
        (*admission.evidence_references, *request.evidence_references)
    )

    checks = (
        (
            "admission_approved",
            admission.status is LiveOrderAdmissionStatus.ADMITTED,
        ),
        ("admission_not_from_future", age >= 0),
        (
            "admission_fresh",
            0 <= age <= handoff_policy.maximum_admission_age_seconds,
        ),
        (
            "operator_authorization",
            not handoff_policy.require_operator_authorization
            or bool(request.operator_authorization_reference),
        ),
        (
            "authorization_matches_admission",
            request.operator_authorization_reference
            == admission.operator_authorization_reference,
        ),
        (
            "evidence",
            not handoff_policy.require_evidence or bool(evidence),
        ),
        (
            "not_duplicate",
            not handoff_policy.reject_duplicate_admission_ids or not duplicate,
        ),
    )

    passed = tuple(name for name, ok in checks if ok)
    failed = tuple(name for name, ok in checks if not ok)

    if duplicate and handoff_policy.reject_duplicate_admission_ids:
        status = LiveExecutionHandoffStatus.DUPLICATE
    elif age > handoff_policy.maximum_admission_age_seconds:
        status = LiveExecutionHandoffStatus.EXPIRED
    elif failed:
        status = LiveExecutionHandoffStatus.REJECTED
    else:
        status = LiveExecutionHandoffStatus.READY

    envelope = None
    if status is LiveExecutionHandoffStatus.READY:
        envelope_id = deterministic_identifier(
            "live-execution-envelope",
            admission.admission_id,
            admission.client_order_id,
            admission.broker_name,
            admission.account_identifier,
            admission.asset_class,
            admission.symbol,
            admission.order_type,
            admission.side,
            admission.quantity,
            admission.limit_price,
            admission.estimated_notional,
            request.execution_adapter_name,
            request.execution_environment,
            request.operator_identity,
            request.operator_authorization_reference,
            request.policy_version,
            *evidence,
        )
        envelope = LiveExecutionEnvelope(
            envelope_id=envelope_id,
            admission_id=admission.admission_id,
            client_order_id=admission.client_order_id,
            broker_name=admission.broker_name,
            account_identifier=admission.account_identifier,
            asset_class=admission.asset_class,
            symbol=admission.symbol,
            order_type=admission.order_type,
            side=admission.side,
            quantity=admission.quantity,
            limit_price=admission.limit_price,
            estimated_notional=admission.estimated_notional,
            execution_adapter_name=request.execution_adapter_name,
            execution_environment=request.execution_environment,
            operator_identity=request.operator_identity,
            operator_authorization_reference=(
                request.operator_authorization_reference
            ),
            policy_version=request.policy_version,
            evidence_references=evidence,
        )

    handoff_id = deterministic_identifier(
        "live-execution-handoff",
        admission.admission_id,
        request.request_id,
        request.execution_adapter_name,
        request.execution_environment,
        request.admission_evaluated_at_epoch,
        request.requested_at_epoch,
        request.operator_identity,
        request.operator_authorization_reference,
        request.policy_version,
        status,
        envelope.envelope_id if envelope else "no-envelope",
        *failed,
    )

    return LiveExecutionHandoff(
        handoff_id=handoff_id,
        request_id=request.request_id,
        admission_id=admission.admission_id,
        status=status,
        admission_age_seconds=age,
        envelope=envelope,
        evidence_references=evidence,
        passed_checks=passed,
        failed_checks=failed,
    )
