"""HTR lifecycle event log and state transition API."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from htr import paths
from htr.ids import new_event_id, validate_id
from htr.io import (
    append_jsonl,
    atomic_write_json,
    create_attempt_workspace,
    create_run_workspace,
    create_task_workspace,
    ensure_dir,
    read_json,
    read_jsonl,
)
from htr.contracts import (
    EXECUTION_REQUEST_PENDING,
    EXECUTION_VERIFICATION_ACCEPTED,
    EXECUTION_VERIFICATION_NEEDS_CHANGES,
    EXECUTION_VERIFICATION_REJECTED,
    make_run_execution_result_record,
    process_execution_items,
    result_fingerprint,
    run_completion_fingerprint,
    run_completion_record_json_path,
    run_execution_request_fingerprint,
    run_execution_request_record_json_path,
    run_execution_result_fingerprint,
    run_execution_result_record_json_path,
    run_execution_verification_fingerprint,
    run_execution_verification_record_json_path,
    run_post_verification_followup_plan_fingerprint,
    run_post_verification_followup_plan_record_json_path,
    run_followup_plan_fingerprint,
    run_followup_plan_record_json_path,
    run_review_fingerprint,
    run_review_record_json_path,
    task_completion_fingerprint,
    task_completion_record_json_path,
    validate_item_verifications_correspond_to_results,
    validate_post_verification_followup_items_correspond,
    verification_fingerprint,
    verification_result_json_path,
)
from htr.schemas import validate as validate_schema
from htr.state import (
    ATTEMPT_HEAL_REQUIRED,
    ATTEMPT_RESULT_SUBMITTED,
    ATTEMPT_VERIFICATION_FAILED,
    ATTEMPT_VERIFICATION_PASSED,
    RUN_COMPLETED,
    TASK_COMPLETED,
    AttemptAlreadyRegistered,
    EventConflict,
    InvalidTransition,
    assert_valid_attempt_transition,
    assert_valid_run_transition,
    assert_valid_task_transition,
)

EVENT_TYPE_TASK_STATUS_CHANGED = "task_status_changed"
EVENT_TYPE_ATTEMPT_REGISTERED = "attempt_registered"
EVENT_TYPE_ATTEMPT_STATUS_CHANGED = "attempt_status_changed"
EVENT_TYPE_ATTEMPT_RESULT_SUBMITTED = "attempt_result_submitted"
EVENT_TYPE_MANUAL_VERIFICATION_SUBMITTED = "manual_verification_submitted"
EVENT_TYPE_MANUAL_TASK_COMPLETED = "manual_task_completed"
EVENT_TYPE_MANUAL_RUN_COMPLETED = "manual_run_completed"
EVENT_TYPE_MANUAL_RUN_REVIEWED = "manual_run_reviewed"
EVENT_TYPE_MANUAL_RUN_FOLLOWUP_PLANNED = "manual_run_followup_planned"
EVENT_TYPE_RUN_EXECUTION_REQUESTED = "run_execution_requested"
EVENT_TYPE_RUN_EXECUTION_COMPLETED = "run_execution_completed"
EVENT_TYPE_RUN_EXECUTION_VERIFIED = "run_execution_verified"
EVENT_TYPE_RUN_EXECUTION_REJECTED = "run_execution_rejected"
EVENT_TYPE_RUN_EXECUTION_NEEDS_CHANGES = "run_execution_needs_changes"
EVENT_TYPE_RUN_POST_VERIFICATION_FOLLOWUP_PLANNED = (
    "run_post_verification_followup_planned"
)

_EXECUTION_VERIFICATION_EVENT_TYPES: dict[str, str] = {
    EXECUTION_VERIFICATION_ACCEPTED: EVENT_TYPE_RUN_EXECUTION_VERIFIED,
    EXECUTION_VERIFICATION_REJECTED: EVENT_TYPE_RUN_EXECUTION_REJECTED,
    EXECUTION_VERIFICATION_NEEDS_CHANGES: EVENT_TYPE_RUN_EXECUTION_NEEDS_CHANGES,
}

EVENT_TYPES = frozenset(
    {
        EVENT_TYPE_TASK_STATUS_CHANGED,
        EVENT_TYPE_ATTEMPT_REGISTERED,
        EVENT_TYPE_ATTEMPT_STATUS_CHANGED,
        EVENT_TYPE_ATTEMPT_RESULT_SUBMITTED,
        EVENT_TYPE_MANUAL_VERIFICATION_SUBMITTED,
        EVENT_TYPE_MANUAL_TASK_COMPLETED,
        EVENT_TYPE_MANUAL_RUN_COMPLETED,
        EVENT_TYPE_MANUAL_RUN_REVIEWED,
        EVENT_TYPE_MANUAL_RUN_FOLLOWUP_PLANNED,
        EVENT_TYPE_RUN_EXECUTION_REQUESTED,
        EVENT_TYPE_RUN_EXECUTION_COMPLETED,
        EVENT_TYPE_RUN_EXECUTION_VERIFIED,
        EVENT_TYPE_RUN_EXECUTION_REJECTED,
        EVENT_TYPE_RUN_EXECUTION_NEEDS_CHANGES,
        EVENT_TYPE_RUN_POST_VERIFICATION_FOLLOWUP_PLANNED,
    }
)

VERIFICATION_OUTCOME_TO_STATUS: dict[str, str] = {
    "passed": ATTEMPT_VERIFICATION_PASSED,
    "failed": ATTEMPT_VERIFICATION_FAILED,
    "heal_required": ATTEMPT_HEAL_REQUIRED,
}

VERIFICATION_RECORDED_STATUSES: frozenset[str] = frozenset(
    {
        ATTEMPT_VERIFICATION_PASSED,
        ATTEMPT_VERIFICATION_FAILED,
        ATTEMPT_HEAL_REQUIRED,
    }
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_event_ids(event: dict[str, Any]) -> None:
    for value, kind in (
        (event["run_id"], "run"),
        (event["event_id"], "event"),
    ):
        if not validate_id(value, kind):
            raise ValueError(f"invalid {kind} id: {value!r}")
    task_id = event.get("task_id")
    if task_id is not None and not validate_id(task_id, "task"):
        raise ValueError(f"invalid task id: {task_id!r}")
    attempt_id = event.get("attempt_id")
    if attempt_id is not None and not validate_id(attempt_id, "attempt"):
        raise ValueError(f"invalid attempt id: {attempt_id!r}")


def _semantic_fingerprint(event: dict[str, Any]) -> str:
    payload = event.get("payload")
    if payload is None:
        payload = {}
    return json.dumps(
        {
            "event_type": event.get("event_type"),
            "run_id": event.get("run_id"),
            "task_id": event.get("task_id"),
            "attempt_id": event.get("attempt_id"),
            "previous_status": event.get("previous_status"),
            "new_status": event.get("new_status"),
            "actor": event.get("actor"),
            "payload": payload,
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def _find_event_by_id(
    run_id: str,
    event_id: str,
    base_dir: Path | None = None,
) -> dict[str, Any] | None:
    for event in read_task_events(run_id, base_dir):
        if event.get("event_id") == event_id:
            return event
    return None


def make_event(
    *,
    event_type: str,
    run_id: str,
    task_id: str,
    actor: str,
    payload: dict[str, Any] | None = None,
    event_id: str | None = None,
    attempt_id: str | None = None,
    previous_status: str | None = None,
    new_status: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build a validated lifecycle event envelope."""
    event: dict[str, Any] = {
        "event_id": event_id or new_event_id(),
        "event_type": event_type,
        "run_id": run_id,
        "task_id": task_id,
        "created_at": created_at or _utc_now_iso(),
        "actor": actor,
        "payload": payload if payload is not None else {},
    }
    if attempt_id is not None:
        event["attempt_id"] = attempt_id
    if previous_status is not None:
        event["previous_status"] = previous_status
    if new_status is not None:
        event["new_status"] = new_status
    validate_schema(event, "event")
    _validate_event_ids(event)
    return event


def make_run_event(
    *,
    event_type: str,
    run_id: str,
    actor: str,
    payload: dict[str, Any] | None = None,
    event_id: str | None = None,
    previous_status: str | None = None,
    new_status: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build a validated run-level lifecycle event envelope."""
    event: dict[str, Any] = {
        "event_id": event_id or new_event_id(),
        "event_type": event_type,
        "run_id": run_id,
        "created_at": created_at or _utc_now_iso(),
        "actor": actor,
        "payload": payload if payload is not None else {},
    }
    if previous_status is not None:
        event["previous_status"] = previous_status
    if new_status is not None:
        event["new_status"] = new_status
    validate_schema(event, "event")
    _validate_event_ids(event)
    return event


def append_task_event(
    run_id: str,
    event: dict[str, Any],
    base_dir: Path | None = None,
) -> None:
    """Append one lifecycle event to ``task_events.jsonl``."""
    validate_schema(event, "event")
    _validate_event_ids(event)
    if event["run_id"] != run_id:
        raise ValueError("event run_id does not match append target run_id")
    append_jsonl(paths.task_events_path(run_id, base_dir), event)


def append_run_event(
    run_id: str,
    event: dict[str, Any],
    base_dir: Path | None = None,
) -> None:
    """Append one run-level lifecycle event to ``task_events.jsonl``."""
    validate_schema(event, "event")
    _validate_event_ids(event)
    if event["run_id"] != run_id:
        raise ValueError("event run_id does not match append target run_id")
    if "task_id" in event:
        raise ValueError("run-level event must not include task_id")
    append_jsonl(paths.task_events_path(run_id, base_dir), event)


def read_task_events(
    run_id: str,
    base_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Read all lifecycle events for *run_id*; empty JSONL returns []."""
    return read_jsonl(paths.task_events_path(run_id, base_dir))


def event_exists(
    run_id: str,
    event_id: str,
    base_dir: Path | None = None,
) -> bool:
    """Return True when *event_id* is already present in the run event log."""
    return _find_event_by_id(run_id, event_id, base_dir) is not None


def _resolve_idempotent_event(
    run_id: str,
    event: dict[str, Any],
    base_dir: Path | None = None,
) -> dict[str, Any] | None:
    existing = _find_event_by_id(run_id, event["event_id"], base_dir)
    if existing is None:
        return None
    if _semantic_fingerprint(existing) == _semantic_fingerprint(event):
        return existing
    raise EventConflict(
        f"event_id {event['event_id']!r} already exists with different semantics"
    )


def _ensure_run_and_task_workspace(
    run_id: str,
    task_id: str,
    base_dir: Path | None = None,
) -> None:
    manifest_path = paths.run_manifest_path(run_id, base_dir)
    if not manifest_path.exists():
        create_run_workspace(run_id, base_dir)
    task_status_path = paths.task_status_path(run_id, task_id, base_dir)
    if not task_status_path.exists():
        create_task_workspace(run_id, task_id, base_dir)


def apply_task_transition(
    run_id: str,
    task_id: str,
    new_status: str,
    actor: str,
    event_id: str | None = None,
    payload: dict[str, Any] | None = None,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """Append a task status event, then update ``task_status.json`` snapshot."""
    validate_id(run_id, "run")
    validate_id(task_id, "task")
    _ensure_run_and_task_workspace(run_id, task_id, base_dir)

    status_path = paths.task_status_path(run_id, task_id, base_dir)
    current = read_json(status_path)
    previous_status = current["status"]

    assert_valid_task_transition(previous_status, new_status)

    candidate = make_event(
        event_type=EVENT_TYPE_TASK_STATUS_CHANGED,
        run_id=run_id,
        task_id=task_id,
        actor=actor,
        payload=payload,
        event_id=event_id,
        previous_status=previous_status,
        new_status=new_status,
    )
    existing = _resolve_idempotent_event(run_id, candidate, base_dir)
    if existing is not None:
        return existing

    append_task_event(run_id, candidate, base_dir)

    updated = dict(current)
    updated["status"] = new_status
    validate_schema(updated, "task_status")
    atomic_write_json(status_path, updated)
    return candidate


def register_attempt(
    run_id: str,
    task_id: str,
    attempt_id: str,
    actor: str,
    event_id: str | None = None,
    payload: dict[str, Any] | None = None,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """Bootstrap attempt workspace, append event, then register in task status."""
    validate_id(run_id, "run")
    validate_id(task_id, "task")
    validate_id(attempt_id, "attempt")
    _ensure_run_and_task_workspace(run_id, task_id, base_dir)

    status_path = paths.task_status_path(run_id, task_id, base_dir)
    current = read_json(status_path)

    candidate = make_event(
        event_type=EVENT_TYPE_ATTEMPT_REGISTERED,
        run_id=run_id,
        task_id=task_id,
        actor=actor,
        payload=payload,
        event_id=event_id,
        attempt_id=attempt_id,
    )
    existing = _resolve_idempotent_event(run_id, candidate, base_dir)
    if existing is not None:
        return existing

    if attempt_id in current.get("attempts", []):
        raise AttemptAlreadyRegistered(
            f"attempt_id {attempt_id!r} is already registered for task {task_id!r}"
        )

    create_attempt_workspace(run_id, task_id, attempt_id, base_dir)
    append_task_event(run_id, candidate, base_dir)

    updated = dict(current)
    attempts = list(updated.get("attempts", []))
    if attempt_id not in attempts:
        attempts.append(attempt_id)
    updated["attempts"] = attempts
    validate_schema(updated, "task_status")
    atomic_write_json(status_path, updated)
    return candidate


def apply_attempt_transition(
    run_id: str,
    task_id: str,
    attempt_id: str,
    new_status: str,
    actor: str,
    event_id: str | None = None,
    payload: dict[str, Any] | None = None,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """Append an attempt status event, then update ``attempt_status.json``."""
    validate_id(run_id, "run")
    validate_id(task_id, "task")
    validate_id(attempt_id, "attempt")
    _ensure_run_and_task_workspace(run_id, task_id, base_dir)

    attempt_status_path = paths.attempt_status_path(
        run_id, task_id, attempt_id, base_dir
    )
    if not attempt_status_path.exists():
        raise FileNotFoundError(
            f"attempt workspace missing for {attempt_id!r}; call register_attempt first"
        )

    current = read_json(attempt_status_path)
    previous_status = current["status"]

    assert_valid_attempt_transition(previous_status, new_status)

    candidate = make_event(
        event_type=EVENT_TYPE_ATTEMPT_STATUS_CHANGED,
        run_id=run_id,
        task_id=task_id,
        actor=actor,
        payload=payload,
        event_id=event_id,
        attempt_id=attempt_id,
        previous_status=previous_status,
        new_status=new_status,
    )
    existing = _resolve_idempotent_event(run_id, candidate, base_dir)
    if existing is not None:
        return existing

    append_task_event(run_id, candidate, base_dir)

    updated = dict(current)
    updated["status"] = new_status
    validate_schema(updated, "attempt_status")
    atomic_write_json(attempt_status_path, updated)
    return candidate


def _matches_result_submitted_replay(
    existing: dict[str, Any],
    *,
    run_id: str,
    task_id: str,
    attempt_id: str,
    actor: str,
    result: dict[str, Any],
) -> bool:
    """Return True when *existing* matches a successful result-submitted replay."""
    payload = existing.get("payload")
    if not isinstance(payload, dict):
        return False
    return (
        existing.get("event_type") == EVENT_TYPE_ATTEMPT_RESULT_SUBMITTED
        and existing.get("run_id") == run_id
        and existing.get("task_id") == task_id
        and existing.get("attempt_id") == attempt_id
        and existing.get("new_status") == ATTEMPT_RESULT_SUBMITTED
        and existing.get("actor") == actor
        and payload.get("result_fingerprint") == result_fingerprint(result)
    )


def submit_attempt_result(
    run_id: str,
    task_id: str,
    attempt_id: str,
    result: dict[str, Any],
    *,
    actor: str = "system",
    event_id: str | None = None,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """Write attempt result, append event, and move status to result_submitted."""
    validate_id(run_id, "run")
    validate_id(task_id, "task")
    validate_id(attempt_id, "attempt")
    validate_schema(result, "attempt_result")
    if (
        result["run_id"] != run_id
        or result["task_id"] != task_id
        or result["attempt_id"] != attempt_id
    ):
        raise ValueError("attempt_result ids do not match submission target")

    submitted_fingerprint = result_fingerprint(result)
    result_path = paths.result_json_path(run_id, task_id, attempt_id, base_dir)

    _ensure_run_and_task_workspace(run_id, task_id, base_dir)

    attempt_status_path = paths.attempt_status_path(
        run_id, task_id, attempt_id, base_dir
    )
    if not attempt_status_path.exists():
        raise FileNotFoundError(
            f"attempt workspace missing for {attempt_id!r}; call register_attempt first"
        )

    current = read_json(attempt_status_path)
    previous_status = current["status"]

    if previous_status == ATTEMPT_RESULT_SUBMITTED:
        if event_id is None:
            raise InvalidTransition(
                f"illegal attempt transition: {previous_status!r} -> "
                f"{ATTEMPT_RESULT_SUBMITTED!r}"
            )
        existing_event = _find_event_by_id(run_id, event_id, base_dir)
        if existing_event is None:
            raise InvalidTransition(
                f"illegal attempt transition: {previous_status!r} -> "
                f"{ATTEMPT_RESULT_SUBMITTED!r}"
            )
        if _matches_result_submitted_replay(
            existing_event,
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            actor=actor,
            result=result,
        ):
            return existing_event
        if (
            existing_event.get("event_type") == EVENT_TYPE_ATTEMPT_RESULT_SUBMITTED
            and existing_event.get("attempt_id") == attempt_id
        ):
            raise EventConflict(
                f"event_id {event_id!r} already exists with different semantics"
            )
        raise EventConflict(
            f"event_id {event_id!r} already exists with different semantics"
        )

    assert_valid_attempt_transition(previous_status, ATTEMPT_RESULT_SUBMITTED)

    candidate = make_event(
        event_type=EVENT_TYPE_ATTEMPT_RESULT_SUBMITTED,
        run_id=run_id,
        task_id=task_id,
        actor=actor,
        payload={
            "result_path": str(result_path),
            "result_fingerprint": submitted_fingerprint,
        },
        event_id=event_id,
        attempt_id=attempt_id,
        previous_status=previous_status,
        new_status=ATTEMPT_RESULT_SUBMITTED,
    )
    existing = _resolve_idempotent_event(run_id, candidate, base_dir)
    if existing is not None:
        return existing

    ensure_dir(result_path.parent)
    atomic_write_json(result_path, result)
    append_task_event(run_id, candidate, base_dir)

    updated = dict(current)
    updated["status"] = ATTEMPT_RESULT_SUBMITTED
    validate_schema(updated, "attempt_status")
    atomic_write_json(attempt_status_path, updated)
    return candidate


def _matches_manual_verification_replay(
    existing: dict[str, Any],
    *,
    run_id: str,
    task_id: str,
    attempt_id: str,
    actor: str,
    target_status: str,
    outcome: str,
    verification_result: dict[str, Any],
) -> bool:
    """Return True when *existing* matches a successful manual verification replay."""
    payload = existing.get("payload")
    if not isinstance(payload, dict):
        return False
    return (
        existing.get("event_type") == EVENT_TYPE_MANUAL_VERIFICATION_SUBMITTED
        and existing.get("run_id") == run_id
        and existing.get("task_id") == task_id
        and existing.get("attempt_id") == attempt_id
        and existing.get("new_status") == target_status
        and existing.get("actor") == actor
        and payload.get("outcome") == outcome
        and payload.get("verification_fingerprint")
        == verification_fingerprint(verification_result)
    )


def submit_manual_verification(
    run_id: str,
    task_id: str,
    attempt_id: str,
    verification_result: dict[str, Any],
    *,
    actor: str = "system",
    event_id: str | None = None,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """Record a manual verification decision and move attempt to the mapped status."""
    validate_schema(verification_result, "verification_result")
    if (
        verification_result["run_id"] != run_id
        or verification_result["task_id"] != task_id
        or verification_result["attempt_id"] != attempt_id
    ):
        raise ValueError(
            "verification_result ids do not match submission target"
        )

    outcome = verification_result["outcome"]
    target_status = VERIFICATION_OUTCOME_TO_STATUS.get(outcome)
    if target_status is None:
        raise ValueError(
            "verification_result outcome must be one of passed, failed, heal_required"
        )

    submitted_fingerprint = verification_fingerprint(verification_result)
    verification_path = verification_result_json_path(
        run_id, task_id, attempt_id, base_dir
    )

    _ensure_run_and_task_workspace(run_id, task_id, base_dir)

    attempt_status_path = paths.attempt_status_path(
        run_id, task_id, attempt_id, base_dir
    )
    if not attempt_status_path.exists():
        raise FileNotFoundError(
            f"attempt workspace missing for {attempt_id!r}; call register_attempt first"
        )

    current = read_json(attempt_status_path)
    previous_status = current["status"]

    if previous_status in VERIFICATION_RECORDED_STATUSES:
        if event_id is None:
            raise InvalidTransition(
                f"illegal attempt transition: {previous_status!r} -> {target_status!r}"
            )
        existing_event = _find_event_by_id(run_id, event_id, base_dir)
        if existing_event is None:
            raise InvalidTransition(
                f"illegal attempt transition: {previous_status!r} -> {target_status!r}"
            )
        if _matches_manual_verification_replay(
            existing_event,
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            actor=actor,
            target_status=target_status,
            outcome=outcome,
            verification_result=verification_result,
        ):
            return existing_event
        if (
            existing_event.get("event_type")
            == EVENT_TYPE_MANUAL_VERIFICATION_SUBMITTED
            and existing_event.get("attempt_id") == attempt_id
        ):
            raise EventConflict(
                f"event_id {event_id!r} already exists with different semantics"
            )
        raise EventConflict(
            f"event_id {event_id!r} already exists with different semantics"
        )

    assert_valid_attempt_transition(previous_status, target_status)

    candidate = make_event(
        event_type=EVENT_TYPE_MANUAL_VERIFICATION_SUBMITTED,
        run_id=run_id,
        task_id=task_id,
        actor=actor,
        payload={
            "outcome": outcome,
            "verification_fingerprint": submitted_fingerprint,
            "verification_result_path": str(verification_path),
        },
        event_id=event_id,
        attempt_id=attempt_id,
        previous_status=previous_status,
        new_status=target_status,
    )
    existing = _resolve_idempotent_event(run_id, candidate, base_dir)
    if existing is not None:
        return existing

    ensure_dir(verification_path.parent)
    atomic_write_json(verification_path, verification_result)
    append_task_event(run_id, candidate, base_dir)

    updated = dict(current)
    updated["status"] = target_status
    validate_schema(updated, "attempt_status")
    atomic_write_json(attempt_status_path, updated)
    return candidate


def _find_task_event_by_id(
    run_id: str,
    task_id: str,
    event_id: str,
    base_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Return the event for *event_id* when it belongs to *task_id*, else None."""
    existing = _find_event_by_id(run_id, event_id, base_dir)
    if existing is None or existing.get("task_id") != task_id:
        return None
    return existing


def _matches_manual_task_completed_replay(
    existing: dict[str, Any],
    *,
    run_id: str,
    task_id: str,
    attempt_id: str,
    actor: str,
    completion_record: dict[str, Any],
) -> bool:
    """Return True when *existing* matches a successful manual completion replay."""
    payload = existing.get("payload")
    if not isinstance(payload, dict):
        return False
    existing_attempt_id = existing.get("attempt_id") or payload.get("attempt_id")
    return (
        existing.get("event_type") == EVENT_TYPE_MANUAL_TASK_COMPLETED
        and existing.get("run_id") == run_id
        and existing.get("task_id") == task_id
        and existing_attempt_id == attempt_id
        and existing.get("new_status") == TASK_COMPLETED
        and existing.get("actor") == actor
        and payload.get("completion_fingerprint")
        == task_completion_fingerprint(completion_record)
    )


def complete_task_manually(
    run_id: str,
    task_id: str,
    attempt_id: str,
    completion_record: dict[str, Any],
    *,
    actor: str = "human",
    event_id: str | None = None,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """Manually mark a task completed after *attempt_id* has verification_passed."""
    validate_id(run_id, "run")
    validate_id(task_id, "task")
    validate_id(attempt_id, "attempt")
    validate_schema(completion_record, "task_completion_record")
    if (
        completion_record["run_id"] != run_id
        or completion_record["task_id"] != task_id
        or completion_record["attempt_id"] != attempt_id
    ):
        raise ValueError("completion_record ids do not match submission target")

    submitted_fingerprint = task_completion_fingerprint(completion_record)
    completion_record_path = task_completion_record_json_path(
        run_id, task_id, base_dir
    )

    _ensure_run_and_task_workspace(run_id, task_id, base_dir)

    attempt_status_path = paths.attempt_status_path(
        run_id, task_id, attempt_id, base_dir
    )
    if not attempt_status_path.exists():
        raise InvalidTransition(
            f"attempt {attempt_id!r} is not verification_passed; "
            f"status is missing"
        )
    attempt_status = read_json(attempt_status_path)
    if attempt_status["status"] != ATTEMPT_VERIFICATION_PASSED:
        raise InvalidTransition(
            f"attempt {attempt_id!r} is not verification_passed; "
            f"status is {attempt_status['status']!r}"
        )

    task_status_path = paths.task_status_path(run_id, task_id, base_dir)
    current_task_status = read_json(task_status_path)
    previous_task_status = current_task_status["status"]

    if previous_task_status == TASK_COMPLETED:
        if event_id is None:
            raise InvalidTransition(
                f"illegal task transition: {previous_task_status!r} -> "
                f"{TASK_COMPLETED!r}"
            )
        existing_event = _find_task_event_by_id(
            run_id, task_id, event_id, base_dir
        )
        if existing_event is None:
            raise InvalidTransition(
                f"illegal task transition: {previous_task_status!r} -> "
                f"{TASK_COMPLETED!r}"
            )
        if _matches_manual_task_completed_replay(
            existing_event,
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            actor=actor,
            completion_record=completion_record,
        ):
            return existing_event
        if existing_event.get("event_type") == EVENT_TYPE_MANUAL_TASK_COMPLETED:
            raise EventConflict(
                f"event_id {event_id!r} already exists with different semantics"
            )
        raise EventConflict(
            f"event_id {event_id!r} already exists with different semantics"
        )

    if completion_record_path.exists():
        raise InvalidTransition(
            "task_completion_record.json exists while task_status is not completed"
        )

    assert_valid_task_transition(previous_task_status, TASK_COMPLETED)

    candidate = make_event(
        event_type=EVENT_TYPE_MANUAL_TASK_COMPLETED,
        run_id=run_id,
        task_id=task_id,
        actor=actor,
        payload={
            "attempt_id": attempt_id,
            "completion_fingerprint": submitted_fingerprint,
            "completion_record_path": str(completion_record_path),
        },
        event_id=event_id,
        attempt_id=attempt_id,
        previous_status=previous_task_status,
        new_status=TASK_COMPLETED,
    )
    existing = _resolve_idempotent_event(run_id, candidate, base_dir)
    if existing is not None:
        return existing

    ensure_dir(completion_record_path.parent)
    atomic_write_json(completion_record_path, completion_record)
    append_task_event(run_id, candidate, base_dir)

    updated_task_status = dict(current_task_status)
    updated_task_status["status"] = TASK_COMPLETED
    validate_schema(updated_task_status, "task_status")
    atomic_write_json(task_status_path, updated_task_status)
    return candidate


def _find_run_event_by_id(
    run_id: str,
    event_id: str,
    base_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Return the run-level event for *event_id*, else None."""
    existing = _find_event_by_id(run_id, event_id, base_dir)
    if existing is None or existing.get("run_id") != run_id:
        return None
    if "task_id" in existing:
        return None
    return existing


def _matches_manual_run_completed_replay(
    existing: dict[str, Any],
    *,
    run_id: str,
    actor: str,
    completion_record: dict[str, Any],
) -> bool:
    """Return True when *existing* matches a successful manual run completion replay."""
    payload = existing.get("payload")
    if not isinstance(payload, dict):
        return False
    return (
        existing.get("event_type") == EVENT_TYPE_MANUAL_RUN_COMPLETED
        and existing.get("run_id") == run_id
        and existing.get("new_status") == RUN_COMPLETED
        and existing.get("actor") == actor
        and payload.get("completed_task_ids")
        == completion_record["completed_task_ids"]
        and payload.get("run_completion_fingerprint")
        == run_completion_fingerprint(completion_record)
    )


def complete_run_manually(
    run_id: str,
    completion_record: dict[str, Any],
    *,
    actor: str = "human",
    event_id: str | None = None,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """Manually mark a run completed after listed tasks are already completed."""
    validate_id(run_id, "run")
    validate_schema(completion_record, "run_completion_record")
    if completion_record["run_id"] != run_id:
        raise ValueError("completion_record run_id does not match submission target")

    submitted_fingerprint = run_completion_fingerprint(completion_record)
    completion_record_path = run_completion_record_json_path(run_id, base_dir)

    manifest_path = paths.run_manifest_path(run_id, base_dir)
    if not manifest_path.exists():
        create_run_workspace(run_id, base_dir)

    for task_id in completion_record["completed_task_ids"]:
        validate_id(task_id, "task")
        task_status_path = paths.task_status_path(run_id, task_id, base_dir)
        if not task_status_path.exists():
            raise InvalidTransition(
                f"task {task_id!r} is not completed; task_status is missing"
            )
        task_status = read_json(task_status_path)
        if task_status["status"] != TASK_COMPLETED:
            raise InvalidTransition(
                f"task {task_id!r} is not completed; "
                f"status is {task_status['status']!r}"
            )

    current_run_manifest = read_json(manifest_path)
    previous_run_status = current_run_manifest["status"]

    if previous_run_status == RUN_COMPLETED:
        if event_id is None:
            raise InvalidTransition(
                f"illegal run transition: {previous_run_status!r} -> "
                f"{RUN_COMPLETED!r}"
            )
        existing_event = _find_run_event_by_id(run_id, event_id, base_dir)
        if existing_event is None:
            raise InvalidTransition(
                f"illegal run transition: {previous_run_status!r} -> "
                f"{RUN_COMPLETED!r}"
            )
        if _matches_manual_run_completed_replay(
            existing_event,
            run_id=run_id,
            actor=actor,
            completion_record=completion_record,
        ):
            return existing_event
        if existing_event.get("event_type") == EVENT_TYPE_MANUAL_RUN_COMPLETED:
            raise EventConflict(
                f"event_id {event_id!r} already exists with different semantics"
            )
        raise EventConflict(
            f"event_id {event_id!r} already exists with different semantics"
        )

    if completion_record_path.exists():
        raise InvalidTransition(
            "run_completion_record.json exists while run status is not completed"
        )

    assert_valid_run_transition(previous_run_status, RUN_COMPLETED)

    candidate = make_run_event(
        event_type=EVENT_TYPE_MANUAL_RUN_COMPLETED,
        run_id=run_id,
        actor=actor,
        payload={
            "completed_task_ids": list(completion_record["completed_task_ids"]),
            "run_completion_fingerprint": submitted_fingerprint,
            "run_completion_record_path": str(completion_record_path),
        },
        event_id=event_id,
        previous_status=previous_run_status,
        new_status=RUN_COMPLETED,
    )
    existing = _resolve_idempotent_event(run_id, candidate, base_dir)
    if existing is not None:
        return existing

    ensure_dir(completion_record_path.parent)
    atomic_write_json(completion_record_path, completion_record)
    append_run_event(run_id, candidate, base_dir)

    updated_run_manifest = dict(current_run_manifest)
    updated_run_manifest["status"] = RUN_COMPLETED
    validate_schema(updated_run_manifest, "run_manifest")
    atomic_write_json(manifest_path, updated_run_manifest)
    return candidate


def _matches_manual_run_reviewed_replay(
    existing: dict[str, Any],
    *,
    run_id: str,
    actor: str,
    review_record: dict[str, Any],
) -> bool:
    """Return True when *existing* matches a successful manual run review replay."""
    payload = existing.get("payload")
    if not isinstance(payload, dict):
        return False
    return (
        existing.get("event_type") == EVENT_TYPE_MANUAL_RUN_REVIEWED
        and existing.get("run_id") == run_id
        and existing.get("actor") == actor
        and payload.get("decision") == review_record["decision"]
        and payload.get("reviewer") == review_record["reviewer"]
        and payload.get("run_review_fingerprint")
        == run_review_fingerprint(review_record)
    )


def review_run_manually(
    run_id: str,
    review_record: dict[str, Any],
    *,
    actor: str = "human",
    event_id: str | None = None,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """Manually record a human review decision for a completed run."""
    validate_id(run_id, "run")
    validate_schema(review_record, "run_review_record")
    if review_record["run_id"] != run_id:
        raise ValueError("review_record run_id does not match submission target")

    submitted_fingerprint = run_review_fingerprint(review_record)
    review_record_path = run_review_record_json_path(run_id, base_dir)
    completion_record_path = run_completion_record_json_path(run_id, base_dir)
    manifest_path = paths.run_manifest_path(run_id, base_dir)

    if not manifest_path.exists():
        raise InvalidTransition(f"run {run_id!r} is not completed")

    current_run_manifest = read_json(manifest_path)
    if current_run_manifest["status"] != RUN_COMPLETED:
        raise InvalidTransition(
            f"run {run_id!r} is not completed; "
            f"status is {current_run_manifest['status']!r}"
        )

    if not completion_record_path.exists():
        raise InvalidTransition("run_completion_record.json is missing")

    if review_record_path.exists():
        if event_id is None:
            raise InvalidTransition("run_review_record.json already exists")
        existing_event = _find_run_event_by_id(run_id, event_id, base_dir)
        if existing_event is None:
            raise InvalidTransition("run_review_record.json already exists")
        if _matches_manual_run_reviewed_replay(
            existing_event,
            run_id=run_id,
            actor=actor,
            review_record=review_record,
        ):
            return existing_event
        if existing_event.get("event_type") == EVENT_TYPE_MANUAL_RUN_REVIEWED:
            raise EventConflict(
                f"event_id {event_id!r} already exists with different semantics"
            )
        raise EventConflict(
            f"event_id {event_id!r} already exists with different semantics"
        )

    candidate = make_run_event(
        event_type=EVENT_TYPE_MANUAL_RUN_REVIEWED,
        run_id=run_id,
        actor=actor,
        payload={
            "decision": review_record["decision"],
            "reviewer": review_record["reviewer"],
            "run_review_fingerprint": submitted_fingerprint,
            "run_review_record_path": str(review_record_path),
        },
        event_id=event_id,
    )
    existing = _resolve_idempotent_event(run_id, candidate, base_dir)
    if existing is not None:
        return existing

    ensure_dir(review_record_path.parent)
    atomic_write_json(review_record_path, review_record)
    append_run_event(run_id, candidate, base_dir)
    return candidate


def _matches_manual_run_followup_planned_replay(
    existing: dict[str, Any],
    *,
    run_id: str,
    actor: str,
    followup_plan_record: dict[str, Any],
) -> bool:
    """Return True when *existing* matches a successful follow-up plan replay."""
    payload = existing.get("payload")
    if not isinstance(payload, dict):
        return False
    return (
        existing.get("event_type") == EVENT_TYPE_MANUAL_RUN_FOLLOWUP_PLANNED
        and existing.get("run_id") == run_id
        and existing.get("actor") == actor
        and payload.get("run_id") == run_id
        and payload.get("source_review_decision")
        == followup_plan_record["source_review_decision"]
        and payload.get("plan_status") == followup_plan_record["plan_status"]
        and payload.get("planner") == followup_plan_record["planner"]
        and payload.get("run_followup_plan_fingerprint")
        == run_followup_plan_fingerprint(followup_plan_record)
    )


def plan_run_followup(
    run_id: str,
    followup_plan_record: dict[str, Any],
    *,
    actor: str = "human",
    event_id: str | None = None,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """Record a review-gated follow-up plan for a reviewed completed run.

    The plan may be authored by a human, assistant, tool, or mixed process.
    This API only validates, stores, and audits the plan; it does not execute it.
    """
    validate_id(run_id, "run")
    validate_schema(followup_plan_record, "run_followup_plan_record")
    if followup_plan_record["run_id"] != run_id:
        raise ValueError(
            "followup_plan_record run_id does not match submission target"
        )

    submitted_fingerprint = run_followup_plan_fingerprint(followup_plan_record)
    followup_plan_record_path = run_followup_plan_record_json_path(run_id, base_dir)
    completion_record_path = run_completion_record_json_path(run_id, base_dir)
    review_record_path = run_review_record_json_path(run_id, base_dir)
    manifest_path = paths.run_manifest_path(run_id, base_dir)

    if not manifest_path.exists():
        raise InvalidTransition(f"run {run_id!r} is not completed")

    current_run_manifest = read_json(manifest_path)
    if current_run_manifest["status"] != RUN_COMPLETED:
        raise InvalidTransition(
            f"run {run_id!r} is not completed; "
            f"status is {current_run_manifest['status']!r}"
        )

    if not completion_record_path.exists():
        raise InvalidTransition("run_completion_record.json is missing")

    if not review_record_path.exists():
        raise InvalidTransition("run_review_record.json is missing")

    stored_review_record = read_json(review_record_path)
    if (
        followup_plan_record["source_review_decision"]
        != stored_review_record["decision"]
    ):
        raise InvalidTransition(
            "source_review_decision does not match run_review_record decision"
        )

    if followup_plan_record_path.exists():
        existing_followup_plan_record = read_json(followup_plan_record_path)
        validate_schema(existing_followup_plan_record, "run_followup_plan_record")
        if event_id is None:
            raise InvalidTransition("run_followup_plan_record.json already exists")
        existing_event = _find_run_event_by_id(run_id, event_id, base_dir)
        if existing_event is None:
            raise InvalidTransition("run_followup_plan_record.json already exists")
        if _matches_manual_run_followup_planned_replay(
            existing_event,
            run_id=run_id,
            actor=actor,
            followup_plan_record=followup_plan_record,
        ):
            return existing_followup_plan_record
        if existing_event.get("event_type") == EVENT_TYPE_MANUAL_RUN_FOLLOWUP_PLANNED:
            raise EventConflict(
                f"event_id {event_id!r} already exists with different semantics"
            )
        raise InvalidTransition("run_followup_plan_record.json already exists")

    candidate = make_run_event(
        event_type=EVENT_TYPE_MANUAL_RUN_FOLLOWUP_PLANNED,
        run_id=run_id,
        actor=actor,
        payload={
            "run_id": run_id,
            "source_review_decision": followup_plan_record["source_review_decision"],
            "plan_status": followup_plan_record["plan_status"],
            "planner": followup_plan_record["planner"],
            "run_followup_plan_fingerprint": submitted_fingerprint,
            "run_followup_plan_record_path": str(followup_plan_record_path),
        },
        event_id=event_id,
    )
    existing = _resolve_idempotent_event(run_id, candidate, base_dir)
    if existing is not None:
        return followup_plan_record

    ensure_dir(followup_plan_record_path.parent)
    atomic_write_json(followup_plan_record_path, followup_plan_record)
    append_run_event(run_id, candidate, base_dir)
    return followup_plan_record


def _matches_run_execution_requested_replay(
    existing: dict[str, Any],
    *,
    run_id: str,
    actor: str,
    execution_request_record: dict[str, Any],
) -> bool:
    """Return True when *existing* matches a successful execution request replay."""
    payload = existing.get("payload")
    if not isinstance(payload, dict):
        return False
    return (
        existing.get("event_type") == EVENT_TYPE_RUN_EXECUTION_REQUESTED
        and existing.get("run_id") == run_id
        and existing.get("actor") == actor
        and payload.get("run_id") == run_id
        and payload.get("requester") == execution_request_record["requester"]
        and payload.get("request_status") == execution_request_record["request_status"]
        and payload.get("source_followup_plan_fingerprint")
        == execution_request_record["source_followup_plan_fingerprint"]
        and payload.get("run_execution_request_fingerprint")
        == run_execution_request_fingerprint(execution_request_record)
    )


def request_run_execution(
    run_id: str,
    execution_request_record: dict[str, Any],
    *,
    actor: str = "human",
    event_id: str | None = None,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """Record a review-gated execution request for a planned completed run.

    Converts an approved follow-up plan into a structured execution request.
    This API prepares controlled automation; it does not execute work.
    """
    validate_id(run_id, "run")
    validate_schema(execution_request_record, "run_execution_request_record")
    if execution_request_record["run_id"] != run_id:
        raise ValueError(
            "execution_request_record run_id does not match submission target"
        )

    submitted_fingerprint = run_execution_request_fingerprint(
        execution_request_record
    )
    execution_request_record_path = run_execution_request_record_json_path(
        run_id, base_dir
    )
    followup_plan_record_path = run_followup_plan_record_json_path(run_id, base_dir)
    completion_record_path = run_completion_record_json_path(run_id, base_dir)
    review_record_path = run_review_record_json_path(run_id, base_dir)
    manifest_path = paths.run_manifest_path(run_id, base_dir)

    if not manifest_path.exists():
        raise InvalidTransition(f"run {run_id!r} is not completed")

    current_run_manifest = read_json(manifest_path)
    if current_run_manifest["status"] != RUN_COMPLETED:
        raise InvalidTransition(
            f"run {run_id!r} is not completed; "
            f"status is {current_run_manifest['status']!r}"
        )

    if not completion_record_path.exists():
        raise InvalidTransition("run_completion_record.json is missing")

    if not review_record_path.exists():
        raise InvalidTransition("run_review_record.json is missing")

    if not followup_plan_record_path.exists():
        raise InvalidTransition("run_followup_plan_record.json is missing")

    stored_followup_plan_record = read_json(followup_plan_record_path)
    validate_schema(stored_followup_plan_record, "run_followup_plan_record")
    expected_followup_fingerprint = run_followup_plan_fingerprint(
        stored_followup_plan_record
    )
    if (
        execution_request_record["source_followup_plan_fingerprint"]
        != expected_followup_fingerprint
    ):
        raise InvalidTransition(
            "source_followup_plan_fingerprint does not match run_followup_plan_record"
        )

    if execution_request_record_path.exists():
        existing_execution_request_record = read_json(execution_request_record_path)
        validate_schema(
            existing_execution_request_record, "run_execution_request_record"
        )
        if event_id is None:
            raise InvalidTransition(
                "run_execution_request_record.json already exists"
            )
        existing_event = _find_run_event_by_id(run_id, event_id, base_dir)
        if existing_event is None:
            raise InvalidTransition(
                "run_execution_request_record.json already exists"
            )
        if _matches_run_execution_requested_replay(
            existing_event,
            run_id=run_id,
            actor=actor,
            execution_request_record=execution_request_record,
        ):
            return existing_execution_request_record
        if existing_event.get("event_type") == EVENT_TYPE_RUN_EXECUTION_REQUESTED:
            raise EventConflict(
                f"event_id {event_id!r} already exists with different semantics"
            )
        raise InvalidTransition("run_execution_request_record.json already exists")

    candidate = make_run_event(
        event_type=EVENT_TYPE_RUN_EXECUTION_REQUESTED,
        run_id=run_id,
        actor=actor,
        payload={
            "run_id": run_id,
            "requester": execution_request_record["requester"],
            "request_status": execution_request_record["request_status"],
            "source_followup_plan_fingerprint": execution_request_record[
                "source_followup_plan_fingerprint"
            ],
            "run_execution_request_fingerprint": submitted_fingerprint,
            "run_execution_request_record_path": str(execution_request_record_path),
        },
        event_id=event_id,
    )
    existing = _resolve_idempotent_event(run_id, candidate, base_dir)
    if existing is not None:
        return execution_request_record

    ensure_dir(execution_request_record_path.parent)
    atomic_write_json(execution_request_record_path, execution_request_record)
    append_run_event(run_id, candidate, base_dir)
    return execution_request_record


def _matches_run_execution_completed_replay(
    existing: dict[str, Any],
    *,
    run_id: str,
    actor: str,
    execution_result_record: dict[str, Any],
) -> bool:
    """Return True when *existing* matches a successful execution result replay."""
    payload = existing.get("payload")
    if not isinstance(payload, dict):
        return False
    return (
        existing.get("event_type") == EVENT_TYPE_RUN_EXECUTION_COMPLETED
        and existing.get("run_id") == run_id
        and existing.get("actor") == actor
        and payload.get("run_id") == run_id
        and payload.get("executor") == execution_result_record["executor"]
        and payload.get("result_status") == execution_result_record["result_status"]
        and payload.get("source_execution_request_fingerprint")
        == execution_result_record["source_execution_request_fingerprint"]
        and payload.get("run_execution_result_fingerprint")
        == run_execution_result_fingerprint(execution_result_record)
    )


def execute_run_execution_request(
    project_dir: Path | str,
    run_id: str,
    executor: str,
    *,
    event_id: str | None = None,
    notes: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a controlled one-shot processing pass for a pending execution request.

    Loads the approved execution request from disk, processes each execution item
    without external side effects, stores the result record, and appends an audit
    event. This adapter is manually triggered and does not mutate lifecycle state.
    """
    validate_id(run_id, "run")
    if not isinstance(executor, str) or not executor:
        raise ValueError("executor must be a non-empty string")

    base_dir = Path(project_dir)
    execution_result_record_path = run_execution_result_record_json_path(
        run_id, base_dir
    )
    execution_request_record_path = run_execution_request_record_json_path(
        run_id, base_dir
    )
    followup_plan_record_path = run_followup_plan_record_json_path(run_id, base_dir)
    completion_record_path = run_completion_record_json_path(run_id, base_dir)
    review_record_path = run_review_record_json_path(run_id, base_dir)
    manifest_path = paths.run_manifest_path(run_id, base_dir)

    if not manifest_path.exists():
        raise InvalidTransition(f"run {run_id!r} is not completed")

    current_run_manifest = read_json(manifest_path)
    if current_run_manifest["status"] != RUN_COMPLETED:
        raise InvalidTransition(
            f"run {run_id!r} is not completed; "
            f"status is {current_run_manifest['status']!r}"
        )

    if not completion_record_path.exists():
        raise InvalidTransition("run_completion_record.json is missing")

    if not review_record_path.exists():
        raise InvalidTransition("run_review_record.json is missing")

    if not followup_plan_record_path.exists():
        raise InvalidTransition("run_followup_plan_record.json is missing")

    if not execution_request_record_path.exists():
        raise InvalidTransition("run_execution_request_record.json is missing")

    stored_execution_request_record = read_json(execution_request_record_path)
    validate_schema(stored_execution_request_record, "run_execution_request_record")
    if stored_execution_request_record["run_id"] != run_id:
        raise ValueError(
            "execution_request_record run_id does not match submission target"
        )

    stored_followup_plan_record = read_json(followup_plan_record_path)
    validate_schema(stored_followup_plan_record, "run_followup_plan_record")
    expected_followup_fingerprint = run_followup_plan_fingerprint(
        stored_followup_plan_record
    )
    if (
        stored_execution_request_record["source_followup_plan_fingerprint"]
        != expected_followup_fingerprint
    ):
        raise InvalidTransition(
            "source_followup_plan_fingerprint does not match run_followup_plan_record"
        )

    expected_request_fingerprint = run_execution_request_fingerprint(
        stored_execution_request_record
    )
    reloaded_execution_request_record = read_json(execution_request_record_path)
    if (
        run_execution_request_fingerprint(reloaded_execution_request_record)
        != expected_request_fingerprint
    ):
        raise InvalidTransition(
            "source_execution_request_fingerprint does not match "
            "run_execution_request_record"
        )

    if stored_execution_request_record["request_status"] != EXECUTION_REQUEST_PENDING:
        raise InvalidTransition(
            f"execution request status is "
            f"{stored_execution_request_record['request_status']!r}; "
            "expected pending"
        )

    if execution_result_record_path.exists():
        existing_execution_result_record = read_json(execution_result_record_path)
        validate_schema(existing_execution_result_record, "run_execution_result_record")
        if event_id is None:
            raise InvalidTransition("run_execution_result_record.json already exists")
        existing_event = _find_run_event_by_id(run_id, event_id, base_dir)
        if existing_event is None:
            raise InvalidTransition("run_execution_result_record.json already exists")
        if _matches_run_execution_completed_replay(
            existing_event,
            run_id=run_id,
            actor=executor,
            execution_result_record=existing_execution_result_record,
        ):
            return existing_execution_result_record
        if existing_event.get("event_type") == EVENT_TYPE_RUN_EXECUTION_COMPLETED:
            raise EventConflict(
                f"event_id {event_id!r} already exists with different semantics"
            )
        raise InvalidTransition("run_execution_result_record.json already exists")

    item_results = process_execution_items(
        stored_execution_request_record["execution_items"]
    )
    execution_result_record = make_run_execution_result_record(
        run_id=run_id,
        source_execution_request_fingerprint=expected_request_fingerprint,
        item_results=item_results,
        executor=executor,
        notes=notes,
        metadata=metadata,
    )
    submitted_fingerprint = run_execution_result_fingerprint(execution_result_record)

    candidate = make_run_event(
        event_type=EVENT_TYPE_RUN_EXECUTION_COMPLETED,
        run_id=run_id,
        actor=executor,
        payload={
            "run_id": run_id,
            "executor": executor,
            "result_status": execution_result_record["result_status"],
            "source_execution_request_fingerprint": expected_request_fingerprint,
            "run_execution_result_fingerprint": submitted_fingerprint,
            "run_execution_result_record_path": str(execution_result_record_path),
        },
        event_id=event_id,
    )
    existing = _resolve_idempotent_event(run_id, candidate, base_dir)
    if existing is not None:
        return execution_result_record

    ensure_dir(execution_result_record_path.parent)
    atomic_write_json(execution_result_record_path, execution_result_record)
    append_run_event(run_id, candidate, base_dir)
    return execution_result_record


def _execution_verification_event_type(verification_decision: str) -> str:
    """Return the audit event type for *verification_decision*."""
    return _EXECUTION_VERIFICATION_EVENT_TYPES[verification_decision]


def _matches_run_execution_verification_replay(
    existing: dict[str, Any],
    *,
    run_id: str,
    actor: str,
    verification_record: dict[str, Any],
) -> bool:
    """Return True when *existing* matches a successful verification replay."""
    payload = existing.get("payload")
    if not isinstance(payload, dict):
        return False
    expected_event_type = _execution_verification_event_type(
        verification_record["verification_decision"]
    )
    return (
        existing.get("event_type") == expected_event_type
        and existing.get("run_id") == run_id
        and existing.get("actor") == actor
        and payload.get("run_id") == run_id
        and payload.get("reviewer") == verification_record["reviewer"]
        and payload.get("verification_decision")
        == verification_record["verification_decision"]
        and payload.get("source_execution_result_fingerprint")
        == verification_record["source_execution_result_fingerprint"]
        and payload.get("run_execution_verification_fingerprint")
        == run_execution_verification_fingerprint(verification_record)
    )


def verify_run_execution_result(
    project_dir: Path | str,
    run_id: str,
    verification_record: dict[str, Any],
    actor: str,
    *,
    event_id: str | None = None,
) -> dict[str, Any]:
    """Record a manual verification decision for a completed execution result.

    The verification record is reviewer-provided; this API validates, stores,
    and audits the decision without executing work or mutating prior records.
    """
    validate_id(run_id, "run")
    validate_schema(verification_record, "run_execution_verification_record")
    if verification_record["run_id"] != run_id:
        raise ValueError(
            "verification_record run_id does not match submission target"
        )
    if not isinstance(actor, str) or not actor:
        raise ValueError("actor must be a non-empty string")

    submitted_fingerprint = run_execution_verification_fingerprint(verification_record)
    base_dir = Path(project_dir)
    verification_record_path = run_execution_verification_record_json_path(
        run_id, base_dir
    )
    execution_result_record_path = run_execution_result_record_json_path(
        run_id, base_dir
    )
    execution_request_record_path = run_execution_request_record_json_path(
        run_id, base_dir
    )
    followup_plan_record_path = run_followup_plan_record_json_path(run_id, base_dir)
    completion_record_path = run_completion_record_json_path(run_id, base_dir)
    review_record_path = run_review_record_json_path(run_id, base_dir)
    manifest_path = paths.run_manifest_path(run_id, base_dir)

    if not manifest_path.exists():
        raise InvalidTransition(f"run {run_id!r} is not completed")

    current_run_manifest = read_json(manifest_path)
    if current_run_manifest["status"] != RUN_COMPLETED:
        raise InvalidTransition(
            f"run {run_id!r} is not completed; "
            f"status is {current_run_manifest['status']!r}"
        )

    if not completion_record_path.exists():
        raise InvalidTransition("run_completion_record.json is missing")

    if not review_record_path.exists():
        raise InvalidTransition("run_review_record.json is missing")

    if not followup_plan_record_path.exists():
        raise InvalidTransition("run_followup_plan_record.json is missing")

    if not execution_request_record_path.exists():
        raise InvalidTransition("run_execution_request_record.json is missing")

    if not execution_result_record_path.exists():
        raise InvalidTransition("run_execution_result_record.json is missing")

    stored_execution_result_record = read_json(execution_result_record_path)
    validate_schema(stored_execution_result_record, "run_execution_result_record")
    expected_result_fingerprint = run_execution_result_fingerprint(
        stored_execution_result_record
    )
    if (
        verification_record["source_execution_result_fingerprint"]
        != expected_result_fingerprint
    ):
        raise InvalidTransition(
            "source_execution_result_fingerprint does not match "
            "run_execution_result_record"
        )

    try:
        validate_item_verifications_correspond_to_results(
            verification_record["item_verifications"],
            stored_execution_result_record["item_results"],
        )
    except ValueError as exc:
        raise InvalidTransition(str(exc)) from exc

    if verification_record_path.exists():
        existing_verification_record = read_json(verification_record_path)
        validate_schema(
            existing_verification_record, "run_execution_verification_record"
        )
        if event_id is None:
            raise InvalidTransition(
                "run_execution_verification_record.json already exists"
            )
        existing_event = _find_run_event_by_id(run_id, event_id, base_dir)
        if existing_event is None:
            raise InvalidTransition(
                "run_execution_verification_record.json already exists"
            )
        if _matches_run_execution_verification_replay(
            existing_event,
            run_id=run_id,
            actor=actor,
            verification_record=verification_record,
        ):
            return existing_verification_record
        if existing_event.get("event_type") in _EXECUTION_VERIFICATION_EVENT_TYPES.values():
            raise EventConflict(
                f"event_id {event_id!r} already exists with different semantics"
            )
        raise InvalidTransition(
            "run_execution_verification_record.json already exists"
        )

    event_type = _execution_verification_event_type(
        verification_record["verification_decision"]
    )
    candidate = make_run_event(
        event_type=event_type,
        run_id=run_id,
        actor=actor,
        payload={
            "run_id": run_id,
            "reviewer": verification_record["reviewer"],
            "verification_decision": verification_record["verification_decision"],
            "source_execution_result_fingerprint": verification_record[
                "source_execution_result_fingerprint"
            ],
            "run_execution_verification_fingerprint": submitted_fingerprint,
            "run_execution_verification_record_path": str(verification_record_path),
        },
        event_id=event_id,
    )
    existing = _resolve_idempotent_event(run_id, candidate, base_dir)
    if existing is not None:
        return verification_record

    ensure_dir(verification_record_path.parent)
    atomic_write_json(verification_record_path, verification_record)
    append_run_event(run_id, candidate, base_dir)
    return verification_record


def _matches_run_post_verification_followup_planned_replay(
    existing: dict[str, Any],
    *,
    run_id: str,
    actor: str,
    plan_record: dict[str, Any],
) -> bool:
    """Return True when *existing* matches a successful post-verification plan replay."""
    payload = existing.get("payload")
    if not isinstance(payload, dict):
        return False
    return (
        existing.get("event_type") == EVENT_TYPE_RUN_POST_VERIFICATION_FOLLOWUP_PLANNED
        and existing.get("run_id") == run_id
        and existing.get("actor") == actor
        and payload.get("run_id") == run_id
        and payload.get("planner") == plan_record["planner"]
        and payload.get("plan_status") == plan_record["plan_status"]
        and payload.get("source_execution_result_fingerprint")
        == plan_record["source_execution_result_fingerprint"]
        and payload.get("source_execution_verification_fingerprint")
        == plan_record["source_execution_verification_fingerprint"]
        and payload.get("run_post_verification_followup_plan_fingerprint")
        == run_post_verification_followup_plan_fingerprint(plan_record)
    )


def plan_post_verification_followup(
    project_dir: Path | str,
    run_id: str,
    plan_record: dict[str, Any],
    actor: str,
    *,
    event_id: str | None = None,
) -> dict[str, Any]:
    """Record verification-driven follow-up planning for a verified execution result.

    Planning is based only on execution result and verification records.
    This API stores and audits the plan; it does not execute work.
    """
    validate_id(run_id, "run")
    validate_schema(plan_record, "run_post_verification_followup_plan_record")
    if plan_record["run_id"] != run_id:
        raise ValueError("plan_record run_id does not match submission target")
    if not isinstance(actor, str) or not actor:
        raise ValueError("actor must be a non-empty string")

    submitted_fingerprint = run_post_verification_followup_plan_fingerprint(plan_record)
    base_dir = Path(project_dir)
    plan_record_path = run_post_verification_followup_plan_record_json_path(
        run_id, base_dir
    )
    verification_record_path = run_execution_verification_record_json_path(
        run_id, base_dir
    )
    execution_result_record_path = run_execution_result_record_json_path(
        run_id, base_dir
    )
    execution_request_record_path = run_execution_request_record_json_path(
        run_id, base_dir
    )
    followup_plan_record_path = run_followup_plan_record_json_path(run_id, base_dir)
    completion_record_path = run_completion_record_json_path(run_id, base_dir)
    review_record_path = run_review_record_json_path(run_id, base_dir)
    manifest_path = paths.run_manifest_path(run_id, base_dir)

    if not manifest_path.exists():
        raise InvalidTransition(f"run {run_id!r} is not completed")

    current_run_manifest = read_json(manifest_path)
    if current_run_manifest["status"] != RUN_COMPLETED:
        raise InvalidTransition(
            f"run {run_id!r} is not completed; "
            f"status is {current_run_manifest['status']!r}"
        )

    if not completion_record_path.exists():
        raise InvalidTransition("run_completion_record.json is missing")

    if not review_record_path.exists():
        raise InvalidTransition("run_review_record.json is missing")

    if not followup_plan_record_path.exists():
        raise InvalidTransition("run_followup_plan_record.json is missing")

    if not execution_request_record_path.exists():
        raise InvalidTransition("run_execution_request_record.json is missing")

    if not execution_result_record_path.exists():
        raise InvalidTransition("run_execution_result_record.json is missing")

    if not verification_record_path.exists():
        raise InvalidTransition("run_execution_verification_record.json is missing")

    stored_execution_result_record = read_json(execution_result_record_path)
    validate_schema(stored_execution_result_record, "run_execution_result_record")
    expected_result_fingerprint = run_execution_result_fingerprint(
        stored_execution_result_record
    )
    if plan_record["source_execution_result_fingerprint"] != expected_result_fingerprint:
        raise InvalidTransition(
            "source_execution_result_fingerprint does not match "
            "run_execution_result_record"
        )

    stored_verification_record = read_json(verification_record_path)
    validate_schema(stored_verification_record, "run_execution_verification_record")
    expected_verification_fingerprint = run_execution_verification_fingerprint(
        stored_verification_record
    )
    if (
        plan_record["source_execution_verification_fingerprint"]
        != expected_verification_fingerprint
    ):
        raise InvalidTransition(
            "source_execution_verification_fingerprint does not match "
            "run_execution_verification_record"
        )
    if (
        stored_verification_record["source_execution_result_fingerprint"]
        != expected_result_fingerprint
    ):
        raise InvalidTransition(
            "run_execution_verification_record source_execution_result_fingerprint "
            "does not match run_execution_result_record"
        )

    try:
        validate_post_verification_followup_items_correspond(
            plan_record["followup_items"],
            stored_execution_result_record,
            stored_verification_record,
        )
    except ValueError as exc:
        raise InvalidTransition(str(exc)) from exc

    if plan_record_path.exists():
        existing_plan_record = read_json(plan_record_path)
        validate_schema(
            existing_plan_record, "run_post_verification_followup_plan_record"
        )
        if event_id is None:
            raise InvalidTransition(
                "run_post_verification_followup_plan_record.json already exists"
            )
        existing_event = _find_run_event_by_id(run_id, event_id, base_dir)
        if existing_event is None:
            raise InvalidTransition(
                "run_post_verification_followup_plan_record.json already exists"
            )
        if _matches_run_post_verification_followup_planned_replay(
            existing_event,
            run_id=run_id,
            actor=actor,
            plan_record=plan_record,
        ):
            return existing_plan_record
        if (
            existing_event.get("event_type")
            == EVENT_TYPE_RUN_POST_VERIFICATION_FOLLOWUP_PLANNED
        ):
            raise EventConflict(
                f"event_id {event_id!r} already exists with different semantics"
            )
        raise InvalidTransition(
            "run_post_verification_followup_plan_record.json already exists"
        )

    candidate = make_run_event(
        event_type=EVENT_TYPE_RUN_POST_VERIFICATION_FOLLOWUP_PLANNED,
        run_id=run_id,
        actor=actor,
        payload={
            "run_id": run_id,
            "planner": plan_record["planner"],
            "plan_status": plan_record["plan_status"],
            "source_execution_result_fingerprint": plan_record[
                "source_execution_result_fingerprint"
            ],
            "source_execution_verification_fingerprint": plan_record[
                "source_execution_verification_fingerprint"
            ],
            "run_post_verification_followup_plan_fingerprint": submitted_fingerprint,
            "run_post_verification_followup_plan_record_path": str(plan_record_path),
        },
        event_id=event_id,
    )
    existing = _resolve_idempotent_event(run_id, candidate, base_dir)
    if existing is not None:
        return plan_record

    ensure_dir(plan_record_path.parent)
    atomic_write_json(plan_record_path, plan_record)
    append_run_event(run_id, candidate, base_dir)
    return plan_record
