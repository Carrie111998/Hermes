"""Gateway worker-bridge alerts, queued-task recovery, and idle nudges.

This module reconstructs the source surface preserved in the orphaned
``worker_bridge_watchers.cpython-311.pyc``.  It is the sole dispatcher of both
``created`` and ``queued`` tasks: ``worker_task_dispatcher`` briefly owned
``created`` after the rebuild but enforced none of the guards here, so
ownership was handed back and that loop now stands down.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import replace
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from gateway.failure_successors import (
    create_failure_successors as create_failure_successor_tasks,
    resolve_failure_successor_settings,
)
from gateway.review_continuation import (
    create_review_continuations as create_review_continuation_tasks,
    resolve_review_continuation_settings,
)
from gateway.stage_successors import (
    create_stage_successors as create_stage_successor_tasks,
    resolve_stage_successor_settings,
)
from gateway.worker_bridge_ultra import resolve_ultra_settings

logger = logging.getLogger("gateway.run")

DEFAULT_STATUSES = ("succeeded", "failed", "timed_out")
ACTIVE_STATUSES = ("running", "verifying")
DEFAULT_INTERVAL_SECONDS = 15.0
DEFAULT_NUDGE_INTERVAL_SECONDS = 900.0
DEFAULT_AUTO_DISPATCH_ENABLED = True
DEFAULT_MAX_TASKS_PER_CYCLE = 2
MIN_INTERVAL_SECONDS = 5.0
MIN_MAX_TASKS_PER_CYCLE = 1
MAX_MAX_TASKS_PER_CYCLE = 50
CURSOR_FILENAME = "gateway_alerts_cursor.json"
MAX_CURSOR_BACKLOG = 1000

SKIP_LIVE_PID = "live_pid"
SKIP_PENDING_APPROVAL = "pending_approval"
SKIP_PENDING_INPUT = "pending_input"
SKIP_MANUAL_HOLD = "manual_hold"
SKIP_NO_CAPACITY = "no_capacity"
SKIP_SPAWN_ERROR = "spawn_error"
SKIP_CLAIM_LOST = "claim_lost"
SKIP_PAUSED = "paused"
SKIP_WAITING_INPUT = "waiting_input"
SKIP_DEP_INCOMPLETE = "dependency_incomplete"
SKIP_RETRIES_EXHAUSTED = "retries_exhausted"

# Statuses this watcher may dispatch. It owns BOTH, and is the only loop that
# dispatches either: GatewayWorkerTaskDispatcherMixin used to own ``created``
# and now stands down. That split existed for recovery reasons, not by design,
# and it meant every guard below — manual hold, dependencies, pending
# permission, pending input, retry budget — applied only to orphaned ``queued``
# tasks while ``created`` tasks went straight out through the thin dispatcher,
# which enforces none of them.
AUTO_DISPATCH_STATUSES = ("created", "queued")
# Additional statuses surfaced as *skipped* so the alert text can explain why
# work is sitting idle. Resuming these is a human/agent decision.
PENDING_VISIBLE_STATUSES = ("paused", "waiting_input")
_STATUS_SKIP_REASONS = {
    "paused": SKIP_PAUSED,
    "waiting_input": SKIP_WAITING_INPUT,
}
# A dependency counts as satisfied only in these terminal-success states.
_DEP_SATISFIED_STATUSES = ("succeeded", "accepted")

_DISPATCH_CLAIM_TTL_SECONDS = 120.0
# Runtime key marking a task claimed by one gateway process for dispatch.
_DISPATCH_CLAIM_KEY = "dispatch_claim"
# Pre-rename spelling, still honoured on read so an upgrade cannot double
# dispatch a task claimed moments before it. Never written.
_LEGACY_DISPATCH_CLAIM_KEY = "gateway_dispatch_claim"
def resolve_bridge_db_path() -> Path:
    """Locate the worker bridge SQLite store."""
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "workers" / "bridge.db"


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _connect_readwrite(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _read_cursor_payload(state_file: Path) -> dict:
    try:
        payload = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_cursor_payload(state_file: Path, payload: dict) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    temp = state_file.with_suffix(state_file.suffix + ".tmp")
    temp.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temp.replace(state_file)


def load_cursor(state_file: Path) -> Optional[int]:
    value = _read_cursor_payload(state_file).get("last_event_id")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def save_cursor(state_file: Path, last_event_id: int) -> None:
    payload = _read_cursor_payload(state_file)
    payload.update({"last_event_id": int(last_event_id), "updated_at": time.time()})
    _write_cursor_payload(state_file, payload)


# Maximum characters of alert text kept in the durable hand-off record. The
# record only has to be good enough to re-deliver a notification, not to
# reproduce an unbounded transcript in the cursor file.
PENDING_TEXT_MAX = 8000


def save_pending_injection(state_file: Path, head: int, text: str) -> None:
    """Durably record an alert *before* the cursor advances past it.

    ``BaseAdapter.handle_message`` documents that it "returns quickly by
    spawning background tasks" — returning means the event was accepted into
    an in-memory queue, NOT that it was delivered. The cursor was advanced
    immediately afterwards, so a crash in that window moved the cursor past a
    transition whose alert never reached anyone, and ``collect_new_transitions``
    only ever reads events *newer* than the cursor. The alert was gone.

    Writing the intent first turns that into at-least-once: whatever is in this
    record has not been confirmed delivered, so startup replays it.
    """
    payload = _read_cursor_payload(state_file)
    payload["pending_injection"] = {
        "head": int(head),
        "text": (text or "")[:PENDING_TEXT_MAX],
        "at": time.time(),
    }
    _write_cursor_payload(state_file, payload)


def load_pending_injection(state_file: Path) -> Optional[dict]:
    """Return an unconfirmed hand-off record, or None."""
    raw = _read_cursor_payload(state_file).get("pending_injection")
    if not isinstance(raw, dict):
        return None
    text = raw.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    return raw


def clear_pending_injection(state_file: Path) -> None:
    """Drop the hand-off record. Only called once a replay has been issued."""
    payload = _read_cursor_payload(state_file)
    if payload.pop("pending_injection", None) is not None:
        _write_cursor_payload(state_file, payload)


def load_last_nudge(state_file: Path) -> float:
    try:
        return float(_read_cursor_payload(state_file).get("last_nudge", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def save_last_nudge(state_file: Path, when: Optional[float] = None) -> None:
    payload = _read_cursor_payload(state_file)
    payload["last_nudge"] = time.time() if when is None else float(when)
    _write_cursor_payload(state_file, payload)


def load_last_auto_dispatch(state_file: Path) -> float:
    try:
        return float(
            _read_cursor_payload(state_file).get("last_auto_dispatch", 0) or 0
        )
    except (TypeError, ValueError):
        return 0.0


def save_last_auto_dispatch(
    state_file: Path, when: Optional[float] = None
) -> None:
    payload = _read_cursor_payload(state_file)
    payload["last_auto_dispatch"] = time.time() if when is None else float(when)
    _write_cursor_payload(state_file, payload)


def _max_event_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(event_id), 0) AS head FROM events").fetchone()
    return int(row["head"] if row else 0)


def baseline_cursor(db_path: Path, state_file: Path) -> int:
    """Re-baseline a missing or substantially stale alert cursor.

    A cursor more than ``MAX_CURSOR_BACKLOG`` events behind the store head is
    reset to the head so restoring the watcher cannot replay an unbounded
    historical backlog.  A recent cursor is preserved so events committed
    during an ordinary short gateway restart are still delivered.
    """
    if not db_path.exists():
        return load_cursor(state_file) or 0
    conn = _connect_readonly(db_path)
    try:
        head = _max_event_id(conn)
    finally:
        conn.close()
    cursor = load_cursor(state_file)
    if cursor is None or head - cursor > MAX_CURSOR_BACKLOG:
        save_cursor(state_file, head)
        return head
    return cursor


def _decode_json(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    try:
        payload = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _task_details(conn: sqlite3.Connection, task_id: str) -> dict:
    row = conn.execute(
        "SELECT worker, status, spec, runtime, result FROM tasks WHERE task_id=?",
        (task_id,),
    ).fetchone()
    if not row:
        return {}
    spec = _decode_json(row["spec"])
    result = _decode_json(row["result"])
    return {
        "worker": row["worker"] or "?",
        "task_status": row["status"],
        "objective": str(spec.get("objective") or ""),
        "error": str(result.get("error") or result.get("summary") or ""),
    }


def _pid_alive(pid: Any) -> bool:
    try:
        value = int(pid)
        if value <= 0:
            return False
        if os.name == "nt":
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, value)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        os.kill(value, 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def collect_new_transitions(
    db_path: Path, state_file: Path, statuses: frozenset[str]
) -> "tuple[list[dict], Optional[int]]":
    """Read terminal ``task.status`` events newer than the persisted cursor."""
    if not db_path.exists():
        return [], None
    conn = _connect_readonly(db_path)
    try:
        cursor = load_cursor(state_file)
        head = _max_event_id(conn)
        if cursor is None:
            save_cursor(state_file, head)
            return [], None
        if head <= cursor:
            return [], None
        rows = conn.execute(
            "SELECT event_id, task_id, payload, created_at FROM events "
            "WHERE kind='task.status' AND event_id > ? ORDER BY event_id",
            (cursor,),
        ).fetchall()
        transitions: list[dict] = []
        for row in rows:
            payload = _decode_json(row["payload"])
            status = str(payload.get("status") or "")
            if status not in statuses:
                continue
            transitions.append(
                {
                    "event_id": int(row["event_id"]),
                    "task_id": row["task_id"],
                    "status": status,
                    "at": row["created_at"],
                    **_task_details(conn, row["task_id"]),
                }
            )
        return transitions, head
    finally:
        conn.close()


def collect_pending_work(db_path: Path, limit: int = 10) -> list[dict]:
    """Return waiting work for alert text, excluding live task owners."""
    if not db_path.exists():
        return []
    conn = _connect_readonly(db_path)
    try:
        rows = conn.execute(
            "SELECT task_id, worker, status, spec, runtime FROM tasks "
            "WHERE status IN ('created','queued','paused','waiting_input') "
            "ORDER BY priority DESC, created_at ASC LIMIT ?",
            (max(0, int(limit)),),
        ).fetchall()
        pending = []
        for row in rows:
            runtime = _decode_json(row["runtime"])
            if runtime.get("pid") and _pid_alive(runtime["pid"]):
                continue
            spec = _decode_json(row["spec"])
            pending.append(
                {
                    "task_id": row["task_id"],
                    "worker": row["worker"] or "?",
                    "status": row["status"],
                    "objective": str(spec.get("objective") or "")[:100],
                }
            )
        return pending
    finally:
        conn.close()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    )


def select_dispatchable_tasks(
    db_path: Path, limit: int
) -> tuple[list[dict], list[dict]]:
    """Return dispatchable tasks and pending tasks excluded by a guard.

    Covers ``created`` and ``queued``. This watcher is the sole dispatcher of
    both; ``claim_task_for_dispatch`` reserves a ``created`` row into ``queued``
    inside the claim transaction, so the reservation and the guard checks cannot
    be interleaved by another process.

    ``paused`` and ``waiting_input`` are never dispatchable — resuming them is a
    human/agent decision — but they are returned in ``skipped`` so the alert
    text can say why work is sitting idle.
    """
    if not db_path.exists():
        return [], []
    conn = _connect_readonly(db_path)
    try:
        visible = (*AUTO_DISPATCH_STATUSES, *PENDING_VISIBLE_STATUSES)
        placeholders = ",".join("?" for _ in visible)
        rows = conn.execute(
            "SELECT task_id, worker, status, priority, spec, runtime, created_at "
            f"FROM tasks WHERE status IN ({placeholders}) "
            "ORDER BY priority DESC, created_at ASC, task_id ASC",
            visible,
        ).fetchall()
        has_permissions = _table_exists(conn, "permission_requests")
        has_inputs = _table_exists(conn, "input_requests")
        eligible: list[dict] = []
        skipped: list[dict] = []
        for row in rows:
            spec = _decode_json(row["spec"])
            runtime = _decode_json(row["runtime"])
            task = {
                "task_id": row["task_id"],
                "worker": row["worker"] or "?",
                "status": row["status"],
                "priority": row["priority"],
                "created_at": row["created_at"],
                "objective": str(spec.get("objective") or "")[:100],
            }
            reason: Optional[str] = _STATUS_SKIP_REASONS.get(row["status"])
            if reason:
                pass  # paused / waiting_input: surfaced, never dispatched
            elif runtime.get("pid") and _pid_alive(runtime["pid"]):
                reason = SKIP_LIVE_PID
            elif _dispatch_claim_is_live(runtime):
                reason = SKIP_LIVE_PID
            elif has_permissions and conn.execute(
                "SELECT 1 FROM permission_requests "
                "WHERE task_id=? AND status='pending' LIMIT 1",
                (row["task_id"],),
            ).fetchone():
                reason = SKIP_PENDING_APPROVAL
            elif has_inputs and conn.execute(
                "SELECT 1 FROM input_requests "
                "WHERE task_id=? AND status='pending' LIMIT 1",
                (row["task_id"],),
            ).fetchone():
                reason = SKIP_PENDING_INPUT
            metadata = spec.get("metadata") or {}
            if not reason and isinstance(metadata, dict) and (
                metadata.get("manual_dispatch") or metadata.get("hold")
            ):
                reason = SKIP_MANUAL_HOLD
            if not reason and _retries_exhausted(spec, runtime):
                reason = SKIP_RETRIES_EXHAUSTED
            if not reason and not _dependencies_satisfied(conn, spec):
                reason = SKIP_DEP_INCOMPLETE
            if reason:
                skipped.append({**task, "skip_reason": reason})
            elif len(eligible) < max(0, int(limit)):
                eligible.append(task)
        return eligible, skipped
    finally:
        conn.close()


def _retries_exhausted(spec: dict, runtime: dict) -> bool:
    """True when the task has already used its whole retry budget.

    A re-queued task carries ``runtime.retries``; ``queue_retry()`` already
    enforces the ceiling, so this is belt-and-braces against a task that was
    re-queued by hand.  A missing or unparseable limit means "no limit".
    """
    limits = spec.get("limits")
    if not isinstance(limits, dict):
        return False
    try:
        maximum = int(limits.get("maximum_retries"))
    except (TypeError, ValueError):
        return False
    try:
        used = int(runtime.get("retries", 0) or 0)
    except (TypeError, ValueError):
        used = 0
    return maximum >= 0 and used >= maximum


def _dependencies_satisfied(conn: Any, spec: dict) -> bool:
    """True when every task this one depends on reached terminal success.

    Dependencies come from ``spec.parent_task_id`` and
    ``spec.metadata.depends_on``.  Fails closed: a dependency that is not in
    the store at all counts as unsatisfied, so a task whose parent was pruned
    is held rather than dispatched into a broken precondition.

    Exception — failure successors.  ``gateway.failure_successors`` creates
    those only for parents that reached ``failed``/``timed_out`` and stamps the
    failed parent as ``parent_task_id``.  There the field records *which task
    failed* (provenance), not a precondition: the successor exists precisely
    because the parent will never succeed.  Gating it on parent success would
    hold every one of them forever.  Their ``depends_on`` entries, if any, are
    still real preconditions and are still enforced.
    """
    metadata = spec.get("metadata") or {}
    depends_on = metadata.get("depends_on") if isinstance(metadata, dict) else None
    if isinstance(depends_on, str):
        depends_on = [depends_on]
    parent = spec.get("parent_task_id")
    if isinstance(metadata, dict) and metadata.get("failure_successor") is True:
        parent = None
    deps = [
        str(dep)
        for dep in [parent, *(depends_on or [])]
        if dep
    ]
    if not deps:
        return True
    placeholders = ",".join("?" for _ in deps)
    rows = conn.execute(
        f"SELECT task_id, status FROM tasks WHERE task_id IN ({placeholders})",
        deps,
    ).fetchall()
    done = {
        row["task_id"]
        for row in rows
        if row["status"] in _DEP_SATISFIED_STATUSES
    }
    return not (set(deps) - done)


def _dispatch_claim_is_live(runtime: dict) -> bool:
    claim = runtime.get(_DISPATCH_CLAIM_KEY)
    if not isinstance(claim, dict):
        # Rows claimed by a pre-rename gateway. Read-only compatibility: without
        # it, an upgrade lands mid-claim and the next tick sees an unclaimed
        # task and dispatches a second runner. Only ever read, never written, so
        # it ages out with the 120 s TTL.
        claim = runtime.get(_LEGACY_DISPATCH_CLAIM_KEY)
    if not isinstance(claim, dict):
        return False
    try:
        return time.time() - float(claim.get("at", 0)) < _DISPATCH_CLAIM_TTL_SECONDS
    except (TypeError, ValueError):
        return False


def claim_task_for_dispatch(db_path: Path, task_id: str) -> Optional[dict]:
    """Atomically claim a ``created`` or orphaned ``queued`` task.

    A ``created`` row is reserved into ``queued`` in the same transaction that
    stamps the claim — the same created->queued reservation the thin dispatcher
    used to perform, moved here now that this watcher owns both statuses. Doing
    it in one BEGIN IMMEDIATE is what makes the reservation safe: a second
    process either sees the row already ``queued`` and claimed, or does not see
    it at all.

    The status the row had is returned in the snapshot so release can put it
    back — a released ``created`` task must not be left sitting in ``queued``.
    """
    conn = _connect_readwrite(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status, runtime FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if not row or row["status"] not in AUTO_DISPATCH_STATUSES:
            conn.rollback()
            return None
        original = _decode_json(row["runtime"])
        if (
            original.get("pid") and _pid_alive(original["pid"])
        ) or _dispatch_claim_is_live(original):
            conn.rollback()
            return None
        claimed = dict(original)
        claimed[_DISPATCH_CLAIM_KEY] = {"pid": os.getpid(), "at": time.time()}
        conn.execute(
            "UPDATE tasks SET status='queued', runtime=?, updated_at=? "
            "WHERE task_id=? AND status=?",
            (
                json.dumps(claimed, sort_keys=True),
                time.time(),
                task_id,
                row["status"],
            ),
        )
        conn.commit()
        # Snapshot carries the status too: release has to restore the row as it
        # was, and runtime alone cannot say what status to put back.
        return {"status": row["status"], "runtime": original}
    finally:
        conn.close()


def release_dispatch_claim(
    db_path: Path, task_id: str, original: Optional[dict]
) -> None:
    """Undo a claim, unless a real runner has since taken the task.

    Called when the spawn failed, so the claim must not be left behind. But
    "spawn raised" does not mean "nothing started" — a runner can stamp its pid
    and then have the call fail. Blindly restoring the snapshot in that window
    erased a live runner's pid and left the task looking unclaimed to the next
    tick, which is how one task gets two runners.
    """
    if not original or not db_path.exists():
        return
    snapshot_runtime = original.get("runtime")
    if not isinstance(snapshot_runtime, dict):
        return
    conn = _connect_readwrite(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status, runtime FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None:
            conn.rollback()
            return
        current = _decode_json(row["runtime"])
        if current.get("pid") and _pid_alive(current["pid"]):
            conn.rollback()
            return
        conn.execute(
            "UPDATE tasks SET status=?, runtime=?, updated_at=? WHERE task_id=?",
            (
                original.get("status", row["status"]),
                json.dumps(snapshot_runtime, sort_keys=True),
                time.time(),
                task_id,
            ),
        )
        conn.commit()
    except sqlite3.Error:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        logger.warning("worker dispatch claim release failed", exc_info=True)
    finally:
        conn.close()


def _live_global_lease_count(conn: sqlite3.Connection) -> Optional[int]:
    """Global capacity slots held by a process that is still alive.

    ``leases`` is the orchestrator's own capacity ledger: start_task acquires
    ("global", maximum_concurrency) before running anything, and reclaims slots
    whose pid is dead. Returns None on a bridge.db predating the table, so
    callers can fall back rather than silently reporting unlimited capacity.
    """
    try:
        rows = conn.execute(
            "SELECT pid FROM leases WHERE scope = 'global'"
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    return sum(1 for row in rows if _pid_alive(row["pid"]))


def count_free_global_slots(db_path: Path, maximum_concurrency: int) -> int:
    if not db_path.exists():
        return 0
    conn = _connect_readonly(db_path)
    try:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        row = conn.execute(
            f"SELECT COUNT(*) AS count FROM tasks WHERE status IN ({placeholders})",
            ACTIVE_STATUSES,
        ).fetchone()
        consumed = int(row["count"] if row else 0)

        # Two independent views of the same capacity, and each catches what the
        # other misses. A task row stuck in `running` because its runner was
        # killed consumes capacity forever — the lease self-heals via pid
        # liveness. A live lease whose task row has already moved on is real
        # capacity the status count no longer sees. Take whichever says MORE is
        # consumed: this gate fails closed, and over-reporting free slots is the
        # failure that overruns concurrency.
        leased = _live_global_lease_count(conn)
        if leased is not None:
            consumed = max(consumed, leased)
        return max(0, int(maximum_concurrency) - consumed)
    finally:
        conn.close()


def has_active_worker_tasks(db_path: Path) -> bool:
    """Is any worker task running? Gates the idle nudge, NOT dispatch capacity.

    Deliberately standalone rather than ``count_free_global_slots(db_path, 1)``.
    Sharing that helper coupled two unrelated policies: making capacity
    leases-backed silently moved this threshold too, so a live global lease with
    no running task row began suppressing the nudge. A capacity gate wants to
    fail closed; a nudge wants to fire when nothing is actually running. They
    disagree at the edges, so they get their own definitions.
    """
    if not db_path.exists():
        return False
    conn = _connect_readonly(db_path)
    try:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        row = conn.execute(
            f"SELECT COUNT(*) AS count FROM tasks WHERE status IN ({placeholders})",
            ACTIVE_STATUSES,
        ).fetchone()
        return int(row["count"] if row else 0) > 0
    finally:
        conn.close()


def _truncate(text: str, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: max(0, limit - 1)] + "…"


def _format_pending_section(
    pending: list[dict], *, with_transitions: bool
) -> list[str]:
    if not pending:
        return []
    # Auto-dispatch changes what this section means. Under manual dispatch it
    # was a to-do list for a human. With the dispatcher live, pending work means
    # the gateway HAS capacity and is choosing not to use it, so the count and
    # the reason each task is held are the actionable facts.
    if with_transitions:
        lines = [
            "",
            f"Free worker capacity: {len(pending)} task(s) are pending dispatch.",
        ]
    else:
        lines = [
            "",
            "Idle: no worker tasks are currently running; "
            f"{len(pending)} task(s) are pending dispatch.",
        ]
    for task in pending[:10]:
        reason = task.get("skip_reason")
        suffix = f" (blocked: {reason})" if reason else ""
        lines.append(
            f"- {task['task_id']} [{task.get('status', '?')}] "
            f"{_truncate(task.get('objective', ''), 120)}{suffix}"
        )
    if with_transitions:
        lines.append(
            "Triage failures before starting unrelated pending work — ack any "
            "failures first, then run `hermes worker tasks start <task_id>`."
        )
    else:
        lines.append(
            "Triage/ack any open failures first, then run "
            "`hermes worker tasks start <task_id>`."
        )
    return lines


def format_alert_text(
    transitions: list[dict], pending: Optional[list[dict]] = None
) -> str:
    lines = [
        "[Worker Bridge Alert — automated gateway notification, not a user message]",
        "",
    ]
    # Failures first. A batch alert that opens with three green successes buries
    # the one task that needs a human, and this text is read in a chat client
    # where only the first lines are visible without expanding.
    ordered = sorted(
        transitions,
        key=lambda item: item.get("status") in _DEP_SATISFIED_STATUSES,
    )
    for item in ordered:
        icon = "✅" if item.get("status") in _DEP_SATISFIED_STATUSES else "❌"
        lines.append(
            f"{icon} {item.get('task_id')} ({item.get('worker', '?')}) "
            f"→ {item.get('status')}"
        )
        if item.get("objective"):
            lines.append(f"   objective: {_truncate(item['objective'], 180)}")
        if item.get("error"):
            lines.append(f"   error: {_truncate(item['error'], 240)}")
    if transitions:
        lines.append(
            "Act on each task now: run `hermes worker tasks show <task_id>` for "
            "detail, then ack or retry."
        )
    lines.extend(
        _format_pending_section(
            list(pending or []), with_transitions=bool(transitions)
        )
    )
    return "\n".join(lines)


def _resolve_alert_settings(cfg: Any) -> "Optional[dict]":
    """Extract and normalize ``worker_bridge.gateway_alerts`` settings."""
    wb_cfg = cfg.get("worker_bridge", {}) if isinstance(cfg, dict) else {}
    raw = wb_cfg.get("gateway_alerts", {}) if isinstance(wb_cfg, dict) else {}
    if not isinstance(raw, dict):
        raw = {}
    enabled = raw.get("enabled", False) is True
    if not enabled:
        return None
    configured_statuses = raw.get("statuses") or list(DEFAULT_STATUSES)
    if isinstance(configured_statuses, str):
        configured_statuses = [
            part.strip() for part in configured_statuses.split(",") if part.strip()
        ]
    statuses = frozenset(
        str(status)
        for status in configured_statuses
        if str(status) in DEFAULT_STATUSES
    )
    try:
        interval = max(
            MIN_INTERVAL_SECONDS,
            float(raw.get("interval_seconds", DEFAULT_INTERVAL_SECONDS)),
        )
    except (TypeError, ValueError):
        interval = DEFAULT_INTERVAL_SECONDS
    try:
        nudge = float(
            raw.get("nudge_interval_seconds", DEFAULT_NUDGE_INTERVAL_SECONDS)
        )
    except (TypeError, ValueError):
        nudge = DEFAULT_NUDGE_INTERVAL_SECONDS
    auto_raw = raw.get("auto_dispatch", {})
    if isinstance(auto_raw, bool):
        auto_raw = {"enabled": auto_raw}
    if not isinstance(auto_raw, dict):
        auto_raw = {}
    auto_enabled = auto_raw.get("enabled", DEFAULT_AUTO_DISPATCH_ENABLED) is not False
    # Env kill switch: lets an operator stop the watcher from starting new
    # worker tasks without editing config or restarting into a new profile.
    # Auto-dispatch spawns workers that write into their target repository, so
    # this is the fastest way to quiet it.
    if os.environ.get("HERMES_WORKER_BRIDGE_AUTODISPATCH", "").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        auto_enabled = False
    try:
        auto_interval = max(
            MIN_INTERVAL_SECONDS,
            float(auto_raw.get("interval_seconds", interval)),
        )
    except (TypeError, ValueError):
        auto_interval = interval
    try:
        max_per_cycle = max(
            MIN_MAX_TASKS_PER_CYCLE,
            min(
                MAX_MAX_TASKS_PER_CYCLE,
                int(
                    auto_raw.get(
                        "max_tasks_per_cycle", DEFAULT_MAX_TASKS_PER_CYCLE
                    )
                ),
            ),
        )
    except (TypeError, ValueError):
        max_per_cycle = DEFAULT_MAX_TASKS_PER_CYCLE
    return {
        "interval": interval,
        "nudge_interval": max(0.0, nudge),
        "statuses": statuses,
        "platform": str(raw.get("platform") or "").strip().lower() or None,
        "chat_id": str(raw.get("chat_id") or "").strip() or None,
        "thread_id": str(raw.get("thread_id") or "").strip() or None,
        "chat_type": str(raw.get("chat_type") or "").strip() or "group",
        "auto_dispatch": {
            "enabled": auto_enabled,
            "interval": auto_interval,
            "max_tasks_per_cycle": max_per_cycle,
        },
        "auto_ultra": resolve_ultra_settings(wb_cfg),
    }


class GatewayWorkerBridgeWatchersMixin:
    """Worker-bridge notifier, orphan recovery, and idle-nudge mixin."""

    def _get_worker_bridge(self, db_path: Optional[Path] = None):
        try:
            from hermes_worker_bridge.dispatch import spawn
            from hermes_worker_bridge.runner import _bridge_for

            return _bridge_for(str(db_path or resolve_bridge_db_path())), spawn
        except Exception as exc:
            logger.warning("worker-bridge gateway integration unavailable: %s", exc)
            return None

    def _audit_dispatch(
        self,
        bridge: Any,
        task: dict,
        *,
        action: str,
        reason: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        task_id = task["task_id"]

        # A held task is re-evaluated every poll and holds for the same reason
        # every time, so auditing each pass buries the events that matter under
        # thousands of identical rows. Audit the first occurrence and each time
        # the reason CHANGES; forget the task once it stops being skipped, so a
        # later hold for the same reason is recorded again.
        memo = getattr(self, "_autodispatch_skip_memo", None)
        if memo is None:
            memo = self._autodispatch_skip_memo = {}
        if action == "skipped":
            if memo.get(task_id) == reason:
                return
            memo[task_id] = reason
        else:
            memo.pop(task_id, None)

        payload = {
            "action": action,
            "reason": reason,
            "error": error,
            "status": task.get("status"),
            "status_before": task.get("status"),
            "by": "gateway-watcher",
            "pid": os.getpid(),
        }
        try:
            try:
                bridge.store.append_event(
                    kind="task.autodispatch", payload=payload, task_id=task_id
                )
            except TypeError:
                bridge.store.append_event(
                    "task.autodispatch", payload, task_id=task_id
                )
        except Exception:
            # Warning, not debug. This audit trail IS the record of what the
            # gateway dispatched on its own; a silent debug-level swallow is
            # why its absence went unnoticed.
            logger.warning("worker dispatch audit failed", exc_info=True)

    async def _worker_bridge_auto_dispatch(
        self, settings: dict, *, db_path: Path, state_file: Path
    ) -> int:
        ad = settings.get("auto_dispatch") or {}
        if not ad.get("enabled"):
            self._last_skipped_pending = []
            return 0
        now = time.time()
        last = await asyncio.to_thread(load_last_auto_dispatch, state_file)
        # `or` would turn an explicit interval of 0 into the 15 s default, so a
        # deployment asking for "check every pass" silently got one pass per 15 s.
        # Config already floors this at MIN_INTERVAL_SECONDS where it is resolved.
        raw_interval = (
            ad["interval"] if "interval" in ad else DEFAULT_INTERVAL_SECONDS
        )
        try:
            dispatch_interval = float(raw_interval)
        except (TypeError, ValueError):
            dispatch_interval = DEFAULT_INTERVAL_SECONDS
        if now - last < dispatch_interval:
            return 0
        await asyncio.to_thread(save_last_auto_dispatch, state_file, now)
        loaded = self._get_worker_bridge(db_path)
        if loaded is None:
            _, skipped = await asyncio.to_thread(select_dispatchable_tasks, db_path, 0)
            self._last_skipped_pending = skipped
            return 0
        bridge, spawn = loaded
        free = await asyncio.to_thread(
            count_free_global_slots,
            db_path,
            getattr(bridge, "_maximum_concurrency", 1),
        )
        budget = min(free, int(ad.get("max_tasks_per_cycle") or 1))
        dispatchable, skipped = await asyncio.to_thread(
            select_dispatchable_tasks, db_path, budget
        )
        if free <= 0:
            pending = await asyncio.to_thread(collect_pending_work, db_path)
            skipped.extend({**item, "skip_reason": SKIP_NO_CAPACITY} for item in pending)
        started = 0
        for task in dispatchable:
            original = await asyncio.to_thread(
                claim_task_for_dispatch, db_path, task["task_id"]
            )
            if original is None:
                skipped.append({**task, "skip_reason": SKIP_CLAIM_LOST})
                continue
            try:
                await asyncio.to_thread(spawn, bridge, task["task_id"])
                self._audit_dispatch(bridge, task, action="dispatched")
                started += 1
            except Exception as exc:
                await asyncio.to_thread(
                    release_dispatch_claim, db_path, task["task_id"], original
                )
                logger.warning(
                    "worker-bridge auto-dispatch spawn failed for %s: %s",
                    task["task_id"],
                    exc,
                )
                skipped.append({**task, "skip_reason": SKIP_SPAWN_ERROR})
                self._audit_dispatch(
                    bridge,
                    task,
                    action="skipped",
                    reason=SKIP_SPAWN_ERROR,
                    error=str(exc),
                )
        # Audit why each eligible-looking task was held back. Without this the
        # audit trail records only what WAS dispatched, and "nothing dispatched
        # and nothing logged" is indistinguishable from "the watcher is dead".
        for task in skipped:
            reason = task.get("skip_reason")
            if reason:
                self._audit_dispatch(
                    bridge, task, action="skipped", reason=reason
                )
        self._last_skipped_pending = skipped
        return started

    def _most_recent_session_source(self, settings: dict):
        store = getattr(self, "session_store", None)
        if store is None or not hasattr(store, "list_sessions"):
            return None
        try:
            entries = store.list_sessions(active_minutes=240)
        except Exception:
            return None
        adapters = getattr(self, "adapters", {}) or {}
        platform_filter = settings.get("platform")
        candidates = []
        for entry in entries:
            origin = getattr(entry, "origin", None)
            if origin is None or str(getattr(origin, "user_id", "") or "").startswith(
                "system:"
            ):
                continue
            platform = getattr(entry, "platform", None) or origin.platform
            if platform not in adapters:
                continue
            value = getattr(platform, "value", str(platform)).lower()
            if platform_filter and value != platform_filter:
                continue
            candidates.append(entry)
        if not candidates:
            return None
        best = max(candidates, key=lambda entry: entry.updated_at or 0)
        platform = getattr(best, "platform", None) or best.origin.platform

        # Re-stamp the borrowed session origin with the bridge's own system
        # identity. The candidate loop above skips origins whose user_id starts
        # with "system:", so injecting under the human's identity would make
        # this alert's own turn the freshest session and the watcher would keep
        # re-selecting the channel it just wrote to.
        origin = best.origin
        try:
            source = replace(
                origin,
                user_id="system:worker-bridge",
                user_name="Worker Bridge",
            )
        except TypeError:
            from gateway.session import SessionSource

            source = SessionSource(
                platform=origin.platform,
                chat_id=origin.chat_id,
                chat_name=getattr(origin, "chat_name", None),
                chat_type=getattr(origin, "chat_type", None) or "group",
                user_id="system:worker-bridge",
                user_name="Worker Bridge",
                thread_id=getattr(origin, "thread_id", None),
            )
        return adapters[platform], source

    def _resolve_worker_alert_target(self, settings: dict):
        if not settings.get("chat_id"):
            recent = self._most_recent_session_source(settings)
            if recent:
                return recent
        from gateway.session import SessionSource

        for platform, adapter in (getattr(self, "adapters", {}) or {}).items():
            value = getattr(platform, "value", str(platform)).lower()
            if settings.get("platform") and value != settings["platform"]:
                continue
            try:
                home = self.config.get_home_channel(platform)
            except Exception:
                home = None
            chat_id = settings.get("chat_id") or (
                str(home.chat_id) if home and home.chat_id else None
            )
            if not chat_id:
                continue
            source = SessionSource(
                platform=platform,
                chat_id=str(chat_id),
                chat_name=home.name if home else None,
                chat_type=settings.get("chat_type") or "group",
                user_id="system:worker-bridge",
                user_name="Worker Bridge",
                thread_id=settings.get("thread_id")
                or (str(home.thread_id) if home and home.thread_id else None),
            )
            return adapter, source
        return None

    async def _worker_bridge_tick(
        self, settings: dict, *, db_path: Path, state_file: Path
    ) -> bool:
        from gateway.platforms.base import MessageEvent, MessageType

        transitions, head = await asyncio.to_thread(
            collect_new_transitions, db_path, state_file, settings["statuses"]
        )
        if head is None:
            return False
        if not transitions:
            await asyncio.to_thread(save_cursor, state_file, head)
            return False
        target = self._resolve_worker_alert_target(settings)
        if not target:
            logger.debug(
                "worker-bridge alerts: %d transition(s) pending but no "
                "adapter/home-channel target; retrying next tick",
                len(transitions),
            )
            return False
        ready, _deferred = self._ultra_route_transitions(transitions, settings)
        self._ultra_pump(settings)
        if not ready:
            await asyncio.to_thread(save_cursor, state_file, head)
            return False
        if (settings.get("auto_dispatch") or {}).get("enabled"):
            pending = list(getattr(self, "_last_skipped_pending", []) or [])
            if not pending:
                _, pending = await asyncio.to_thread(
                    select_dispatchable_tasks, db_path, 0
                )
        else:
            pending = await asyncio.to_thread(collect_pending_work, db_path)
        adapter, source = target
        event = MessageEvent(
            text=format_alert_text(ready, pending),
            message_type=MessageType.TEXT,
            source=source,
            internal=True,
        )
        logger.info(
            "worker-bridge alerts: injecting %d transition(s) (+%d pending) "
            "into %s chat=%s thread=%s (events ≤ %d)",
            len(ready),
            len(pending),
            getattr(source.platform, "value", source.platform),
            source.chat_id,
            source.thread_id,
            head,
        )
        # Order matters and is the whole point: persist the hand-off BEFORE the
        # cursor moves. handle_message only queues the event in memory, so a
        # crash between here and delivery used to lose the alert outright —
        # collect_new_transitions never looks back past the cursor.
        await asyncio.to_thread(save_pending_injection, state_file, head, event.text)
        await adapter.handle_message(event)
        await asyncio.to_thread(save_cursor, state_file, head)
        return True

    async def _replay_pending_alert(self, settings: dict, *, state_file: Path) -> bool:
        """Re-deliver an unconfirmed alert hand-off after a restart.

        Returns True when a replay was issued. Never raises into the watcher
        loop: a replay failure must not stop the watcher from starting, and the
        record survives so the next start tries again.
        """
        from gateway.platforms.base import MessageEvent, MessageType

        try:
            pending = await asyncio.to_thread(load_pending_injection, state_file)
            if not pending:
                return False
            target = self._resolve_worker_alert_target(settings)
            if not target:
                # No adapter yet — keep the record and retry on a later start.
                logger.warning(
                    "worker-bridge alerts: unconfirmed alert for events ≤ %s held "
                    "(no delivery target yet)", pending.get("head"),
                )
                return False
            adapter, source = target
            logger.warning(
                "worker-bridge alerts: replaying unconfirmed alert for events ≤ %s "
                "(previous run did not confirm delivery)", pending.get("head"),
            )
            await adapter.handle_message(MessageEvent(
                text=pending["text"], message_type=MessageType.TEXT,
                source=source, internal=True,
            ))
            await asyncio.to_thread(clear_pending_injection, state_file)
            return True
        except Exception as exc:
            logger.error("worker-bridge alerts: pending-alert replay failed: %s", exc)
            return False

    async def _worker_bridge_idle_nudge(
        self, settings: dict, *, db_path: Path, state_file: Path
    ) -> bool:
        from gateway.platforms.base import MessageEvent, MessageType

        interval = float(settings.get("nudge_interval") or 0)
        if interval <= 0:
            return False
        last = await asyncio.to_thread(load_last_nudge, state_file)
        now = time.time()
        if now - last < interval or await asyncio.to_thread(
            has_active_worker_tasks, db_path
        ):
            return False
        # Same source as the transition alert: with auto-dispatch live, the
        # useful nudge is "these are the tasks the dispatcher declined, and
        # why", not a bare list of everything queued. Without it, the operator
        # sees pending work and free capacity and cannot tell which guard held
        # it back.
        if (settings.get("auto_dispatch") or {}).get("enabled"):
            pending = list(getattr(self, "_last_skipped_pending", []) or [])
            if not pending:
                _, pending = await asyncio.to_thread(
                    select_dispatchable_tasks, db_path, 0
                )
        else:
            pending = await asyncio.to_thread(collect_pending_work, db_path)
        if not pending:
            return False
        target = self._resolve_worker_alert_target(settings)
        if not target:
            return False
        adapter, source = target
        event = MessageEvent(
            text=format_alert_text([], pending),
            message_type=MessageType.TEXT,
            source=source,
            internal=True,
        )
        await adapter.handle_message(event)
        await asyncio.to_thread(save_last_nudge, state_file, now)
        logger.info(
            "worker-bridge alerts: idle nudge for %d pending task(s) into "
            "%s chat=%s thread=%s",
            len(pending),
            getattr(source.platform, "value", source.platform),
            source.chat_id,
            source.thread_id,
        )
        return True

    async def _worker_bridge_failure_successors(
        self, config: dict, *, db_path: Path
    ) -> int:
        """Run one failure-successor pass, ignoring recursive re-entry."""
        if getattr(self, "_failure_successor_pass_running", False):
            logger.warning(
                "worker-bridge failure successor pass already running; "
                "recursive entry ignored"
            )
            return 0
        self._failure_successor_pass_running = True
        try:
            settings = resolve_failure_successor_settings(config)
            if not settings["enabled"]:
                return 0
            loaded = self._get_worker_bridge(db_path)
            if loaded is None:
                return 0
            bridge, _spawn = loaded
            return await asyncio.to_thread(
                create_failure_successor_tasks,
                bridge,
                settings,
            )
        finally:
            self._failure_successor_pass_running = False

    async def _worker_bridge_review_continuations(
        self, config: dict, *, db_path: Path
    ) -> int:
        """Run one review-continuation pass, ignoring recursive re-entry."""
        if getattr(self, "_review_continuation_pass_running", False):
            logger.warning(
                "worker-bridge review continuation pass already running; "
                "recursive entry ignored"
            )
            return 0
        self._review_continuation_pass_running = True
        try:
            settings = resolve_review_continuation_settings(config)
            if not settings["enabled"]:
                return 0
            loaded = self._get_worker_bridge(db_path)
            if loaded is None:
                return 0
            bridge, _spawn = loaded
            return await asyncio.to_thread(
                create_review_continuation_tasks,
                bridge,
                settings,
            )
        finally:
            self._review_continuation_pass_running = False

    async def _worker_bridge_stage_successors(
        self, config: dict, *, db_path: Path
    ) -> int:
        """Run one stage-successor pass, ignoring recursive re-entry."""
        if getattr(self, "_stage_successor_pass_running", False):
            logger.warning(
                "worker-bridge stage successor pass already running; "
                "recursive entry ignored"
            )
            return 0
        self._stage_successor_pass_running = True
        try:
            settings = resolve_stage_successor_settings(config)
            if not settings["enabled"]:
                return 0
            loaded = self._get_worker_bridge(db_path)
            if loaded is None:
                return 0
            bridge, _spawn = loaded
            return await asyncio.to_thread(
                create_stage_successor_tasks,
                bridge,
                settings,
            )
        finally:
            self._stage_successor_pass_running = False

    async def _worker_bridge_notifier_watcher(
        self, interval: Optional[float] = None
    ) -> None:
        try:
            from hermes_cli.config import load_config
        except Exception as exc:
            logger.warning("worker-bridge alerts unavailable: %s", exc)
            return
        db_path = resolve_bridge_db_path()
        state_file = db_path.parent / CURSOR_FILENAME
        cursor_ready = False
        announced = False
        while self._running:
            sleep_for = interval or DEFAULT_INTERVAL_SECONDS
            try:
                config = load_config() or {}
                settings = _resolve_alert_settings(config)
                await self._worker_bridge_failure_successors(
                    config,
                    db_path=db_path,
                )
                await self._worker_bridge_review_continuations(
                    config,
                    db_path=db_path,
                )
                await self._worker_bridge_stage_successors(
                    config,
                    db_path=db_path,
                )
                if settings is None:
                    await asyncio.sleep(sleep_for)
                    continue
                if not cursor_ready:
                    cursor = await asyncio.to_thread(
                        baseline_cursor, db_path, state_file
                    )
                    logger.info(
                        "worker-bridge alerts: cursor ready at event %d; "
                        "backlogs over %d events are not replayed",
                        cursor,
                        MAX_CURSOR_BACKLOG,
                    )
                    cursor_ready = True
                    # Re-deliver an alert whose hand-off was never confirmed.
                    # The record is written before the cursor advances, so its
                    # presence at startup means the previous process died (or
                    # was killed) between queueing the event and delivering it.
                    # Replaying is at-least-once: a duplicate notification is
                    # strictly better than a worker failure nobody is told
                    # about, and the record is cleared only after the replay is
                    # issued so a crash mid-replay still leaves it pending.
                    await self._replay_pending_alert(settings, state_file=state_file)
                sleep_for = interval or settings["interval"]
                if not announced:
                    logger.info(
                        "worker-bridge alerts: watching %s every %.0fs "
                        "(statuses=%s)",
                        db_path,
                        sleep_for,
                        ",".join(sorted(settings["statuses"])),
                    )
                    announced = True
                await self._worker_bridge_auto_dispatch(
                    settings, db_path=db_path, state_file=state_file
                )
                await self._worker_bridge_tick(
                    settings, db_path=db_path, state_file=state_file
                )
                await self._worker_bridge_idle_nudge(
                    settings, db_path=db_path, state_file=state_file
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("worker-bridge alerts tick failed")
            await asyncio.sleep(sleep_for)
