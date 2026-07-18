"""HTR task card and attempt result contracts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from htr import paths
from htr.io import atomic_write_json, ensure_dir, read_json, sha256_file
from htr.schemas import validate as validate_schema


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def task_card_json_path(
    run_id: str,
    task_id: str,
    base_dir: Path | None = None,
) -> Path:
    """Return the JSON task card path for *task_id*."""
    return paths.task_dir(run_id, task_id, base_dir) / "task_card.json"


def make_task_card(
    *,
    run_id: str,
    task_id: str,
    title: str,
    instruction: str,
    created_by: str,
    inputs: dict[str, Any] | None = None,
    constraints: dict[str, Any] | None = None,
    acceptance: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    schema_version: str = "1",
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build a validated task card envelope."""
    task_card: dict[str, Any] = {
        "schema_version": schema_version,
        "run_id": run_id,
        "task_id": task_id,
        "title": title,
        "instruction": instruction,
        "created_at": created_at or _utc_now_iso(),
        "created_by": created_by,
        "inputs": inputs if inputs is not None else {},
        "constraints": constraints if constraints is not None else {},
        "acceptance": acceptance if acceptance is not None else {},
        "metadata": metadata if metadata is not None else {},
    }
    validate_schema(task_card, "task_card")
    return task_card


def write_task_card(
    run_id: str,
    task_id: str,
    task_card: dict[str, Any],
    base_dir: Path | None = None,
) -> Path:
    """Atomically write *task_card* to the task workspace."""
    validate_schema(task_card, "task_card")
    if task_card["run_id"] != run_id or task_card["task_id"] != task_id:
        raise ValueError("task_card run_id/task_id do not match write target")
    target = task_card_json_path(run_id, task_id, base_dir)
    ensure_dir(target.parent)
    atomic_write_json(target, task_card)
    return target


def read_task_card(
    run_id: str,
    task_id: str,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """Read and validate the task card for *task_id*."""
    target = task_card_json_path(run_id, task_id, base_dir)
    task_card = read_json(target)
    validate_schema(task_card, "task_card")
    if task_card["run_id"] != run_id or task_card["task_id"] != task_id:
        raise ValueError("task_card run_id/task_id do not match read target")
    return task_card


def make_attempt_result(
    *,
    run_id: str,
    task_id: str,
    attempt_id: str,
    produced_by: str,
    summary: str,
    outputs: dict[str, Any] | None = None,
    artifacts: list[Any] | None = None,
    metrics: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    schema_version: str = "1",
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build a validated attempt result envelope."""
    result: dict[str, Any] = {
        "schema_version": schema_version,
        "run_id": run_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "created_at": created_at or _utc_now_iso(),
        "produced_by": produced_by,
        "summary": summary,
        "outputs": outputs if outputs is not None else {},
        "artifacts": artifacts if artifacts is not None else [],
        "metrics": metrics if metrics is not None else {},
        "metadata": metadata if metadata is not None else {},
    }
    validate_schema(result, "attempt_result")
    return result


def result_fingerprint(result: dict[str, Any]) -> str:
    """Return a stable semantic fingerprint for an attempt result envelope."""
    validate_schema(result, "attempt_result")
    return json.dumps(result, sort_keys=True, ensure_ascii=False)


def compute_sha256(path: Path | str) -> str:
    """Return the lowercase hex SHA-256 digest of *path*."""
    return sha256_file(path)


def verification_result_json_path(
    run_id: str,
    task_id: str,
    attempt_id: str,
    base_dir: Path | None = None,
) -> Path:
    """Return the JSON verification result path for *attempt_id*."""
    return (
        paths.verification_dir(run_id, task_id, attempt_id, base_dir)
        / "verification_result.json"
    )


def make_verification_result(
    *,
    run_id: str,
    task_id: str,
    attempt_id: str,
    outcome: str,
    summary: str | None = None,
    checks: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
    schema_version: str = "1",
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build a validated manual verification result envelope."""
    verification_result: dict[str, Any] = {
        "schema_version": schema_version,
        "run_id": run_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "outcome": outcome,
        "summary": summary,
        "checks": checks if checks is not None else [],
        "metadata": metadata if metadata is not None else {},
        "created_at": created_at or _utc_now_iso(),
    }
    validate_schema(verification_result, "verification_result")
    return verification_result


def verification_fingerprint(verification_result: dict[str, Any]) -> str:
    """Return a stable semantic fingerprint for a verification result envelope."""
    validate_schema(verification_result, "verification_result")
    return json.dumps(
        verification_result,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def task_completion_record_json_path(
    run_id: str,
    task_id: str,
    base_dir: Path | None = None,
) -> Path:
    """Return the JSON task completion record path for *task_id*."""
    return paths.task_dir(run_id, task_id, base_dir) / "task_completion_record.json"


def make_task_completion_record(
    *,
    run_id: str,
    task_id: str,
    attempt_id: str,
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
    schema_version: str = "1",
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build a validated manual task completion record envelope."""
    completion_record: dict[str, Any] = {
        "schema_version": schema_version,
        "run_id": run_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "reason": reason,
        "metadata": metadata if metadata is not None else {},
        "created_at": created_at or _utc_now_iso(),
    }
    validate_schema(completion_record, "task_completion_record")
    return completion_record


def task_completion_fingerprint(completion_record: dict[str, Any]) -> str:
    """Return a stable semantic fingerprint for a task completion record envelope."""
    validate_schema(completion_record, "task_completion_record")
    return json.dumps(
        completion_record,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def run_completion_record_json_path(
    run_id: str,
    base_dir: Path | None = None,
) -> Path:
    """Return the JSON run completion record path for *run_id*."""
    return paths.run_root(run_id, base_dir) / "run_completion_record.json"


def make_run_completion_record(
    *,
    run_id: str,
    completed_task_ids: list[str],
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
    schema_version: str = "1",
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build a validated manual run completion record envelope."""
    completion_record: dict[str, Any] = {
        "schema_version": schema_version,
        "run_id": run_id,
        "completed_task_ids": list(completed_task_ids),
        "reason": reason,
        "metadata": metadata if metadata is not None else {},
        "created_at": created_at or _utc_now_iso(),
    }
    validate_schema(completion_record, "run_completion_record")
    return completion_record


def run_completion_fingerprint(completion_record: dict[str, Any]) -> str:
    """Return a stable semantic fingerprint for a run completion record envelope."""
    validate_schema(completion_record, "run_completion_record")
    return json.dumps(
        completion_record,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


RUN_REVIEW_ACCEPTED = "accepted"
RUN_REVIEW_REJECTED = "rejected"
RUN_REVIEW_NEEDS_FOLLOWUP = "needs_followup"

RUN_REVIEW_DECISIONS: frozenset[str] = frozenset(
    {
        RUN_REVIEW_ACCEPTED,
        RUN_REVIEW_REJECTED,
        RUN_REVIEW_NEEDS_FOLLOWUP,
    }
)


def run_review_record_json_path(
    run_id: str,
    base_dir: Path | None = None,
) -> Path:
    """Return the JSON run review record path for *run_id*."""
    return paths.run_root(run_id, base_dir) / "run_review_record.json"


def make_run_review_record(
    *,
    run_id: str,
    decision: str,
    reviewer: str = "human",
    notes: str | None = None,
    metadata: dict[str, Any] | None = None,
    schema_version: str = "1",
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build a validated manual run review record envelope."""
    review_record: dict[str, Any] = {
        "schema_version": schema_version,
        "run_id": run_id,
        "decision": decision,
        "reviewer": reviewer,
        "notes": notes,
        "metadata": metadata if metadata is not None else {},
        "created_at": created_at or _utc_now_iso(),
    }
    validate_schema(review_record, "run_review_record")
    return review_record


def run_review_fingerprint(review_record: dict[str, Any]) -> str:
    """Return a stable semantic fingerprint for a run review record envelope."""
    validate_schema(review_record, "run_review_record")
    return json.dumps(
        review_record,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


FOLLOWUP_PLAN_OPEN = "open"
FOLLOWUP_PLAN_CANCELLED = "cancelled"

FOLLOWUP_PLAN_STATUSES: frozenset[str] = frozenset(
    {
        FOLLOWUP_PLAN_OPEN,
        FOLLOWUP_PLAN_CANCELLED,
    }
)

FOLLOWUP_ITEM_KINDS: frozenset[str] = frozenset(
    {
        "manual_check",
        "rerun_recommended",
        "documentation_update",
        "external_action",
        "other",
    }
)


def run_followup_plan_record_json_path(
    run_id: str,
    base_dir: Path | None = None,
) -> Path:
    """Return the JSON run follow-up plan record path for *run_id*."""
    return paths.run_root(run_id, base_dir) / "run_followup_plan_record.json"


def _normalize_followup_items(
    followup_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in followup_items:
        normalized.append(
            {
                "item_id": item["item_id"],
                "title": item["title"],
                "kind": item["kind"],
                "rationale": item.get("rationale"),
                "proposed_action": item["proposed_action"],
                "metadata": item["metadata"] if item.get("metadata") is not None else {},
            }
        )
    return normalized


def make_run_followup_plan_record(
    *,
    run_id: str,
    source_review_decision: str,
    summary: str,
    followup_items: list[dict[str, Any]],
    planner: str = "human",
    plan_status: str = FOLLOWUP_PLAN_OPEN,
    notes: str | None = None,
    metadata: dict[str, Any] | None = None,
    schema_version: int = 1,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build a validated review-gated run follow-up plan record envelope."""
    followup_plan_record: dict[str, Any] = {
        "schema_version": schema_version,
        "run_id": run_id,
        "source_review_decision": source_review_decision,
        "planner": planner,
        "plan_status": plan_status,
        "summary": summary,
        "followup_items": _normalize_followup_items(followup_items),
        "notes": notes,
        "metadata": metadata if metadata is not None else {},
        "created_at": created_at or _utc_now_iso(),
    }
    validate_schema(followup_plan_record, "run_followup_plan_record")
    return followup_plan_record


def run_followup_plan_fingerprint(followup_plan_record: dict[str, Any]) -> str:
    """Return a stable semantic fingerprint for a run follow-up plan record."""
    validate_schema(followup_plan_record, "run_followup_plan_record")
    return json.dumps(
        followup_plan_record,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


EXECUTION_REQUEST_PENDING = "pending"
EXECUTION_REQUEST_CANCELLED = "cancelled"

EXECUTION_REQUEST_STATUSES: frozenset[str] = frozenset(
    {
        EXECUTION_REQUEST_PENDING,
        EXECUTION_REQUEST_CANCELLED,
    }
)

EXECUTION_KINDS: frozenset[str] = frozenset(
    {
        "manual_open_link",
        "rerun_task",
        "regenerate_output",
        "update_documentation",
        "external_action",
        "other",
    }
)


def run_execution_request_record_json_path(
    run_id: str,
    base_dir: Path | None = None,
) -> Path:
    """Return the JSON run execution request record path for *run_id*."""
    return paths.run_root(run_id, base_dir) / "run_execution_request_record.json"


def _normalize_execution_items(
    execution_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in execution_items:
        normalized.append(
            {
                "item_id": item["item_id"],
                "source_followup_item_id": item["source_followup_item_id"],
                "title": item["title"],
                "execution_kind": item["execution_kind"],
                "command": item["command"],
                "approval_reason": item.get("approval_reason"),
                "metadata": item["metadata"] if item.get("metadata") is not None else {},
            }
        )
    return normalized


def make_run_execution_request_record(
    *,
    run_id: str,
    source_followup_plan_fingerprint: str,
    execution_items: list[dict[str, Any]],
    requester: str = "human",
    request_status: str = EXECUTION_REQUEST_PENDING,
    notes: str | None = None,
    metadata: dict[str, Any] | None = None,
    schema_version: int = 1,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build a validated review-gated run execution request record envelope."""
    execution_request_record: dict[str, Any] = {
        "schema_version": schema_version,
        "run_id": run_id,
        "source_followup_plan_fingerprint": source_followup_plan_fingerprint,
        "requester": requester,
        "request_status": request_status,
        "execution_items": _normalize_execution_items(execution_items),
        "notes": notes,
        "metadata": metadata if metadata is not None else {},
        "created_at": created_at or _utc_now_iso(),
    }
    validate_schema(execution_request_record, "run_execution_request_record")
    return execution_request_record


def run_execution_request_fingerprint(execution_request_record: dict[str, Any]) -> str:
    """Return a stable semantic fingerprint for a run execution request record."""
    validate_schema(execution_request_record, "run_execution_request_record")
    return json.dumps(
        execution_request_record,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


EXECUTION_RESULT_COMPLETED = "completed"
EXECUTION_RESULT_PARTIAL = "partial"
EXECUTION_RESULT_FAILED = "failed"

EXECUTION_RESULT_STATUSES: frozenset[str] = frozenset(
    {
        EXECUTION_RESULT_COMPLETED,
        EXECUTION_RESULT_PARTIAL,
        EXECUTION_RESULT_FAILED,
    }
)

EXECUTION_ITEM_COMPLETED = "completed"
EXECUTION_ITEM_SKIPPED = "skipped"
EXECUTION_ITEM_FAILED = "failed"
EXECUTION_ITEM_UNSUPPORTED = "unsupported"

EXECUTION_ITEM_STATUSES: frozenset[str] = frozenset(
    {
        EXECUTION_ITEM_COMPLETED,
        EXECUTION_ITEM_SKIPPED,
        EXECUTION_ITEM_FAILED,
        EXECUTION_ITEM_UNSUPPORTED,
    }
)


def run_execution_result_record_json_path(
    run_id: str,
    base_dir: Path | None = None,
) -> Path:
    """Return the JSON run execution result record path for *run_id*."""
    return paths.run_root(run_id, base_dir) / "run_execution_result_record.json"


def _normalize_item_results(
    item_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in item_results:
        normalized.append(
            {
                "item_id": item["item_id"],
                "source_followup_item_id": item["source_followup_item_id"],
                "execution_kind": item["execution_kind"],
                "item_status": item["item_status"],
                "output": item["output"],
                "error": item.get("error"),
                "metadata": item["metadata"] if item.get("metadata") is not None else {},
            }
        )
    return normalized


def process_execution_item(item: dict[str, Any]) -> dict[str, Any]:
    """Process one approved execution item without external side effects."""
    kind = item["execution_kind"]
    command = dict(item["command"])
    base = {
        "item_id": item["item_id"],
        "source_followup_item_id": item["source_followup_item_id"],
        "execution_kind": kind,
        "metadata": item["metadata"] if item.get("metadata") is not None else {},
    }

    if kind == "manual_open_link":
        return {
            **base,
            "item_status": EXECUTION_ITEM_SKIPPED,
            "output": {
                "human_action_required": True,
                "command": command,
            },
            "error": None,
        }

    if kind == "update_documentation":
        return {
            **base,
            "item_status": EXECUTION_ITEM_SKIPPED,
            "output": {
                "proposed_update": command,
                "command": command,
            },
            "error": None,
        }

    if kind == "other":
        if command.get("no_op") is True:
            return {
                **base,
                "item_status": EXECUTION_ITEM_COMPLETED,
                "output": {
                    "no_op_completed": True,
                    "command": command,
                },
                "error": None,
            }
        return {
            **base,
            "item_status": EXECUTION_ITEM_UNSUPPORTED,
            "output": {"command": command},
            "error": "unsupported execution kind handling for other",
        }

    if kind in {"rerun_task", "regenerate_output", "external_action"}:
        return {
            **base,
            "item_status": EXECUTION_ITEM_UNSUPPORTED,
            "output": {"command": command},
            "error": f"{kind} is not supported in Task 10",
        }

    return {
        **base,
        "item_status": EXECUTION_ITEM_UNSUPPORTED,
        "output": {"command": command},
        "error": f"unsupported execution kind {kind!r}",
    }


def process_execution_items(
    execution_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Process approved execution items and return per-item results."""
    return [process_execution_item(item) for item in execution_items]


def compute_execution_result_status(item_results: list[dict[str, Any]]) -> str:
    """Derive aggregate execution result status from item results."""
    if not item_results:
        return EXECUTION_RESULT_FAILED
    completed = sum(
        1
        for item in item_results
        if item["item_status"] == EXECUTION_ITEM_COMPLETED
    )
    if completed == len(item_results):
        return EXECUTION_RESULT_COMPLETED
    if completed > 0:
        return EXECUTION_RESULT_PARTIAL
    return EXECUTION_RESULT_FAILED


def make_run_execution_result_record(
    *,
    run_id: str,
    source_execution_request_fingerprint: str,
    item_results: list[dict[str, Any]],
    executor: str = "human",
    result_status: str | None = None,
    notes: str | None = None,
    metadata: dict[str, Any] | None = None,
    schema_version: int = 1,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build a validated controlled run execution result record envelope."""
    normalized_item_results = _normalize_item_results(item_results)
    resolved_result_status = result_status or compute_execution_result_status(
        normalized_item_results
    )
    execution_result_record: dict[str, Any] = {
        "schema_version": schema_version,
        "run_id": run_id,
        "source_execution_request_fingerprint": source_execution_request_fingerprint,
        "executor": executor,
        "result_status": resolved_result_status,
        "item_results": normalized_item_results,
        "notes": notes,
        "metadata": metadata if metadata is not None else {},
        "created_at": created_at or _utc_now_iso(),
    }
    validate_schema(execution_result_record, "run_execution_result_record")
    return execution_result_record


def run_execution_result_fingerprint(execution_result_record: dict[str, Any]) -> str:
    """Return a stable semantic fingerprint for a run execution result record."""
    validate_schema(execution_result_record, "run_execution_result_record")
    return json.dumps(
        execution_result_record,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


EXECUTION_VERIFICATION_ACCEPTED = "accepted"
EXECUTION_VERIFICATION_REJECTED = "rejected"
EXECUTION_VERIFICATION_NEEDS_CHANGES = "needs_changes"

EXECUTION_VERIFICATION_DECISIONS: frozenset[str] = frozenset(
    {
        EXECUTION_VERIFICATION_ACCEPTED,
        EXECUTION_VERIFICATION_REJECTED,
        EXECUTION_VERIFICATION_NEEDS_CHANGES,
    }
)

EXECUTION_ITEM_VERIFICATION_ACCEPTED = "accepted"
EXECUTION_ITEM_VERIFICATION_REJECTED = "rejected"
EXECUTION_ITEM_VERIFICATION_NEEDS_CHANGES = "needs_changes"
EXECUTION_ITEM_VERIFICATION_NOT_REVIEWED = "not_reviewed"

EXECUTION_ITEM_VERIFICATION_DECISIONS: frozenset[str] = frozenset(
    {
        EXECUTION_ITEM_VERIFICATION_ACCEPTED,
        EXECUTION_ITEM_VERIFICATION_REJECTED,
        EXECUTION_ITEM_VERIFICATION_NEEDS_CHANGES,
        EXECUTION_ITEM_VERIFICATION_NOT_REVIEWED,
    }
)


def run_execution_verification_record_json_path(
    run_id: str,
    base_dir: Path | None = None,
) -> Path:
    """Return the JSON run execution verification record path for *run_id*."""
    return paths.run_root(run_id, base_dir) / "run_execution_verification_record.json"


def _normalize_item_verifications(
    item_verifications: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in item_verifications:
        normalized.append(
            {
                "item_id": item["item_id"],
                "source_followup_item_id": item["source_followup_item_id"],
                "execution_kind": item["execution_kind"],
                "item_status": item["item_status"],
                "verification_decision": item["verification_decision"],
                "reviewer_notes": item.get("reviewer_notes"),
                "evidence": item["evidence"] if item.get("evidence") is not None else {},
                "metadata": item["metadata"] if item.get("metadata") is not None else {},
            }
        )
    return normalized


def _validate_execution_verification_decision_consistency(
    verification_record: dict[str, Any],
) -> None:
    """Validate run-level and item-level verification decision consistency."""
    decision = verification_record["verification_decision"]
    item_decisions = [
        item["verification_decision"]
        for item in verification_record["item_verifications"]
    ]

    if decision == EXECUTION_VERIFICATION_ACCEPTED:
        if item_decisions and not all(
            d == EXECUTION_ITEM_VERIFICATION_ACCEPTED for d in item_decisions
        ):
            raise ValueError(
                "run_execution_verification_record: accepted decision requires all "
                "item_verifications to be accepted"
            )
    elif decision == EXECUTION_VERIFICATION_REJECTED:
        if EXECUTION_ITEM_VERIFICATION_REJECTED not in item_decisions:
            raise ValueError(
                "run_execution_verification_record: rejected decision requires at "
                "least one item_verification rejected"
            )
    elif decision == EXECUTION_VERIFICATION_NEEDS_CHANGES:
        if EXECUTION_ITEM_VERIFICATION_NEEDS_CHANGES not in item_decisions:
            raise ValueError(
                "run_execution_verification_record: needs_changes decision requires "
                "at least one item_verification needs_changes"
            )

    if any(
        d == EXECUTION_ITEM_VERIFICATION_NOT_REVIEWED for d in item_decisions
    ) and decision not in {
        EXECUTION_VERIFICATION_REJECTED,
        EXECUTION_VERIFICATION_NEEDS_CHANGES,
    }:
        raise ValueError(
            "run_execution_verification_record: not_reviewed items are allowed only "
            "when verification_decision is rejected or needs_changes"
        )


def validate_item_verifications_correspond_to_results(
    item_verifications: list[dict[str, Any]],
    item_results: list[dict[str, Any]],
    *,
    allow_partial: bool = False,
) -> None:
    """Ensure item verifications align with execution result item_results."""
    result_by_id = {item["item_id"]: item for item in item_results}
    verification_by_id = {item["item_id"]: item for item in item_verifications}

    if not allow_partial:
        if set(result_by_id) != set(verification_by_id):
            raise ValueError(
                "item_verifications item_id set does not match item_results"
            )

    for item_id, verification in verification_by_id.items():
        if item_id not in result_by_id:
            raise ValueError(
                f"item_verifications contains unknown item_id {item_id!r}"
            )
        result = result_by_id[item_id]
        for field in ("source_followup_item_id", "execution_kind", "item_status"):
            if verification[field] != result[field]:
                raise ValueError(
                    f"item_verifications {field} does not match item_results for "
                    f"{item_id!r}"
                )

    if not allow_partial:
        missing = set(result_by_id) - set(verification_by_id)
        if missing:
            raise ValueError(
                "item_verifications missing item_id(s): "
                + ", ".join(sorted(missing))
            )


def make_run_execution_verification_record(
    *,
    run_id: str,
    source_execution_result_fingerprint: str,
    item_verifications: list[dict[str, Any]],
    reviewer: str = "human",
    verification_decision: str = EXECUTION_VERIFICATION_ACCEPTED,
    notes: str | None = None,
    metadata: dict[str, Any] | None = None,
    schema_version: int = 1,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build a validated manual run execution verification record envelope."""
    verification_record: dict[str, Any] = {
        "schema_version": schema_version,
        "run_id": run_id,
        "source_execution_result_fingerprint": source_execution_result_fingerprint,
        "reviewer": reviewer,
        "verification_decision": verification_decision,
        "item_verifications": _normalize_item_verifications(item_verifications),
        "notes": notes,
        "metadata": metadata if metadata is not None else {},
        "created_at": created_at or _utc_now_iso(),
    }
    validate_schema(verification_record, "run_execution_verification_record")
    return verification_record


def run_execution_verification_fingerprint(
    verification_record: dict[str, Any],
) -> str:
    """Return a stable semantic fingerprint for a run execution verification record."""
    validate_schema(verification_record, "run_execution_verification_record")
    return json.dumps(
        verification_record,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
