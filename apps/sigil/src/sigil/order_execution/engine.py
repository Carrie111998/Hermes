from __future__ import annotations

from dataclasses import dataclass

from .adapter import (
    ExecutionAdapter,
    ExecutionAdapterError,
    SubmissionOutcomeUncertainError,
)
from .audit import build_audit_event, deterministic_identifier
from .input import ExecutionAdmission
from .models import (
    AuditEventType,
    ExecutionAuditEvent,
    ExecutionLifecycleStatus,
    SubmissionAcknowledgement,
    SubmissionAdmissionStatus,
    SubmissionRequest,
)


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    lifecycle_status: ExecutionLifecycleStatus
    requests: tuple[SubmissionRequest, ...]
    acknowledgements: tuple[SubmissionAcknowledgement, ...]
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    audit_trail: tuple[ExecutionAuditEvent, ...]

    @property
    def submitted(self) -> bool:
        return bool(self.acknowledgements)


def build_submission_requests(
    admission: ExecutionAdmission,
) -> tuple[SubmissionRequest, ...]:
    if admission.status is not SubmissionAdmissionStatus.READY:
        return ()

    requests: list[SubmissionRequest] = []

    for approved_order in admission.approved_orders:
        client_order_id = deterministic_identifier(
            "sigil-order",
            admission.package.package_id,
            admission.approval_request.request_id,
            approved_order.intent_id,
            admission.context.provider,
            admission.context.account_id,
            admission.context.environment,
        )

        request_id = deterministic_identifier(
            "submission-request",
            client_order_id,
            approved_order,
        )

        requests.append(
            SubmissionRequest(
                request_id=request_id,
                client_order_id=client_order_id,
                source_intent_id=approved_order.intent_id,
                source_order_intent_package_id=(
                    admission.package.package_id
                ),
                source_approval_request_id=(
                    admission.approval_request.request_id
                ),
                source_approval_record_id=_approval_record_id(
                    admission.approval_record
                ),
                provider=admission.context.provider,
                account_id=admission.context.account_id,
                environment=admission.context.environment,
                symbol=approved_order.symbol,
                side=approved_order.side,
                order_type=approved_order.order_type,
                time_in_force=approved_order.time_in_force,
                quantity=approved_order.quantity,
                reference_price=approved_order.reference_price,
                notional=approved_order.notional,
                limit_price=approved_order.limit_price,
                created_at=admission.context.requested_at,
                evidence_references=tuple(
                    sorted(
                        set(
                            admission.evidence_references
                            + approved_order.evidence_references
                        )
                    )
                ),
            )
        )

    return tuple(
        sorted(
            requests,
            key=lambda item: item.client_order_id,
        )
    )


def execute_admitted_orders(
    admission: ExecutionAdmission,
    adapter: ExecutionAdapter,
) -> SubmissionResult:
    audit: list[ExecutionAuditEvent] = []

    audit.append(
        build_audit_event(
            event_type=AuditEventType.ADMISSION_EVALUATED,
            occurred_at=admission.context.requested_at,
            message=(
                "Execution admission evaluated with status "
                f"{admission.status.value}"
            ),
            source_references=(
                admission.package.package_id,
                admission.approval_request.request_id,
            ),
            evidence_references=admission.evidence_references,
            identity_components=(
                admission.status,
                admission.blockers,
            ),
        )
    )

    if admission.status is not SubmissionAdmissionStatus.READY:
        audit.append(
            build_audit_event(
                event_type=AuditEventType.SUBMISSION_BLOCKED,
                occurred_at=admission.context.requested_at,
                message="Execution submission blocked by admission policy",
                source_references=(
                    admission.package.package_id,
                    admission.approval_request.request_id,
                ),
                evidence_references=admission.evidence_references,
                identity_components=admission.blockers,
            )
        )

        return SubmissionResult(
            lifecycle_status=ExecutionLifecycleStatus.NOT_SUBMITTED,
            requests=(),
            acknowledgements=(),
            blockers=admission.blockers,
            warnings=admission.warnings,
            audit_trail=tuple(audit),
        )

    normalized_provider = adapter.provider_name.strip().lower()

    if normalized_provider != admission.context.provider:
        blocker = (
            "execution adapter provider does not match admitted provider"
        )

        audit.append(
            build_audit_event(
                event_type=AuditEventType.SUBMISSION_BLOCKED,
                occurred_at=admission.context.requested_at,
                message=blocker,
                source_references=(
                    admission.package.package_id,
                    admission.approval_request.request_id,
                ),
                evidence_references=admission.evidence_references,
                identity_components=(
                    normalized_provider,
                    admission.context.provider,
                ),
            )
        )

        return SubmissionResult(
            lifecycle_status=ExecutionLifecycleStatus.NOT_SUBMITTED,
            requests=(),
            acknowledgements=(),
            blockers=(blocker,),
            warnings=admission.warnings,
            audit_trail=tuple(audit),
        )

    requests = build_submission_requests(admission)

    for request in requests:
        audit.append(
            build_audit_event(
                event_type=AuditEventType.SUBMISSION_REQUEST_CREATED,
                occurred_at=request.created_at,
                message=(
                    "Governed submission request created for "
                    f"{request.symbol}"
                ),
                source_references=(
                    request.request_id,
                    request.client_order_id,
                    request.source_intent_id,
                ),
                evidence_references=request.evidence_references,
                identity_components=(request,),
            )
        )

    acknowledgements: list[SubmissionAcknowledgement] = []
    blockers: list[str] = []
    warnings: list[str] = list(admission.warnings)
    uncertain = False

    for request in requests:
        audit.append(
            build_audit_event(
                event_type=AuditEventType.SUBMISSION_ATTEMPTED,
                occurred_at=request.created_at,
                message=(
                    "Governed submission attempted for "
                    f"{request.symbol}"
                ),
                source_references=(
                    request.request_id,
                    request.client_order_id,
                ),
                evidence_references=request.evidence_references,
                identity_components=(request.client_order_id,),
            )
        )

        try:
            acknowledgement = adapter.submit_order(request)

        except SubmissionOutcomeUncertainError as exc:
            uncertain = True
            blockers.append(
                f"{request.client_order_id}: submission outcome uncertain"
            )

            audit.append(
                build_audit_event(
                    event_type=(
                        AuditEventType.SUBMISSION_OUTCOME_UNCERTAIN
                    ),
                    occurred_at=request.created_at,
                    message=str(exc).strip()
                    or "Provider submission outcome is uncertain",
                    source_references=(
                        request.request_id,
                        request.client_order_id,
                    ),
                    evidence_references=request.evidence_references,
                    identity_components=(
                        request.client_order_id,
                        type(exc).__name__,
                    ),
                )
            )
            continue

        except ExecutionAdapterError as exc:
            blockers.append(
                f"{request.client_order_id}: adapter submission failed"
            )

            audit.append(
                build_audit_event(
                    event_type=AuditEventType.SUBMISSION_BLOCKED,
                    occurred_at=request.created_at,
                    message=str(exc).strip()
                    or "Execution adapter submission failed",
                    source_references=(
                        request.request_id,
                        request.client_order_id,
                    ),
                    evidence_references=request.evidence_references,
                    identity_components=(
                        request.client_order_id,
                        type(exc).__name__,
                    ),
                )
            )
            continue

        if acknowledgement.request_id != request.request_id:
            blockers.append(
                f"{request.client_order_id}: acknowledgement request "
                "identifier mismatch"
            )
            continue

        if acknowledgement.client_order_id != request.client_order_id:
            blockers.append(
                f"{request.client_order_id}: acknowledgement client "
                "order identifier mismatch"
            )
            continue

        acknowledgements.append(acknowledgement)

        audit.append(
            build_audit_event(
                event_type=AuditEventType.ACKNOWLEDGEMENT_RECEIVED,
                occurred_at=acknowledgement.acknowledged_at,
                message=(
                    "Provider acknowledgement received with status "
                    f"{acknowledgement.status.value}"
                ),
                source_references=(
                    acknowledgement.acknowledgement_id,
                    acknowledgement.request_id,
                    acknowledgement.client_order_id,
                ),
                evidence_references=(
                    acknowledgement.evidence_references
                ),
                identity_components=(acknowledgement,),
            )
        )

    if uncertain:
        lifecycle_status = ExecutionLifecycleStatus.UNCERTAIN
    elif blockers and not acknowledgements:
        lifecycle_status = ExecutionLifecycleStatus.FAILED
    elif acknowledgements:
        lifecycle_status = (
            ExecutionLifecycleStatus.AWAITING_RECONCILIATION
        )
    else:
        lifecycle_status = ExecutionLifecycleStatus.NOT_SUBMITTED

    return SubmissionResult(
        lifecycle_status=lifecycle_status,
        requests=requests,
        acknowledgements=tuple(
            sorted(
                acknowledgements,
                key=lambda item: item.acknowledgement_id,
            )
        ),
        blockers=tuple(sorted(set(blockers))),
        warnings=tuple(sorted(set(warnings))),
        audit_trail=tuple(
            sorted(
                audit,
                key=lambda item: (
                    item.occurred_at,
                    item.event_id,
                ),
            )
        ),
    )


def _approval_record_id(record: object) -> str:
    for field_name in (
        "record_id",
        "approval_record_id",
        "decision_id",
        "id",
    ):
        value = getattr(record, field_name, None)
        if value is not None and str(value).strip():
            return str(value).strip()

    return deterministic_identifier(
        "approval-record",
        record,
    )
