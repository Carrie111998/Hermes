"""Task and attempt status enums with legal transition rules."""

from __future__ import annotations

from typing import Final

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
