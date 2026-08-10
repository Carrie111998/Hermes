"""Persistence-neutral contracts for the project workspace.

The central contract deliberately contains opaque binding IDs rather than host
absolute paths. Filesystem resolution belongs to a device runner.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from pathlib import PureWindowsPath
from typing import Literal

WORKSPACE_SCHEMA_VERSION = 1


class RunState(str, Enum):
    QUEUED = "queued"
    WAITING_FOR_DEVICE = "waiting_for_device"
    OFFERED = "offered"
    ACCEPTED = "accepted"
    RESOLVING_CONTEXT = "resolving_context"
    PREPARING_WORKSPACE = "preparing_workspace"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_REVIEW = "awaiting_review"
    UNCERTAIN = "uncertain"
    RECONCILING = "reconciling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


SyncStatus = Literal["current", "needs_replay"]

_RUN_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.QUEUED: frozenset(
        {RunState.WAITING_FOR_DEVICE, RunState.OFFERED, RunState.CANCELED}
    ),
    RunState.WAITING_FOR_DEVICE: frozenset({RunState.OFFERED, RunState.CANCELED}),
    RunState.OFFERED: frozenset(
        {
            RunState.QUEUED,
            RunState.WAITING_FOR_DEVICE,
            RunState.ACCEPTED,
            RunState.FAILED,
            RunState.CANCELED,
        }
    ),
    RunState.ACCEPTED: frozenset(
        {
            RunState.RESOLVING_CONTEXT,
            RunState.PREPARING_WORKSPACE,
            RunState.FAILED,
            RunState.CANCELED,
        }
    ),
    RunState.RESOLVING_CONTEXT: frozenset(
        {
            RunState.PREPARING_WORKSPACE,
            RunState.AWAITING_APPROVAL,
            RunState.FAILED,
            RunState.CANCELED,
        }
    ),
    RunState.PREPARING_WORKSPACE: frozenset(
        {
            RunState.RUNNING,
            RunState.AWAITING_APPROVAL,
            RunState.FAILED,
            RunState.CANCELED,
        }
    ),
    RunState.RUNNING: frozenset(
        {
            RunState.AWAITING_APPROVAL,
            RunState.AWAITING_REVIEW,
            RunState.COMPLETED,
            RunState.FAILED,
            RunState.CANCELED,
            RunState.UNCERTAIN,
        }
    ),
    RunState.AWAITING_APPROVAL: frozenset(
        {
            RunState.RUNNING,
            RunState.AWAITING_REVIEW,
            RunState.FAILED,
            RunState.CANCELED,
            RunState.UNCERTAIN,
        }
    ),
    RunState.AWAITING_REVIEW: frozenset(
        {RunState.COMPLETED, RunState.FAILED, RunState.CANCELED}
    ),
    RunState.UNCERTAIN: frozenset(
        {RunState.RECONCILING, RunState.FAILED, RunState.CANCELED}
    ),
    RunState.RECONCILING: frozenset(
        {
            RunState.RUNNING,
            RunState.AWAITING_APPROVAL,
            RunState.AWAITING_REVIEW,
            RunState.COMPLETED,
            RunState.FAILED,
            RunState.CANCELED,
            RunState.UNCERTAIN,
        }
    ),
    RunState.COMPLETED: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.CANCELED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class WorkspaceRunProjection:
    run_id: str
    state: RunState
    last_sequence: int
    sync_status: SyncStatus
    last_event_id: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceRunEvent:
    schema_version: int
    event_id: str
    project_id: str
    run_id: str
    attempt_id: str
    sequence: int
    occurred_at: str
    state: RunState


@dataclass(frozen=True, slots=True)
class PushRequest:
    request_id: str
    run_id: str
    commit_sha: str
    diff_digest: str
    remote: str
    remote_url: str
    remote_url_digest: str
    destination_ref: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class PushApprovalDecision:
    request_id: str
    approved: bool
    commit_sha: str
    diff_digest: str
    remote: str
    remote_url: str
    remote_url_digest: str
    destination_ref: str
    decided_at: str


def can_transition(previous: RunState, next_state: RunState) -> bool:
    return next_state in _RUN_TRANSITIONS[previous]


def reduce_run_event(
    projection: WorkspaceRunProjection, event: WorkspaceRunEvent
) -> WorkspaceRunProjection:
    if event.schema_version != WORKSPACE_SCHEMA_VERSION:
        raise ValueError(f"unsupported workspace event schema {event.schema_version}")
    if event.run_id != projection.run_id:
        raise ValueError(
            f"workspace event for {event.run_id} cannot update {projection.run_id}"
        )
    if event.sequence < 1:
        raise ValueError(f"invalid workspace event sequence {event.sequence}")
    if event.sequence == projection.last_sequence:
        if event.event_id == projection.last_event_id and event.state == projection.state:
            return projection
        if projection.sync_status == "needs_replay":
            return projection
        return replace(projection, sync_status="needs_replay")
    if event.sequence < projection.last_sequence:
        if projection.sync_status == "needs_replay":
            return projection
        return replace(projection, sync_status="needs_replay")
    if event.sequence != projection.last_sequence + 1:
        if projection.sync_status == "needs_replay":
            return projection
        return replace(projection, sync_status="needs_replay")
    if not can_transition(projection.state, event.state):
        raise ValueError(
            f"invalid workspace run transition {projection.state.value} -> {event.state.value}"
        )
    return WorkspaceRunProjection(
        run_id=projection.run_id,
        state=event.state,
        last_sequence=event.sequence,
        last_event_id=event.event_id,
        sync_status="current",
    )


def _parse_timestamp(value: str) -> datetime:
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("workspace timestamps must include a timezone")
    return parsed


def approval_is_current(
    *,
    request: PushRequest,
    approval: PushApprovalDecision,
    current_commit_sha: str,
    current_diff_digest: str,
    now: str,
) -> bool:
    try:
        unexpired = _parse_timestamp(now) < _parse_timestamp(request.expires_at)
    except (TypeError, ValueError):
        return False

    return (
        unexpired
        and approval.approved
        and approval.request_id == request.request_id
        and approval.commit_sha == request.commit_sha == current_commit_sha
        and approval.diff_digest == request.diff_digest == current_diff_digest
        and approval.remote == request.remote
        and approval.remote_url == request.remote_url
        and approval.remote_url_digest == request.remote_url_digest
        and approval.destination_ref == request.destination_ref
    )


def normalize_binding_relative_path(value: str) -> str:
    """Normalize a runner path while rejecting all binding-root escapes."""

    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("binding-relative path is required")

    raw = value.replace("\\", "/")
    windows_path = PureWindowsPath(value)
    if raw.startswith("/") or raw.startswith("//") or windows_path.is_absolute() or windows_path.drive:
        raise ValueError("absolute binding paths are not allowed")

    parts: list[str] = []
    for part in raw.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise ValueError("binding path escapes its root")
            parts.pop()
            continue
        parts.append(part)

    if not parts:
        raise ValueError("binding-relative path must name a resource")

    return "/".join(parts)
