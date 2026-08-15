"""Persistent ledger helpers for Herbie's active task supervisor.

The public implementation is deliberately generic and profile-aware. Runtime
state lives under ``$HERMES_HOME/task-supervisor`` by default and must not be
committed to public repositories.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

TASK_STATES = {
    "RECEIVED",
    "PREFLIGHT",
    "ACTIVE",
    "BLOCKED",
    "WAITING_OWNER",
    "READY_FOR_INDEPENDENT_REVIEW",
    "COMPLETE",
    "ABORTED",
    "QUEUED",
}

ACTIVE_MONITORED_STATES = {"ACTIVE", "BLOCKED", "READY_FOR_INDEPENDENT_REVIEW", "COMPLETE"}
TERMINAL_STATES = {"READY_FOR_INDEPENDENT_REVIEW", "COMPLETE", "ABORTED"}

REQUIRED_TASK_FIELDS = [
    "task_id",
    "title",
    "owner",
    "spec_filename",
    "spec_path",
    "spec_version",
    "spec_sha256",
    "status",
    "created_at",
    "started_at",
    "last_progress_at",
    "last_owner_update_at",
    "next_required_owner_update_at",
    "current_step",
    "next_step",
    "percent_or_stage_complete",
    "blocker_type",
    "blocker_detail",
    "checkpoint_commit",
    "checkpoint_artifact_path",
    "tool_budget_risk",
    "owner_notification_state",
    "internal_nudge_state",
    "completion_artifact",
    "closed_at",
]


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def format_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def fingerprint(*parts: Any) -> str:
    raw = "\x1f".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class LedgerPaths:
    base_dir: Path
    active_task: Path
    events: Path
    dedupe: Path


def default_paths(base_dir: Path | None = None) -> LedgerPaths:
    base = base_dir or (get_hermes_home() / "task-supervisor")
    return LedgerPaths(
        base_dir=base,
        active_task=base / "active_task.json",
        events=base / "events.jsonl",
        dedupe=base / "dedupe_state.json",
    )


def ensure_paths(paths: LedgerPaths) -> None:
    paths.base_dir.mkdir(parents=True, exist_ok=True)


def load_task(paths: LedgerPaths) -> dict[str, Any] | None:
    if not paths.active_task.exists():
        return None
    return json.loads(paths.active_task.read_text(encoding="utf-8"))


def save_task(paths: LedgerPaths, task: dict[str, Any]) -> None:
    ensure_paths(paths)
    tmp = paths.active_task.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(task, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(paths.active_task)


def append_event(paths: LedgerPaths, event: dict[str, Any]) -> None:
    ensure_paths(paths)
    with paths.events.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, sort_keys=True) + "\n")


def load_dedupe(paths: LedgerPaths) -> dict[str, Any]:
    if not paths.dedupe.exists():
        return {"incidents": {}}
    return json.loads(paths.dedupe.read_text(encoding="utf-8"))


def save_dedupe(paths: LedgerPaths, state: dict[str, Any]) -> None:
    ensure_paths(paths)
    tmp = paths.dedupe.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(paths.dedupe)


def validate_task(task: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in REQUIRED_TASK_FIELDS:
        if field not in task:
            errors.append(f"missing required field: {field}")
    status = task.get("status")
    if status not in TASK_STATES:
        errors.append(f"invalid status: {status!r}")
    if not task.get("spec_filename") or not task.get("spec_path") or not task.get("spec_sha256"):
        errors.append("missing spec provenance")
    if status == "ACTIVE" and not task.get("last_progress_at"):
        errors.append("ACTIVE task missing last_progress_at")
    if status == "BLOCKED" and not task.get("blocker_detail"):
        errors.append("BLOCKED task missing blocker_detail")
    return errors


def minutes_between(now: datetime, then: str | None) -> float | None:
    parsed = parse_time(then)
    if parsed is None:
        return None
    return (now - parsed).total_seconds() / 60.0


def add_minutes(value: datetime, minutes: int) -> datetime:
    return value + timedelta(minutes=minutes)


def create_task_entry(
    paths: LedgerPaths,
    *,
    task_id: str,
    title: str,
    owner: str,
    spec_filename: str,
    spec_path: str,
    spec_version: str,
    spec_sha256: str,
    now: datetime | None = None,
    parallel_authorized: bool = False,
) -> dict[str, Any]:
    """Create a new task or queue it behind an active owner task.

    This helper enforces the "one active owner task by default" rule for code
    paths that create ledger entries. It records the current task as QUEUED
    when another consequential task is already ACTIVE/BLOCKED/WAITING_OWNER,
    unless parallel execution was explicitly authorized by the owner.
    """

    ensure_paths(paths)
    now = (now or utc_now()).astimezone(UTC).replace(microsecond=0)
    existing = load_task(paths)
    existing_status = existing.get("status") if existing else None
    must_queue = bool(existing_status in {"ACTIVE", "BLOCKED", "WAITING_OWNER"} and not parallel_authorized)
    status = "QUEUED" if must_queue else "RECEIVED"
    task = {
        "task_id": task_id,
        "title": title,
        "owner": owner,
        "spec_filename": spec_filename,
        "spec_path": spec_path,
        "spec_version": spec_version,
        "spec_sha256": spec_sha256,
        "status": status,
        "created_at": format_time(now),
        "started_at": None if must_queue else format_time(now),
        "last_progress_at": None if must_queue else format_time(now),
        "last_owner_update_at": None,
        "next_required_owner_update_at": None if must_queue else format_time(add_minutes(now, 45)),
        "current_step": "Queued behind existing owner task" if must_queue else "Task received",
        "next_step": "Wait for owner authorization or active task closeout" if must_queue else "Run preflight",
        "percent_or_stage_complete": "queued" if must_queue else "received",
        "blocker_type": None,
        "blocker_detail": None,
        "checkpoint_commit": None,
        "checkpoint_artifact_path": None,
        "tool_budget_risk": "normal",
        "owner_notification_state": "pending_queued_notice" if must_queue else "pending_task_accepted_preflight",
        "internal_nudge_state": {},
        "completion_artifact": None,
        "closed_at": None,
    }
    save_task(paths, task)
    append_event(
        paths,
        {
            "at": format_time(now),
            "task_id": task_id,
            "event": "state_transition",
            "from": None,
            "to": status,
            "detail": "parallel owner task queued" if must_queue else "task received",
        },
    )
    return task
