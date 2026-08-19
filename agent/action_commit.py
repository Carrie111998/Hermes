"""Durable action identity, replay policy, and lifecycle rules for P2.

This module owns execution status only. Mission progression remains owned by
the P1 checkpoint model and external approval/safety/financial authorities
remain outside this ledger.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping


class ActionLedgerError(RuntimeError):
    """Base error for invalid or unsafe action-ledger operations."""


class ActionIntegrityError(ActionLedgerError):
    """Persisted action data is corrupt or violates its invariants."""


class ActionExecutionError(ActionLedgerError):
    """Tool execution failed with an explicit side-effect boundary."""

    def __init__(self, message: str, *, side_effect_started: bool = True):
        super().__init__(message)
        self.side_effect_started = side_effect_started


class ActionStatus(str, Enum):
    PLANNED = "PLANNED"
    AUTHORIZED = "AUTHORIZED"
    RUNNING = "RUNNING"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"
    VERIFY_REQUIRED = "VERIFY_REQUIRED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


class ReplayClass(str, Enum):
    SAFE_TO_REPLAY = "SAFE_TO_REPLAY"
    MUST_REQUERY_EXTERNAL_STATE = "MUST_REQUERY_EXTERNAL_STATE"
    VERIFY_BEFORE_REPLAY = "VERIFY_BEFORE_REPLAY"
    NEVER_REPLAY_WITHOUT_NEW_AUTHORIZATION = "NEVER_REPLAY_WITHOUT_NEW_AUTHORIZATION"


_SECRET_KEY = re.compile(r"(?:token|secret|password|passwd|authorization|credential|api[_-]?key|private[_-]?key)", re.I)
_MAX_SUMMARY_BYTES = 8 * 1024
_MAX_ERROR_BYTES = 2 * 1024
_MAX_REF_BYTES = 512


@dataclass(frozen=True)
class ActionRecord:
    action_id: str
    mission_id: str | None
    checkpoint_id: str | None
    parent_action_id: str | None
    action_type: str
    tool_name: str
    input_fingerprint: str
    input_summary: dict[str, Any] = field(default_factory=dict)
    status: ActionStatus = ActionStatus.PLANNED
    replay_class: ReplayClass = ReplayClass.NEVER_REPLAY_WITHOUT_NEW_AUTHORIZATION
    freshness_policy: str | None = None
    created_at: float = 0.0
    authorized_at: float | None = None
    started_at: float | None = None
    completed_at: float | None = None
    updated_at: float = 0.0
    result_ref: str | None = None
    verification_ref: str | None = None
    error_code: str | None = None
    error_summary: str | None = None
    external_authority_ref: str | None = None
    approval_ref: str | None = None


def new_action_id() -> str:
    """Generate an opaque durable identity independent of session/tool IDs."""
    return f"act_{uuid.uuid4().hex}"


def _safe_summary(value: Any, key: str | None = None) -> Any:
    if key and _SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(k): _safe_summary(v, str(k)) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_safe_summary(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def safe_input_summary(args: Mapping[str, Any]) -> dict[str, Any]:
    summary = _safe_summary(args)
    encoded = json.dumps(summary, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if len(encoded.encode("utf-8")) > _MAX_SUMMARY_BYTES:
        raise ActionIntegrityError("action input summary exceeds bounded size")
    return summary


def canonical_input_fingerprint(
    tool_name: str, args: Mapping[str, Any], context: Mapping[str, Any] | None = None
) -> str:
    """Hash all semantic input while storing only redacted bounded metadata."""
    payload = {
        "tool_name": str(tool_name),
        "arguments": _canonical_value(args),
        "execution_context": _canonical_value(context or {}),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _canonical_value(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


_PURE_READ_TOOLS = frozenset({
    "session_search", "search_files", "list_files", "read_file", "grep", "codegraph_explore",
    "memory_search", "todo", "tool_search", "tool_describe",
})
_EXTERNAL_READ_TOOLS = frozenset({"external_read", "browser", "web_search", "http_get"})
_VERIFY_TOOLS = frozenset({"write_file", "patch", "terminal", "git_commit", "git_apply", "package_install"})
_STRICT_PREFIXES = ("deploy", "financial", "campaign", "play", "approval", "publish", "delete", "destroy")


def classify_replay_policy(tool_name: str, args: Mapping[str, Any] | None = None) -> ReplayClass:
    """Return deterministic replay policy; model prose is never consulted."""
    name = str(tool_name or "").strip().lower()
    if name in _PURE_READ_TOOLS:
        return ReplayClass.SAFE_TO_REPLAY
    if name in _EXTERNAL_READ_TOOLS:
        return ReplayClass.MUST_REQUERY_EXTERNAL_STATE
    if name in _VERIFY_TOOLS:
        return ReplayClass.VERIFY_BEFORE_REPLAY
    if any(name == prefix or name.startswith(prefix + "_") for prefix in _STRICT_PREFIXES):
        return ReplayClass.NEVER_REPLAY_WITHOUT_NEW_AUTHORIZATION
    if name.startswith("read_") or name.endswith("_search"):
        return ReplayClass.SAFE_TO_REPLAY
    return ReplayClass.NEVER_REPLAY_WITHOUT_NEW_AUTHORIZATION


_TRANSITIONS: dict[ActionStatus, frozenset[ActionStatus]] = {
    ActionStatus.PLANNED: frozenset({ActionStatus.AUTHORIZED, ActionStatus.REJECTED, ActionStatus.SUPERSEDED}),
    ActionStatus.AUTHORIZED: frozenset({ActionStatus.RUNNING, ActionStatus.REJECTED, ActionStatus.SUPERSEDED}),
    ActionStatus.RUNNING: frozenset({ActionStatus.COMMITTED, ActionStatus.FAILED, ActionStatus.UNKNOWN_OUTCOME}),
    ActionStatus.UNKNOWN_OUTCOME: frozenset({ActionStatus.VERIFY_REQUIRED}),
    ActionStatus.VERIFY_REQUIRED: frozenset({ActionStatus.COMMITTED, ActionStatus.FAILED}),
    ActionStatus.COMMITTED: frozenset(),
    ActionStatus.FAILED: frozenset(),
    ActionStatus.REJECTED: frozenset(),
    ActionStatus.SUPERSEDED: frozenset(),
}


def validate_transition(current: ActionStatus, target: ActionStatus) -> None:
    current = ActionStatus(current)
    target = ActionStatus(target)
    if target not in _TRANSITIONS[current]:
        raise ActionLedgerError(f"invalid action transition: {current.value} -> {target.value}")


def validate_action(action: ActionRecord) -> ActionRecord:
    if not action.action_id or not action.tool_name or not action.input_fingerprint:
        raise ActionIntegrityError("action identity, tool, and fingerprint are required")
    try:
        status = ActionStatus(action.status)
        replay_class = ReplayClass(action.replay_class)
    except ValueError as exc:
        raise ActionIntegrityError("action status or replay class is unsupported") from exc
    if len(action.input_fingerprint) != 64:
        raise ActionIntegrityError("action fingerprint is invalid")
    summary = safe_input_summary(action.input_summary)
    if status is ActionStatus.RUNNING and action.started_at is None:
        raise ActionIntegrityError("RUNNING action requires started_at")
    if status is ActionStatus.COMMITTED and not (action.completed_at and action.result_ref):
        raise ActionIntegrityError("COMMITTED action requires completion and result reference")
    if status is ActionStatus.UNKNOWN_OUTCOME and not action.error_code:
        raise ActionIntegrityError("UNKNOWN_OUTCOME requires error code")
    if status is ActionStatus.VERIFY_REQUIRED and not action.verification_ref:
        raise ActionIntegrityError("VERIFY_REQUIRED requires verification reference")
    return replace(action, status=status, replay_class=replay_class, input_summary=summary)


def resolve_resume_state(action: ActionRecord) -> ActionStatus:
    """Return the machine-owned status required before any resumed dispatch."""
    if action.status in {ActionStatus.RUNNING, ActionStatus.UNKNOWN_OUTCOME}:
        return ActionStatus.VERIFY_REQUIRED
    return action.status


def _blocked_result(action: ActionRecord, message: str) -> str:
    return json.dumps({
        "error": message,
        "status": "blocked",
        "action_id": action.action_id,
        "action_status": action.status.value,
        "replay_class": action.replay_class.value,
        "verification_required": action.status in {
            ActionStatus.RUNNING, ActionStatus.UNKNOWN_OUTCOME, ActionStatus.VERIFY_REQUIRED
        },
    }, ensure_ascii=False, sort_keys=True)


def execute_with_ledger(
    db: Any,
    *,
    mission_id: str | None,
    checkpoint_id: str | None,
    tool_name: str,
    function_args: Mapping[str, Any],
    execute,
    action_type: str = "tool",
    external_authority_ref: str | None = None,
    approval_ref: str | None = None,
    owner: Any = None,
    identity_context: Mapping[str, Any] | None = None,
) -> Any:
    """Guard one already-authorized dispatch with durable action state.

    A missing mission/database preserves ordinary Hermes behavior. Durable
    missions require the ledger and never silently fall back to transcript
    reconstruction.
    """
    if not mission_id or db is None:
        return execute()

    fingerprint = canonical_input_fingerprint(tool_name, function_args, identity_context)
    replay_class = classify_replay_policy(tool_name, function_args)
    existing = db.find_action_by_fingerprint(mission_id, tool_name, fingerprint)
    prior_action = None
    if existing is not None:
        if existing.status is ActionStatus.COMMITTED:
            if replay_class is not ReplayClass.MUST_REQUERY_EXTERNAL_STATE:
                return _blocked_result(existing, "action already committed; dispatch suppressed")
            # External reads are deliberately re-queried under a new action
            # identity while retaining the prior action as lineage.
            prior_action = existing
            existing = None
        if existing is not None and existing.status is ActionStatus.FAILED:
            existing = None
        elif existing is not None and existing.status is ActionStatus.PLANNED:
            db.authorize_action(existing.action_id)
        elif existing is not None and existing.status is ActionStatus.AUTHORIZED:
            pass
        elif existing is not None:
            if existing.status is ActionStatus.RUNNING:
                try:
                    db.mark_action_unknown_outcome(
                        existing.action_id,
                        error_code="RESUME_AFTER_RUNNING",
                        error_summary="dispatch may have started before process/session resume",
                    )
                    db.require_action_verification(existing.action_id)
                    existing = db.get_action(existing.action_id)
                except Exception as exc:
                    if owner is not None:
                        owner._action_commit_blocked = True
                    raise ActionLedgerError("cannot recover running action safely") from exc
            if existing.status is ActionStatus.UNKNOWN_OUTCOME:
                try:
                    db.require_action_verification(existing.action_id)
                    existing = db.get_action(existing.action_id)
                except Exception as exc:
                    if owner is not None:
                        owner._action_commit_blocked = True
                    raise ActionLedgerError("cannot require verification for unknown action") from exc
            return _blocked_result(existing, "unresolved action requires verification before replay")

    if existing is None:
        action = db.create_action(
            mission_id=mission_id,
            checkpoint_id=checkpoint_id,
            action_type=action_type,
            tool_name=tool_name,
            input_fingerprint=fingerprint,
            replay_class=replay_class.value,
            input_summary=safe_input_summary(function_args),
            parent_action_id=(prior_action.action_id if prior_action is not None else (
                db.find_action_by_fingerprint(mission_id, tool_name, fingerprint).action_id
                if db.find_action_by_fingerprint(mission_id, tool_name, fingerprint) is not None
                else None
            )),
            external_authority_ref=external_authority_ref,
            approval_ref=approval_ref,
        )
        db.authorize_action(action.action_id)
    else:
        action = existing

    pending = [item for item in db.list_pending_actions(mission_id) if item.action_id != action.action_id]
    if pending:
        return _blocked_result(pending[0], "mission has an unresolved action requiring verification")

    db.mark_action_running(action.action_id)
    try:
        result = execute()
    except ActionExecutionError as exc:
        if exc.side_effect_started:
            db.mark_action_unknown_outcome(
                action.action_id, error_code="DISPATCH_EXCEPTION", error_summary=str(exc)
            )
        else:
            db.mark_action_failed(
                action.action_id, error_code="DISPATCH_FAILED", error_summary=str(exc)
            )
        raise
    except BaseException as exc:
        db.mark_action_unknown_outcome(
            action.action_id, error_code="DISPATCH_UNCERTAIN", error_summary=str(exc)
        )
        raise

    try:
        db.mark_action_committed(action.action_id, result_ref=f"action:{action.action_id}")
    except BaseException as exc:
        try:
            db.mark_action_unknown_outcome(
                action.action_id, error_code="LEDGER_COMMIT_FAILED", error_summary=str(exc)
            )
        except BaseException as unknown_exc:
            if owner is not None:
                owner._action_commit_blocked = True
            raise ActionLedgerError("action outcome persistence failed; continuation blocked") from unknown_exc
        if owner is not None:
            owner._action_commit_blocked = True
        raise ActionLedgerError("action outcome persistence failed; continuation blocked") from exc
    return result
