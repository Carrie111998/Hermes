"""Task and attempt status enums with legal transition rules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

RunStatus = str
TaskStatus = str
AttemptStatus = str

RUN_CREATED: Final = "created"
RUN_RUNNING: Final = "running"
RUN_COMPLETED: Final = "completed"
RUN_FAILED: Final = "failed"
RUN_CANCELLED: Final = "cancelled"

TASK_CREATED: Final = "created"
TASK_RUNNING: Final = "running"
TASK_BLOCKED: Final = "blocked"
TASK_COMPLETED: Final = "completed"
TASK_FAILED: Final = "failed"
TASK_CANCELLED: Final = "cancelled"

ATTEMPT_CREATED: Final = "created"
ATTEMPT_RUNNING: Final = "running"
ATTEMPT_RESULT_SUBMITTED: Final = "result_submitted"
ATTEMPT_VERIFICATION_PASSED: Final = "verification_passed"
ATTEMPT_VERIFICATION_FAILED: Final = "verification_failed"
ATTEMPT_HEAL_REQUIRED: Final = "heal_required"
ATTEMPT_COMPLETED: Final = "completed"
ATTEMPT_FAILED: Final = "failed"
ATTEMPT_CANCELLED: Final = "cancelled"

RUN_STATUSES: frozenset[str] = frozenset(
    {
        RUN_CREATED,
        RUN_RUNNING,
        RUN_COMPLETED,
        RUN_FAILED,
        RUN_CANCELLED,
    }
)

TASK_STATUSES: frozenset[str] = frozenset(
    {
        TASK_CREATED,
        TASK_RUNNING,
        TASK_BLOCKED,
        TASK_COMPLETED,
        TASK_FAILED,
        TASK_CANCELLED,
    }
)

ATTEMPT_STATUSES: frozenset[str] = frozenset(
    {
        ATTEMPT_CREATED,
        ATTEMPT_RUNNING,
        ATTEMPT_RESULT_SUBMITTED,
        ATTEMPT_VERIFICATION_PASSED,
        ATTEMPT_VERIFICATION_FAILED,
        ATTEMPT_HEAL_REQUIRED,
        ATTEMPT_COMPLETED,
        ATTEMPT_FAILED,
        ATTEMPT_CANCELLED,
    }
)

TASK_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {TASK_COMPLETED, TASK_FAILED, TASK_CANCELLED}
)

RUN_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {RUN_COMPLETED, RUN_FAILED, RUN_CANCELLED}
)

ATTEMPT_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {ATTEMPT_COMPLETED, ATTEMPT_FAILED, ATTEMPT_CANCELLED}
)

TASK_LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    TASK_CREATED: frozenset({TASK_RUNNING, TASK_CANCELLED}),
    TASK_RUNNING: frozenset(
        {TASK_BLOCKED, TASK_COMPLETED, TASK_FAILED, TASK_CANCELLED}
    ),
    TASK_BLOCKED: frozenset({TASK_RUNNING, TASK_CANCELLED}),
}

RUN_LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    RUN_CREATED: frozenset({RUN_RUNNING, RUN_COMPLETED, RUN_CANCELLED}),
    RUN_RUNNING: frozenset({RUN_COMPLETED, RUN_FAILED, RUN_CANCELLED}),
}

ATTEMPT_LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    ATTEMPT_CREATED: frozenset({ATTEMPT_RUNNING, ATTEMPT_CANCELLED}),
    ATTEMPT_RUNNING: frozenset(
        {ATTEMPT_RESULT_SUBMITTED, ATTEMPT_FAILED, ATTEMPT_CANCELLED}
    ),
    ATTEMPT_RESULT_SUBMITTED: frozenset(
        {ATTEMPT_VERIFICATION_PASSED, ATTEMPT_VERIFICATION_FAILED, ATTEMPT_HEAL_REQUIRED}
    ),
    ATTEMPT_VERIFICATION_PASSED: frozenset({ATTEMPT_COMPLETED}),
    ATTEMPT_VERIFICATION_FAILED: frozenset(
        {ATTEMPT_HEAL_REQUIRED, ATTEMPT_FAILED}
    ),
    ATTEMPT_HEAL_REQUIRED: frozenset({ATTEMPT_FAILED}),
}


class HTRStateError(Exception):
    """Base error for HTR state and event operations."""


class InvalidTransition(HTRStateError):
    """Raised when a lifecycle status transition is not allowed."""


class EventConflict(HTRStateError):
    """Raised when the same event_id is reused with different semantics."""


class AttemptAlreadyRegistered(HTRStateError):
    """Raised when an attempt_id is registered more than once."""


class EventValidationError(HTRStateError):
    """Raised when an event fails schema validation."""


ERROR_CODE_RUN_FINALIZED: Final = "RUN_FINALIZED"
ERROR_CODE_RUN_SEAL_BLOCKED: Final = "RUN_SEAL_BLOCKED"


class RunFinalizedError(HTRStateError):
    """Raised when a valid final closure seals the run against mutation."""

    def __init__(
        self,
        message: str = "Original run is finalized and cannot be modified.",
        *,
        run_id: str | None = None,
        error_code: str = ERROR_CODE_RUN_FINALIZED,
    ) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.error_code = error_code


class RunSealBlockedError(HTRStateError):
    """Raised when closure state is untrusted or indeterminate for mutation."""

    def __init__(
        self,
        message: str = "Run closure state is untrusted; mutation blocked.",
        *,
        run_id: str | None = None,
        error_code: str = ERROR_CODE_RUN_SEAL_BLOCKED,
        reason_codes: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.error_code = error_code
        self.reason_codes = reason_codes


ERROR_CODE_APPROVAL_VALIDATION: Final = "APPROVAL_VALIDATION_FAILED"
ERROR_CODE_APPROVAL_CONFLICT: Final = "APPROVAL_CONFLICT"
ERROR_CODE_APPROVAL_FINALIZED: Final = "APPROVAL_FINALIZED_RUN_BLOCKED"
ERROR_CODE_APPROVAL_STATE: Final = "APPROVAL_ILLEGAL_STATE"


class ApprovalControlError(HTRStateError):
    """Base error for Task 24 approval-control operations."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = ERROR_CODE_APPROVAL_VALIDATION,
        approval_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.approval_id = approval_id


class ApprovalValidationError(ApprovalControlError):
    """Raised when approval inputs or derived validation fail."""


class ApprovalConflictError(ApprovalControlError):
    """Raised when an immutable record replay conflicts with existing evidence."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__(message, error_code=ERROR_CODE_APPROVAL_CONFLICT, **kwargs)


class ApprovalFinalizedRunError(ApprovalControlError):
    """Raised when a lifecycle approval targets a finalized original run."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__(message, error_code=ERROR_CODE_APPROVAL_FINALIZED, **kwargs)


class ApprovalStateError(ApprovalControlError):
    """Raised when an approval transition is not legal for current evidence."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__(message, error_code=ERROR_CODE_APPROVAL_STATE, **kwargs)


ERROR_CODE_INVOKE_STALE: Final = "INVOKE_STALE_REJECTION"
ERROR_CODE_INVOKE_AMBIGUOUS: Final = "INVOKE_AMBIGUOUS_OUTCOME"
ERROR_CODE_INVOKE_OUTCOME_PERSISTENCE: Final = "INVOKE_OUTCOME_PERSISTENCE_FAILED"
ERROR_CODE_INVOKE_CLEANUP_DURABILITY: Final = "INVOKE_CLEANUP_DURABILITY_FAILED"


class InvokeRunCompletionError(HTRStateError):
    """Base error for Task 25 human-gated run-completion invoke."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        approval_id: str | None = None,
        claim_id: str | None = None,
        reason_code: str | None = None,
        mutation_may_have_committed: bool = False,
        safe_to_retry: bool = False,
        outcome_evidence: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.approval_id = approval_id
        self.claim_id = claim_id
        self.reason_code = reason_code
        self.mutation_may_have_committed = mutation_may_have_committed
        self.safe_to_retry = safe_to_retry
        self.outcome_evidence = outcome_evidence


class InvokeStaleApprovalError(InvokeRunCompletionError):
    """Raised when pre-claim validation rejects the approval as stale."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__(
            message,
            error_code=ERROR_CODE_INVOKE_STALE,
            safe_to_retry=False,
            **kwargs,
        )


class InvokeAmbiguousOutcomeError(InvokeRunCompletionError):
    """Raised after a durable claim when invoke outcome is ambiguous."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__(
            message,
            error_code=ERROR_CODE_INVOKE_AMBIGUOUS,
            safe_to_retry=False,
            **kwargs,
        )


class InvokeOutcomePersistenceError(InvokeRunCompletionError):
    """Raised when outcome.json cannot be persisted after claim."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__(
            message,
            error_code=ERROR_CODE_INVOKE_OUTCOME_PERSISTENCE,
            safe_to_retry=False,
            **kwargs,
        )


class InvokeCleanupDurabilityError(InvokeRunCompletionError):
    """Raised when marker cleanup fails after a consumed outcome."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__(
            message,
            error_code=ERROR_CODE_INVOKE_CLEANUP_DURABILITY,
            safe_to_retry=False,
            **kwargs,
        )


@dataclass(frozen=True)
class InvokeRunCompletionResult:
    """Immutable success result for Task 25 run-completion invoke."""

    approval_id: str
    claim_id: str
    run_id: str
    event_id: str
    completion_record_fingerprint: str
    event_semantic_fingerprint: str
    pre_observation_digest: str
    post_observation_digest: str
    outcome_digest: str


ERROR_CODE_RECONCILIATION_INSPECTION: Final = "RECONCILIATION_INSPECTION_FAILED"
ERROR_CODE_RECONCILIATION_UNSUPPORTED: Final = "RECONCILIATION_UNSUPPORTED_APPROVAL"
ERROR_CODE_RECONCILIATION_EVIDENCE: Final = "RECONCILIATION_EVIDENCE_INTEGRITY"


class ReconciliationInspectionError(HTRStateError):
    """Base error for Task 26A read-only reconciliation inspection."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = ERROR_CODE_RECONCILIATION_INSPECTION,
        approval_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.approval_id = approval_id


class ReconciliationUnsupportedApprovalError(ReconciliationInspectionError):
    """Raised when approval is outside Task 26A pilot scope."""

    def __init__(self, message: str, *, approval_id: str | None = None) -> None:
        super().__init__(
            message,
            error_code=ERROR_CODE_RECONCILIATION_UNSUPPORTED,
            approval_id=approval_id,
        )


class ReconciliationEvidenceIntegrityError(ReconciliationInspectionError):
    """Raised when inspection cannot resolve trustworthy identity/evidence."""

    def __init__(self, message: str, *, approval_id: str | None = None) -> None:
        super().__init__(
            message,
            error_code=ERROR_CODE_RECONCILIATION_EVIDENCE,
            approval_id=approval_id,
        )


@dataclass(frozen=True)
class RunCompletionReconciliationInspection:
    """Immutable read-only reconciliation inspection for Task 25 pilot."""

    inspection_schema_version: str
    inspection_projection_version: str
    approval_id: str
    approval_digest: str
    claim_id: str | None
    claim_digest: str | None
    outcome_class: str | None
    outcome_digest: str | None
    run_id: str
    bound_api: str
    event_id: str
    htr_runs_root_path_digest: str
    approval_control_state: str
    marker_state: str
    lifecycle_evidence_state: str
    integrity_state: str
    overall_classification: str
    reason_codes: tuple[str, ...]
    observed_completion_record_fingerprint: str | None
    observed_event_semantic_fingerprint: str | None
    observed_manifest_status: str | None
    current_observation_semantic_digest: str | None
    source_observation_digest: str
    inspection_semantic_digest: str
    safe_to_retry: bool
    marker_disposition_allowed: bool
    reconciliation_case_required: bool
    recovery_protocol_required: bool
    observed_at: str | None



def is_valid_task_transition(from_status: str, to_status: str) -> bool:
    """Return True when *to_status* is legal from *from_status*."""
    if from_status not in TASK_STATUSES or to_status not in TASK_STATUSES:
        return False
    allowed = TASK_LEGAL_TRANSITIONS.get(from_status, frozenset())
    return to_status in allowed


def is_valid_attempt_transition(from_status: str, to_status: str) -> bool:
    """Return True when *to_status* is legal from *from_status*."""
    if from_status not in ATTEMPT_STATUSES or to_status not in ATTEMPT_STATUSES:
        return False
    allowed = ATTEMPT_LEGAL_TRANSITIONS.get(from_status, frozenset())
    return to_status in allowed


def assert_valid_task_transition(from_status: str, to_status: str) -> None:
    """Raise :class:`InvalidTransition` when the transition is not legal."""
    if not is_valid_task_transition(from_status, to_status):
        raise InvalidTransition(
            f"illegal task transition: {from_status!r} -> {to_status!r}"
        )


def assert_valid_attempt_transition(from_status: str, to_status: str) -> None:
    """Raise :class:`InvalidTransition` when the transition is not legal."""
    if not is_valid_attempt_transition(from_status, to_status):
        raise InvalidTransition(
            f"illegal attempt transition: {from_status!r} -> {to_status!r}"
        )


def is_valid_run_transition(from_status: str, to_status: str) -> bool:
    """Return True when *to_status* is legal from *from_status*."""
    if from_status not in RUN_STATUSES or to_status not in RUN_STATUSES:
        return False
    allowed = RUN_LEGAL_TRANSITIONS.get(from_status, frozenset())
    return to_status in allowed


def assert_valid_run_transition(from_status: str, to_status: str) -> None:
    """Raise :class:`InvalidTransition` when the transition is not legal."""
    if not is_valid_run_transition(from_status, to_status):
        raise InvalidTransition(
            f"illegal run transition: {from_status!r} -> {to_status!r}"
        )


def is_terminal_task_status(status: str) -> bool:
    """Return True when *status* is a terminal task status."""
    return status in TASK_TERMINAL_STATUSES


def is_terminal_attempt_status(status: str) -> bool:
    """Return True when *status* is a terminal attempt status."""
    return status in ATTEMPT_TERMINAL_STATUSES


def is_terminal_run_status(status: str) -> bool:
    """Return True when *status* is a terminal run status."""
    return status in RUN_TERMINAL_STATUSES
