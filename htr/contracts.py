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
