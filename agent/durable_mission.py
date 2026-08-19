"""Typed, fail-closed durable mission checkpoint primitives.

This module owns checkpoint validation and deterministic projection only.  It
does not grant approval, safety, financial, routing, CodeGraph, or convergence
authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

CHECKPOINT_SCHEMA_VERSION = 1
CHECKPOINT_STATUSES = frozenset(
    {"ACTIVE", "BLOCKED", "WAITING_AUTHORIZATION", "TERMINAL", "FAILED_INTEGRITY"}
)
_MAX_COLLECTION_ITEMS = 100
_MAX_TEXT = 4096
_MAX_PROJECTION = 16384


class MissionStateError(RuntimeError):
    """Base error for fail-closed durable mission handling."""


class MissionCheckpointRequiredError(MissionStateError):
    """Required durable mission checkpoint is unavailable."""


class MissionCheckpointCompatibilityError(MissionStateError):
    """Checkpoint schema cannot be interpreted by this runtime."""


class MissionCheckpointIntegrityError(MissionStateError):
    """Checkpoint fields violate the deterministic state contract."""


@dataclass(frozen=True)
class MissionCheckpoint:
    mission_id: str
    checkpoint_id: str
    parent_checkpoint_id: Optional[str]
    state_version: int
    objective: str
    phase: str
    completed_steps: list[str] = field(default_factory=list)
    pending_steps: list[str] = field(default_factory=list)
    blocker: Optional[str] = None
    blocking_unknown: Optional[str] = None
    next_action: Optional[str] = None
    forbidden_retries: list[str] = field(default_factory=list)
    terminal_state: Optional[str] = None
    status: str = "ACTIVE"
    created_at: Optional[float] = None
    canonical_repo: Optional[str] = None
    repo_observed_head: Optional[str] = None
    codegraph_project: Optional[str] = None
    codegraph_fingerprint: Optional[str] = None
    approval_reference: Optional[Mapping[str, Any]] = None
    safety_reference: Optional[Mapping[str, Any]] = None
    financial_reference: Optional[Mapping[str, Any]] = None
    convergence_reference: Optional[Mapping[str, Any]] = None


def _check_text(name: str, value: Optional[str], *, required: bool = False) -> None:
    if required and not isinstance(value, str):
        raise MissionCheckpointIntegrityError(f"{name} is required")
    if value is not None and (not isinstance(value, str) or len(value) > _MAX_TEXT):
        raise MissionCheckpointIntegrityError(f"{name} is invalid or too long")


def _check_collection(name: str, value: Any) -> None:
    if not isinstance(value, list) or len(value) > _MAX_COLLECTION_ITEMS:
        raise MissionCheckpointIntegrityError(f"{name} is invalid or too large")
    if any(not isinstance(item, str) or len(item) > _MAX_TEXT for item in value):
        raise MissionCheckpointIntegrityError(f"{name} contains invalid item")


def validate_checkpoint(checkpoint: MissionCheckpoint) -> MissionCheckpoint:
    """Validate checkpoint invariants and return the same immutable value."""
    if not isinstance(checkpoint, MissionCheckpoint):
        raise MissionCheckpointIntegrityError("checkpoint has invalid type")
    if checkpoint.state_version != CHECKPOINT_SCHEMA_VERSION:
        raise MissionCheckpointCompatibilityError(
            f"unsupported checkpoint schema version: {checkpoint.state_version}"
        )
    _check_text("mission_id", checkpoint.mission_id, required=True)
    _check_text("checkpoint_id", checkpoint.checkpoint_id, required=True)
    _check_text("parent_checkpoint_id", checkpoint.parent_checkpoint_id)
    _check_text("objective", checkpoint.objective, required=True)
    _check_text("phase", checkpoint.phase, required=True)
    _check_text("blocker", checkpoint.blocker)
    _check_text("blocking_unknown", checkpoint.blocking_unknown)
    _check_text("next_action", checkpoint.next_action)
    _check_text("terminal_state", checkpoint.terminal_state)
    _check_text("status", checkpoint.status, required=True)
    for name, value in (
        ("canonical_repo", checkpoint.canonical_repo),
        ("repo_observed_head", checkpoint.repo_observed_head),
        ("codegraph_project", checkpoint.codegraph_project),
        ("codegraph_fingerprint", checkpoint.codegraph_fingerprint),
    ):
        _check_text(name, value)
    if checkpoint.status not in CHECKPOINT_STATUSES:
        raise MissionCheckpointIntegrityError(f"unknown checkpoint status: {checkpoint.status}")
    _check_collection("completed_steps", checkpoint.completed_steps)
    _check_collection("pending_steps", checkpoint.pending_steps)
    _check_collection("forbidden_retries", checkpoint.forbidden_retries)
    for name, value in (
        ("approval_reference", checkpoint.approval_reference),
        ("safety_reference", checkpoint.safety_reference),
        ("financial_reference", checkpoint.financial_reference),
        ("convergence_reference", checkpoint.convergence_reference),
    ):
        if value is not None and not isinstance(value, Mapping):
            raise MissionCheckpointIntegrityError(f"{name} must be a mapping")

    if checkpoint.blocking_unknown and not checkpoint.blocker:
        raise MissionCheckpointIntegrityError("blocking_unknown requires blocker")
    if checkpoint.status == "ACTIVE":
        if not checkpoint.next_action:
            raise MissionCheckpointIntegrityError("ACTIVE checkpoint requires next_action")
        if checkpoint.terminal_state:
            raise MissionCheckpointIntegrityError("ACTIVE checkpoint cannot be terminal")
    elif checkpoint.status == "BLOCKED":
        if not checkpoint.blocker:
            raise MissionCheckpointIntegrityError("BLOCKED checkpoint requires blocker")
    elif checkpoint.status == "TERMINAL":
        if not checkpoint.terminal_state:
            raise MissionCheckpointIntegrityError("TERMINAL checkpoint requires terminal_state")
        if checkpoint.next_action is not None:
            raise MissionCheckpointIntegrityError("TERMINAL checkpoint forbids next_action")
    elif checkpoint.status == "FAILED_INTEGRITY" and not checkpoint.blocker:
        raise MissionCheckpointIntegrityError("FAILED_INTEGRITY checkpoint requires blocker")
    return checkpoint


def _reference_label(reference: Optional[Mapping[str, Any]]) -> str:
    if not reference:
        return "NONE"
    parts = []
    for key in ("approval_id", "safety_id", "financial_id", "convergence_id", "observed_at"):
        if reference.get(key) is not None:
            parts.append(f"{key}={reference[key]}")
    return ", ".join(parts) if parts else "PRESENT"


def render_mission_projection(checkpoint: MissionCheckpoint) -> str:
    """Render bounded, deterministic, non-authoritative model context."""
    checkpoint = validate_checkpoint(checkpoint)
    lines = [
        "[DURABLE MISSION STATE]",
        f"MISSION_ID: {checkpoint.mission_id}",
        f"OBJECTIVE: {checkpoint.objective}",
        f"PHASE: {checkpoint.phase}",
        f"STATUS: {checkpoint.status}",
        f"CURRENT_BLOCKER: {checkpoint.blocker or 'NONE'}",
        f"BLOCKING_UNKNOWN: {checkpoint.blocking_unknown or 'NONE'}",
        f"COMPLETED_STEPS: {', '.join(checkpoint.completed_steps) or 'NONE'}",
        f"PENDING_STEPS: {', '.join(checkpoint.pending_steps) or 'NONE'}",
        f"FORBIDDEN_RETRIES: {', '.join(checkpoint.forbidden_retries) or 'NONE'}",
        f"NEXT_ACTION: {checkpoint.next_action or 'NONE'}",
        f"CANONICAL_REPO: {checkpoint.canonical_repo or 'NONE'}",
        f"REPO_OBSERVED_HEAD: {checkpoint.repo_observed_head or 'NONE'}",
        f"CODEGRAPH_PROJECT: {checkpoint.codegraph_project or 'NONE'}",
        f"CODEGRAPH_FINGERPRINT: {checkpoint.codegraph_fingerprint or 'NONE'}",
        f"APPROVAL_REFERENCE: {_reference_label(checkpoint.approval_reference)}",
        f"SAFETY_REFERENCE: {_reference_label(checkpoint.safety_reference)}",
        f"FINANCIAL_REFERENCE: {_reference_label(checkpoint.financial_reference)}",
        f"CONVERGENCE_REFERENCE: {_reference_label(checkpoint.convergence_reference)}",
        "RULE: Continue from NEXT_ACTION.",
        "Conversation memory is non-authoritative.",
        "External authorities remain authoritative; references do not grant authority.",
    ]
    projection = "\n".join(lines)
    if len(projection) > _MAX_PROJECTION:
        raise MissionCheckpointIntegrityError("mission projection exceeds bounded size")
    return projection


def render_action_projection(actions) -> str:
    """Render bounded machine-owned execution status for resumed missions."""
    if not actions:
        return ""
    lines = ["[DURABLE ACTION STATE]"]
    for action in actions:
        lines.extend([
            f"ACTION_ID: {action.action_id}",
            f"ACTION_TOOL: {action.tool_name}",
            f"ACTION_STATUS: {action.status.value}",
            f"REPLAY_CLASS: {action.replay_class.value}",
            f"VERIFICATION_REQUIRED: {'true' if action.status.value in {'RUNNING', 'UNKNOWN_OUTCOME', 'VERIFY_REQUIRED'} else 'false'}",
        ])
    lines.append("Action execution status is ledger-owned; conversation memory is non-authoritative.")
    projection = "\n".join(lines)
    if len(projection) > _MAX_PROJECTION:
        raise MissionCheckpointIntegrityError("action projection exceeds bounded size")
    return projection


def restore_mission_for_turn(agent: Any) -> str:
    """Restore required checkpoint or fail before turn/model construction."""
    mission_id = getattr(agent, "mission_id", None)
    db = getattr(agent, "_session_db", None)
    if not mission_id and db is not None and getattr(agent, "session_id", None):
        bound_mission = db.get_mission_for_session(agent.session_id)
        if bound_mission:
            mission_id = bound_mission["mission_id"]
            agent.mission_id = mission_id
    if not mission_id:
        agent._durable_mission_checkpoint = None
        agent._durable_mission_projection = ""
        return ""
    if db is None:
        raise MissionCheckpointRequiredError("durable mission requires SessionDB")
    checkpoint = db.load_mission_checkpoint(mission_id)
    try:
        pending_actions = list(db.list_pending_actions(mission_id))
        for action in pending_actions:
            if action.status.value == "RUNNING":
                db.mark_action_unknown_outcome(
                    action.action_id,
                    error_code="RESUME_AFTER_RUNNING",
                    error_summary="dispatch may have started before mission restoration",
                )
                db.require_action_verification(action.action_id)
        pending_actions = list(db.list_pending_actions(mission_id))
    except Exception as exc:
        raise MissionCheckpointIntegrityError("action ledger recovery failed closed") from exc
    expected_codegraph = getattr(agent, "codegraph_project", None) or getattr(
        agent, "_codegraph_project", None
    )
    if (
        checkpoint.codegraph_project
        and expected_codegraph
        and checkpoint.codegraph_project != expected_codegraph
    ):
        raise MissionCheckpointIntegrityError(
            "checkpoint CodeGraph project does not match canonical binding"
        )
    projection = render_mission_projection(checkpoint)
    action_projection = render_action_projection(pending_actions)
    if action_projection:
        projection = f"{projection}\n\n{action_projection}"
    agent._durable_mission_checkpoint = checkpoint
    agent._durable_mission_projection = projection
    return projection
