"""Native Kanban exception supervision for gateway-owned delivery.

This module turns otherwise-unsubscribed blocking events into profile-owned
notifications and performs one bounded recovery for agent-owned failures. It
contains no platform sends; the existing Kanban notifier keeps ownership of
retry, cursor, and adapter-routing semantics.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from hermes_cli import kanban_db as kb

_MATERIAL_EVENT_KINDS = ("blocked", "gave_up", "block_loop_detected")
_PROTECTED_GATE_RE = re.compile(
    r"\b(?:"
    r"product (?:decision|judg(?:e)?ment)|business (?:decision|judg(?:e)?ment)|"
    r"credential|credentials|secret|api key|token|password|permission|access grant|"
    r"billing|payment|spend|spending|purchase|destructive|data[- ]loss|"
    r"delete production|production data (?:deletion|migration)|migration risk|"
    r"force[- ]push|history rewrite|irreversible infrastructure|"
    r"infrastructure removal|live trad(?:e|ing)|risk decision|legal|compliance"
    r")\b",
    re.IGNORECASE,
)
_REPLY_MARKER_RE = re.compile(
    r"\[kanban-supervisor:([a-z0-9][a-z0-9_-]{0,63}):(t_[a-zA-Z0-9]+)\]"
)


@dataclass(frozen=True)
class ProactiveSupervisorConfig:
    enabled: bool = False
    platform: str = ""
    chat_id: str = ""
    thread_id: str = ""
    chat_type: str = ""
    recovery_limit: int = 1

    @classmethod
    def from_mapping(cls, raw: Optional[Mapping[str, Any]]) -> "ProactiveSupervisorConfig":
        data = raw if isinstance(raw, Mapping) else {}
        try:
            recovery_limit = max(0, int(data.get("recovery_limit", 1)))
        except (TypeError, ValueError):
            recovery_limit = 1
        return cls(
            enabled=bool(data.get("enabled", False)),
            platform=str(data.get("platform") or "").strip().lower(),
            chat_id=str(data.get("chat_id") or "").strip(),
            thread_id=str(data.get("thread_id") or "").strip(),
            chat_type=str(data.get("chat_type") or "").strip(),
            recovery_limit=recovery_limit,
        )

    @property
    def usable(self) -> bool:
        return self.enabled and bool(self.platform and self.chat_id)


@dataclass
class ReconcileResult:
    protected_gates: list[str] = field(default_factory=list)
    recovered: list[str] = field(default_factory=list)
    recovery_exhausted: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.protected_gates or self.recovered or self.recovery_exhausted)


@dataclass(frozen=True)
class SupervisorReplyResult:
    board: str
    task_id: str
    resumed: bool
    status: str


def is_protected_gate(reason: str) -> bool:
    """Return whether a blocker belongs to an explicit human-only category."""
    return bool(_PROTECTED_GATE_RE.search(reason or ""))


def _event_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _active_blocking_events(conn) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in _MATERIAL_EVENT_KINDS)
    rows = conn.execute(
        f"""
        SELECT t.id AS task_id, t.title, t.status, t.block_kind,
               t.last_failure_error, e.id AS event_id, e.kind AS event_kind,
               e.payload AS event_payload
          FROM tasks t
          JOIN task_events e ON e.id = (
              SELECT MAX(e2.id)
                FROM task_events e2
               WHERE e2.task_id = t.id
                 AND e2.kind IN ({placeholders})
          )
         WHERE t.status IN ('blocked', 'triage')
         ORDER BY e.id
        """,
        _MATERIAL_EVENT_KINDS,
    ).fetchall()
    return [dict(row) for row in rows]


def _ensure_supervisor_subscription(
    conn,
    *,
    board: str,
    task_id: str,
    event_id: int,
    config: ProactiveSupervisorConfig,
    notifier_profile: str,
) -> bool:
    existing = next(
        (
            sub
            for sub in kb.list_notify_subs(conn, task_id)
            if str(sub.get("platform") or "").lower() == config.platform
            and str(sub.get("chat_id") or "") == config.chat_id
            and str(sub.get("thread_id") or "") == config.thread_id
        ),
        None,
    )
    metadata = dict(existing.get("delivery_metadata") or {}) if existing else {}
    metadata["_kanban_proactive_supervisor"] = True
    metadata["_kanban_supervisor_board"] = board
    if config.thread_id:
        metadata.setdefault("thread_id", config.thread_id)
    if config.chat_type:
        metadata.setdefault("chat_type", config.chat_type)
    kb.add_notify_sub(
        conn,
        task_id=task_id,
        platform=config.platform,
        chat_id=config.chat_id,
        chat_type=config.chat_type or None,
        thread_id=config.thread_id or None,
        notifier_profile=notifier_profile,
        delivery_metadata=metadata,
        start_event_id=max(0, int(event_id) - 1),
    )
    return existing is None


def reconcile_board(
    conn,
    *,
    board: str,
    config: ProactiveSupervisorConfig,
    notifier_profile: str,
) -> ReconcileResult:
    """Reconcile active blocking outcomes on one board.

    Protected gates gain a notifier subscription in the configured command
    channel. Agent-owned failures consume a durable per-task recovery budget;
    only an exhausted failure is surfaced, and its message is status-only.
    """
    result = ReconcileResult()
    if not config.usable:
        return result

    for row in _active_blocking_events(conn):
        payload = _event_payload(row.get("event_payload"))
        reason = str(
            payload.get("reason")
            or payload.get("error")
            or row.get("last_failure_error")
            or ""
        ).strip()
        task_id = str(row["task_id"])
        event_id = int(row["event_id"])
        event_kind = str(row["event_kind"])
        block_kind = str(payload.get("kind") or row.get("block_kind") or "")

        protected = block_kind in {"needs_input", "capability"} and is_protected_gate(reason)
        if protected:
            if _ensure_supervisor_subscription(
                conn,
                board=board,
                task_id=task_id,
                event_id=event_id,
                config=config,
                notifier_profile=notifier_profile,
            ):
                result.protected_gates.append(task_id)
            continue

        recovered, state = kb.supervisor_recover_task(
            conn,
            task_id,
            source_event_id=event_id,
            source_kind=event_kind,
            reason=reason or f"agent-owned {event_kind}",
            recovery_limit=config.recovery_limit,
        )
        if recovered:
            result.recovered.append(task_id)
            continue
        if state == "budget_exhausted":
            if _ensure_supervisor_subscription(
                conn,
                board=board,
                task_id=task_id,
                event_id=event_id,
                config=config,
                notifier_profile=notifier_profile,
            ):
                result.recovery_exhausted.append(task_id)

    return result


def render_supervisor_event(*, board: str, task, event) -> Optional[str]:
    """Render a concise gate prompt or exhausted-recovery status message."""
    payload = _event_payload(getattr(event, "payload", None))
    reason = str(
        payload.get("reason")
        or payload.get("error")
        or getattr(task, "last_failure_error", "")
        or ""
    ).strip()
    task_id = str(getattr(task, "id", "") or "")
    title = str(getattr(task, "title", task_id) or task_id)
    block_kind = str(payload.get("kind") or getattr(task, "block_kind", "") or "")
    if block_kind in {"needs_input", "capability"} and is_protected_gate(reason):
        return (
            f"Hermes needs one decision to continue {title}:\n{reason[:700]}\n\n"
            "Reply to this message with the decision. Hermes will attach it to "
            "the existing task and resume the same work graph.\n"
            f"[kanban-supervisor:{board}:{task_id}]"
        )
    return (
        f"Hermes could not recover {title} after the bounded retry. "
        "The task remains paused for engineering follow-up; no decision is requested.\n"
        f"[kanban-supervisor-status:{board}:{task_id}]"
    )


def consume_supervisor_reply(
    *,
    reply_to_text: Optional[str],
    answer: str,
    author: str,
) -> Optional[SupervisorReplyResult]:
    """Consume a direct reply to a native gate prompt and resume that task."""
    match = _REPLY_MARKER_RE.search(reply_to_text or "")
    if match is None:
        return None
    if not answer or not answer.strip():
        return None
    board, task_id = match.groups()
    conn = kb.connect(board=board)
    try:
        resumed, status = kb.resume_supervisor_gate(
            conn,
            task_id,
            answer=answer,
            author=author or "user",
        )
        return SupervisorReplyResult(
            board=board,
            task_id=task_id,
            resumed=resumed,
            status=status,
        )
    finally:
        conn.close()
