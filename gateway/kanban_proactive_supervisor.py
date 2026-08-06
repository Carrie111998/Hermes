"""Native Kanban exception supervision for gateway-owned delivery.

This module turns otherwise-unsubscribed blocking events into profile-owned
notifications and performs one bounded recovery for agent-owned failures. It
contains no platform sends; the existing Kanban notifier keeps ownership of
retry, cursor, and adapter-routing semantics.
"""
from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from hermes_cli import kanban_db as kb

_MATERIAL_EVENT_KINDS = ("blocked", "gave_up", "block_loop_detected")
_REPLY_MARKER_RE = re.compile(r"\[kanban-gate:([a-f0-9]{32})\]", re.IGNORECASE)


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


def is_supervisor_gate_reply(
    reply_to_text: Optional[str], *, reply_to_is_own_message: bool
) -> bool:
    """Return whether an authenticated reply targets a supervisor gate prompt."""
    return bool(
        reply_to_is_own_message and _REPLY_MARKER_RE.search(reply_to_text or "")
    )


def _event_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _active_blocking_events(conn) -> list[dict[str, Any]]:
    state_placeholders = ",".join("?" for _ in kb.SUPERVISOR_STATE_EVENT_KINDS)
    material_placeholders = ",".join("?" for _ in _MATERIAL_EVENT_KINDS)
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
                 AND e2.kind IN ({state_placeholders})
          )
         WHERE t.status IN ('blocked', 'triage')
           AND e.kind IN ({material_placeholders})
         ORDER BY e.id
        """,
        (*kb.SUPERVISOR_STATE_EVENT_KINDS, *_MATERIAL_EVENT_KINDS),
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
    mode: str,
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
    if existing:
        existing_profile = str(existing.get("notifier_profile") or "").strip()
        if existing_profile and existing_profile != notifier_profile:
            # The route key is unique without profile in the DB schema. Never
            # repurpose another profile's subscription: doing so would make its
            # bot deliver a gate configured and consumed by this profile.
            return False
    metadata = dict(existing.get("delivery_metadata") or {}) if existing else {}
    if (
        metadata.get("_kanban_supervisor_event_id") == int(event_id)
        and metadata.get("_kanban_supervisor_mode") == mode
    ):
        return False
    token = secrets.token_hex(16)
    metadata["_kanban_proactive_supervisor"] = True
    metadata["_kanban_supervisor_board"] = board
    metadata["_kanban_supervisor_task"] = task_id
    metadata["_kanban_supervisor_event_id"] = int(event_id)
    metadata["_kanban_supervisor_gate_token"] = token
    metadata["_kanban_supervisor_mode"] = mode
    metadata["_kanban_supervisor_recovery_limit"] = config.recovery_limit
    metadata["_kanban_supervisor_owned_subscription"] = bool(
        metadata.get("_kanban_supervisor_owned_subscription", existing is None)
    )
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
    # A pre-existing ordinary subscription may already have claimed this event
    # before supervision was enabled. Rewind only to the still-current event.
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE kanban_notify_subs SET last_event_id = MIN(last_event_id, ?) "
            "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?",
            (
                max(0, int(event_id) - 1), task_id, config.platform,
                config.chat_id, config.thread_id,
            ),
        )
    return True


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

        # Recovery is allowlisted, not inferred. Legacy, malformed, plugin, and
        # future block kinds remain human gates until they opt into the typed
        # transient contract. A block-loop event is always deliberate triage,
        # even when the repeated underlying blocker was transient.
        recoverable = (
            block_kind == "transient" and event_kind in {"blocked", "gave_up"}
        )
        protected = not recoverable
        if protected:
            if _ensure_supervisor_subscription(
                conn,
                board=board,
                task_id=task_id,
                event_id=event_id,
                config=config,
                notifier_profile=notifier_profile,
                mode="protected_gate",
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
                mode="recovery_exhausted",
            ):
                result.recovery_exhausted.append(task_id)

    return result


def render_supervisor_event(
    *,
    board: str,
    task,
    event,
    delivery_metadata: Optional[Mapping[str, Any]] = None,
    current_event_id: Optional[int] = None,
) -> Optional[str]:
    """Render one event-bound supervisor message.

    An empty string means the event belongs to a supervisor-owned subscription
    but is stale and must be silent. ``None`` leaves an unrelated event to the
    ordinary notifier renderer when a pre-existing subscription was reused.
    """
    payload = _event_payload(getattr(event, "payload", None))
    reason = str(
        payload.get("reason")
        or payload.get("error")
        or getattr(task, "last_failure_error", "")
        or ""
    ).strip()
    task_id = str(getattr(task, "id", "") or "")
    title = str(getattr(task, "title", task_id) or task_id)
    metadata = delivery_metadata if isinstance(delivery_metadata, Mapping) else {}
    try:
        bound_event_id = int(metadata.get("_kanban_supervisor_event_id", -1))
    except (TypeError, ValueError):
        bound_event_id = -1
    event_id = int(getattr(event, "id", -2))
    owned = bool(metadata.get("_kanban_supervisor_owned_subscription"))
    # State truth outranks subscription ownership. A reused ordinary
    # subscription must not fall back to its generic "blocked" renderer after
    # the supervisor already recovered this event (or another actor resolved
    # it); that would report a task as paused when it is actually ready.
    if current_event_id != event_id or getattr(task, "status", None) not in {
        "blocked", "triage"
    }:
        return ""
    if bound_event_id != event_id:
        return "" if owned else None

    mode = str(metadata.get("_kanban_supervisor_mode") or "")
    if mode == "protected_gate":
        token = str(metadata.get("_kanban_supervisor_gate_token") or "")
        if not re.fullmatch(r"[a-f0-9]{32}", token):
            return ""
        return (
            f"Hermes needs one decision to continue {title}:\n{reason[:700]}\n\n"
            "Reply to this message with the decision. Hermes will attach it to "
            "the existing task and resume the same work graph.\n"
            f"[kanban-gate:{token}]"
        )
    if mode != "recovery_exhausted":
        return ""
    try:
        recovery_limit = int(metadata.get("_kanban_supervisor_recovery_limit", 1))
    except (TypeError, ValueError):
        recovery_limit = 1
    if recovery_limit <= 0:
        return (
            f"Hermes did not retry {title} because its recovery budget is zero. "
            "The task remains paused for engineering follow-up; no decision is requested."
        )
    return (
        f"Hermes could not recover {title} after the bounded retry. "
        "The task remains paused for engineering follow-up; no decision is requested."
    )


def record_supervisor_delivery_message(
    *,
    board: str,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: str,
    notifier_profile: str,
    expected_event_id: int,
    expected_token: str,
    message_id: str,
) -> bool:
    """Bind a protected gate generation to the exact delivered bot message."""
    if not message_id:
        return False
    conn = kb.connect(board=board)
    try:
        with kb.write_txn(conn):
            row = conn.execute(
                "SELECT notifier_profile, delivery_metadata "
                "FROM kanban_notify_subs WHERE task_id = ? AND platform = ? "
                "AND chat_id = ? AND thread_id = ?",
                (task_id, platform, chat_id, thread_id or ""),
            ).fetchone()
            if row is None:
                return False
            owner = str(row["notifier_profile"] or "")
            if notifier_profile and owner and owner != notifier_profile:
                return False
            metadata = _event_payload(row["delivery_metadata"])
            if metadata.get("_kanban_supervisor_mode") != "protected_gate":
                return False
            if metadata.get("_kanban_supervisor_gate_token") != expected_token:
                return False
            try:
                raw_event_id = metadata.get("_kanban_supervisor_event_id")
                bound_event_id = int(str(raw_event_id))
            except (TypeError, ValueError):
                return False
            if bound_event_id != int(expected_event_id):
                return False
            placeholders = ",".join("?" for _ in kb.SUPERVISOR_STATE_EVENT_KINDS)
            current = conn.execute(
                f"SELECT id FROM task_events WHERE task_id = ? "
                f"AND kind IN ({placeholders}) ORDER BY id DESC LIMIT 1",
                (task_id, *kb.SUPERVISOR_STATE_EVENT_KINDS),
            ).fetchone()
            if current is None or int(current["id"]) != int(expected_event_id):
                return False
            metadata["_kanban_supervisor_message_id"] = str(message_id)
            encoded = json.dumps(metadata, separators=(",", ":"), sort_keys=True)
            updated = conn.execute(
                "UPDATE kanban_notify_subs SET delivery_metadata = ? "
                "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ? "
                "AND delivery_metadata = ?",
                (
                    encoded,
                    task_id,
                    platform,
                    chat_id,
                    thread_id or "",
                    row["delivery_metadata"],
                ),
            )
            return updated.rowcount == 1
    finally:
        conn.close()


def consume_supervisor_reply(
    *,
    reply_to_text: Optional[str],
    reply_to_message_id: Optional[str],
    answer: str,
    author: str,
    platform: str,
    chat_id: str,
    thread_id: str = "",
    notifier_profile: str = "",
    reply_to_is_own_message: bool = False,
) -> Optional[SupervisorReplyResult]:
    """Consume a direct reply to a native gate prompt and resume that task."""
    match = _REPLY_MARKER_RE.search(reply_to_text or "")
    if match is None:
        return None
    if not answer or not answer.strip() or not reply_to_is_own_message:
        return None
    token = match.group(1)
    boards = [kb.read_board_metadata(kb.DEFAULT_BOARD)]
    try:
        boards.extend(kb.list_boards(include_archived=False))
    except Exception:
        pass
    seen_paths: set[str] = set()
    for board_meta in boards:
        board = str(board_meta.get("slug") or kb.DEFAULT_BOARD)
        db_path = str(kb.kanban_db_path(board).resolve())
        if db_path in seen_paths:
            continue
        seen_paths.add(db_path)
        conn = kb.connect(board=board)
        try:
            for sub in kb.list_notify_subs(conn):
                metadata = sub.get("delivery_metadata")
                if not isinstance(metadata, dict):
                    continue
                if metadata.get("_kanban_supervisor_gate_token") != token:
                    continue
                if metadata.get("_kanban_supervisor_mode") != "protected_gate":
                    continue
                delivered_message_id = str(
                    metadata.get("_kanban_supervisor_message_id") or ""
                )
                if not delivered_message_id or delivered_message_id != str(
                    reply_to_message_id or ""
                ):
                    continue
                if str(sub.get("platform") or "").lower() != str(platform).lower():
                    continue
                if str(sub.get("chat_id") or "") != str(chat_id):
                    continue
                if str(sub.get("thread_id") or "") != str(thread_id or ""):
                    continue
                owner = str(sub.get("notifier_profile") or "")
                if notifier_profile and owner and owner != notifier_profile:
                    continue
                task_id = str(metadata.get("_kanban_supervisor_task") or "")
                try:
                    raw_event_id = metadata.get("_kanban_supervisor_event_id")
                    event_id = int(str(raw_event_id))
                except (TypeError, ValueError):
                    return None
                resumed, status = kb.resume_supervisor_gate(
                    conn,
                    task_id,
                    expected_event_id=event_id,
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
    return None
