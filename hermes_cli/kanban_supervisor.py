"""Durable delegation supervisor for Kanban / delegate_task / Bot Chat.

Keeps an objective ledger alive after the creating session returns. A
guard, an open PR, a completed child, or parent-loop exhaustion is never
a terminal success. See LS-2776.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

OBJECTIVE_STATUSES = frozenset({"open", "blocked_owner", "done"})
UNIT_KINDS = frozenset({"kanban", "delegate_task", "bot_chat"})
UNIT_STATUSES = frozenset(
    {
        "pending",
        "running",
        "guarded",
        "blocked",
        "awaiting_verification",
        "done",
        "failed",
    }
)
EXEMPTION_VALUES = frozenset({"update_existing_pr", "operator_requeue"})
EXEMPTION_COMMENT_NEEDLES = (
    "respawn-ok",
    "guard-exemption: update-existing-pr",
)
STARVATION_THRESHOLD = 3
STARVATION_COOLDOWN_SECONDS = 1800
REQUEUE_EVENT_KINDS = frozenset({"status", "promoted", "unblocked", "reclaimed"})

SUPERVISOR_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS kanban_objectives (
    id TEXT PRIMARY KEY,
    root_task_id TEXT NOT NULL,
    delegator_profile TEXT,
    origin_platform TEXT,
    origin_chat_id TEXT,
    origin_thread_id TEXT,
    origin_session_key TEXT,
    remoko_request_id TEXT,
    remoko_external_id TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_objectives_root ON kanban_objectives(root_task_id);
CREATE INDEX IF NOT EXISTS idx_objectives_status ON kanban_objectives(status);

CREATE TABLE IF NOT EXISTS kanban_objective_units (
    id TEXT PRIMARY KEY,
    objective_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    owner_profile TEXT,
    last_progress_at INTEGER,
    terminal_predicate TEXT,
    next_gate TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    proof TEXT,
    UNIQUE(objective_id, kind, ref)
);
CREATE INDEX IF NOT EXISTS idx_objective_units_obj ON kanban_objective_units(objective_id);
CREATE INDEX IF NOT EXISTS idx_objective_units_ref ON kanban_objective_units(kind, ref);

CREATE TABLE IF NOT EXISTS kanban_respawn_guard_state (
    task_id TEXT PRIMARY KEY,
    last_reason TEXT,
    consecutive_count INTEGER NOT NULL DEFAULT 0,
    first_guard_at INTEGER,
    last_guard_at INTEGER,
    last_starvation_at INTEGER,
    last_starvation_reason TEXT,
    exemption TEXT,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS kanban_supervisor_events (
    event_key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    task_id TEXT,
    objective_id TEXT,
    payload TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS kanban_reconcile_grants (
    id TEXT PRIMARY KEY,
    objective_id TEXT NOT NULL,
    supervisor_task_id TEXT NOT NULL,
    descendant_task_id TEXT NOT NULL,
    transition TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    consumed_at INTEGER,
    created_at INTEGER NOT NULL,
    UNIQUE(
        objective_id, supervisor_task_id, descendant_task_id,
        transition, evidence_hash
    )
);
CREATE INDEX IF NOT EXISTS idx_reconcile_grants_supervisor
    ON kanban_reconcile_grants(supervisor_task_id, consumed_at);
"""


def _new_id(prefix: str) -> str:
    return prefix + secrets.token_hex(4)


def _now() -> int:
    return int(time.time())


def _session_env(name: str, default: str = "") -> str:
    try:
        from gateway.session_context import get_session_env

        value = get_session_env(name, default)
        if value:
            return str(value)
    except Exception:
        pass
    return os.environ.get(name, default) or default


def supervisor_context_active() -> bool:
    """True when this process is inside a Kanban / objective-owned run."""
    return bool(
        os.environ.get("HERMES_KANBAN_TASK")
        or os.environ.get("HERMES_OBJECTIVE_ID")
        or os.environ.get("HERMES_KANBAN_DB")
        or os.environ.get("HERMES_KANBAN_BOARD")
    )


def ensure_supervisor_tables(conn: sqlite3.Connection) -> None:
    # Do NOT use executescript here: it implicit-COMMITs and would abort an
    # outer write_txn (dispatcher guard recording).
    if getattr(conn, "in_transaction", False):
        for stmt in SUPERVISOR_SCHEMA_SQL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(stmt)
        return
    conn.executescript(SUPERVISOR_SCHEMA_SQL)


@dataclass
class SessionOrigin:
    platform: str = ""
    chat_id: str = ""
    thread_id: str = ""
    session_key: str = ""
    profile: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.platform and (self.chat_id or self.session_key))

    def notify_chat_id(self) -> str:
        if self.platform.lower() in {"webui", "tui", "api_server"} and self.session_key:
            return self.session_key
        return self.chat_id or self.session_key


@dataclass
class SupervisorResult:
    starvation: list[dict] = field(default_factory=list)
    repaired: list[str] = field(default_factory=list)
    remoko_requests: list[str] = field(default_factory=list)
    completed_objectives: list[str] = field(default_factory=list)
    invalidated_reviews: list[str] = field(default_factory=list)
    wake_retries: list[str] = field(default_factory=list)


def capture_session_origin() -> SessionOrigin:
    platform = (
        _session_env("HERMES_SESSION_PLATFORM")
        or _session_env("HERMES_SESSION_SOURCE")
        or ""
    )
    chat_id = _session_env("HERMES_SESSION_CHAT_ID")
    thread_id = _session_env("HERMES_SESSION_THREAD_ID")
    session_key = _session_env("HERMES_SESSION_KEY") or _session_env("HERMES_SESSION_ID")
    profile = (
        _session_env("HERMES_SESSION_PROFILE")
        or os.environ.get("HERMES_PROFILE")
        or ""
    )
    if platform.lower() in {"webui", "tui", "api_server"} and session_key and not chat_id:
        chat_id = session_key
    return SessionOrigin(
        platform=str(platform or ""),
        chat_id=str(chat_id or ""),
        thread_id=str(thread_id or ""),
        session_key=str(session_key or ""),
        profile=str(profile or ""),
    )


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _parents_of(conn: sqlite3.Connection, task_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id ASC",
        (task_id,),
    ).fetchall()
    return [r["parent_id"] if isinstance(r, sqlite3.Row) else r[0] for r in rows]


def collect_graph_roots(conn: sqlite3.Connection, task_id: str) -> list[str]:
    """Deterministic graph roots reachable from ``task_id``."""
    if not task_id:
        return []
    seen: set[str] = set()
    roots: set[str] = set()
    stack = [task_id]
    while stack:
        current = stack.pop()
        if not current or current in seen:
            continue
        seen.add(current)
        parents = _parents_of(conn, current)
        if not parents:
            roots.add(current)
            continue
        stack.extend(reversed(parents))
    return sorted(roots)


def canonical_root_task_id(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Single canonical root, or ``None`` on cross-objective fan-in.

    A child with multiple parents is allowed only when every parent
    resolves to the same root. Two independent roots sharing a child
    do not get an arbitrary winner.
    """
    roots = collect_graph_roots(conn, task_id)
    if len(roots) == 1:
        return roots[0]
    if not roots:
        return task_id or None
    return None


def _root_task_id(conn: sqlite3.Connection, task_id: str) -> str:
    root = canonical_root_task_id(conn, task_id)
    if root:
        return root
    # Cross-objective fan-in: never pick unordered first-parent.
    return task_id


def origin_from_row(row: sqlite3.Row | dict) -> SessionOrigin:
    get = row.__getitem__ if not isinstance(row, dict) else row.get
    return SessionOrigin(
        platform=str(get("origin_platform") or ""),
        chat_id=str(get("origin_chat_id") or ""),
        thread_id=str(get("origin_thread_id") or ""),
        session_key=str(get("origin_session_key") or ""),
        profile=str(get("delegator_profile") or ""),
    )


def get_objective_for_root(
    conn: sqlite3.Connection, root_task_id: str
) -> Optional[dict]:
    if not _table_exists(conn, "kanban_objectives"):
        return None
    row = conn.execute(
        "SELECT * FROM kanban_objectives WHERE root_task_id = ? "
        "ORDER BY created_at ASC LIMIT 1",
        (root_task_id,),
    ).fetchone()
    return dict(row) if row else None


def get_objective(conn: sqlite3.Connection, objective_id: str) -> Optional[dict]:
    if not _table_exists(conn, "kanban_objectives"):
        return None
    row = conn.execute(
        "SELECT * FROM kanban_objectives WHERE id = ?",
        (objective_id,),
    ).fetchone()
    return dict(row) if row else None


def _objective_origin(
    conn: sqlite3.Connection, task_id: str
) -> Optional[SessionOrigin]:
    """First-class objective origin, never the live process session."""
    env_obj = os.environ.get("HERMES_OBJECTIVE_ID") or ""
    if env_obj:
        obj = get_objective(conn, env_obj)
        if obj:
            origin = origin_from_row(obj)
            if origin.usable:
                return origin
    root = _root_task_id(conn, task_id)
    obj = get_objective_for_root(conn, root)
    if obj:
        origin = origin_from_row(obj)
        if origin.usable:
            return origin
    parent_task = os.environ.get("HERMES_KANBAN_TASK") or ""
    if parent_task:
        parent_root = _root_task_id(conn, parent_task)
        obj = get_objective_for_root(conn, parent_root)
        if obj:
            origin = origin_from_row(obj)
            if origin.usable:
                return origin
    return None


def _durable_notify_origin(
    conn: sqlite3.Connection, task_id: str
) -> Optional[SessionOrigin]:
    """Origin from objective / stored notify rows only — never the live chat."""
    ensure_supervisor_tables(conn)
    origin = _objective_origin(conn, task_id)
    if origin and origin.usable:
        return origin
    parent_task = os.environ.get("HERMES_KANBAN_TASK") or ""
    if parent_task:
        origin = _origin_from_notify_subs(conn, parent_task)
        if origin and origin.usable:
            return origin
    root = _root_task_id(conn, task_id)
    for candidate in (task_id, root):
        origin = _origin_from_notify_subs(conn, candidate)
        if origin and origin.usable:
            return origin
    return None


def resolve_notify_origin(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    allow_live: bool = True,
) -> Optional[SessionOrigin]:
    """Authoritative delivery origin for ``task_id``.

    Prefer the durable objective origin (copied from the human/root session)
    over the current process session. A worker or auto-decomposer must not
    replace that origin with its own WebUI chat.

    Lifecycle notifications such as review-cap pass ``allow_live=False`` so a
    root supervisor with no durable origin fails closed instead of inventing
    the live worker WebUI session.
    """
    ensure_supervisor_tables(conn)
    parent_task = os.environ.get("HERMES_KANBAN_TASK") or ""
    is_child = bool(parent_task or _parents_of(conn, task_id))
    durable = _durable_notify_origin(conn, task_id)
    if is_child:
        # Child / worker create: never invent a live-session origin
        # (kanban_create from 73c58f750cba must not subscribe 7779276c4c10).
        return durable if durable and durable.usable else None
    if durable and durable.usable and _objective_origin(conn, task_id):
        return durable
    if not allow_live:
        return durable if durable and durable.usable else None
    live = capture_session_origin()
    # A leftover notify+wake row is not proof the current chat will wake.
    # Only reuse a stored sub when this process has no live session
    # (dispatcher-spawned worker / supervisor tick).
    if live.usable:
        return live
    return durable if durable and durable.usable else None


def _origin_from_notify_subs(
    conn: sqlite3.Connection, task_id: str
) -> Optional[SessionOrigin]:
    rows = conn.execute(
        "SELECT platform, chat_id, thread_id, notifier_profile, delivery_metadata "
        "FROM kanban_notify_subs WHERE task_id = ? ORDER BY created_at ASC",
        (task_id,),
    ).fetchall()
    for row in rows:
        platform = str(row["platform"] or "")
        chat_id = str(row["chat_id"] or "")
        thread_id = str(row["thread_id"] or "")
        session_key = ""
        meta = row["delivery_metadata"]
        if meta:
            try:
                parsed = json.loads(meta) if isinstance(meta, str) else meta
            except Exception:
                parsed = {}
            if isinstance(parsed, dict):
                session_key = str(parsed.get("session_key") or "")
        if platform.lower() in {"webui", "tui", "api_server"}:
            session_key = session_key or chat_id
        if platform and chat_id:
            return SessionOrigin(
                platform=platform,
                chat_id=chat_id,
                thread_id=thread_id,
                session_key=session_key,
                profile=str(row["notifier_profile"] or ""),
            )
    return None


def ensure_objective(
    conn: sqlite3.Connection,
    root_task_id: str,
    *,
    origin: Optional[SessionOrigin] = None,
    delegator_profile: Optional[str] = None,
    allow_live: bool = True,
) -> str:
    """Create or return the objective for ``root_task_id``.

    Lifecycle callers such as review-cap pass ``allow_live=False`` so a
    missing durable origin stays empty instead of capturing the live
    worker WebUI session and later treating it as a notify target.
    """
    ensure_supervisor_tables(conn)
    existing = get_objective_for_root(conn, root_task_id)
    if existing:
        return str(existing["id"])
    now = _now()
    if origin is None:
        origin = _durable_notify_origin(conn, root_task_id)
    worker_task = os.environ.get("HERMES_KANBAN_TASK") or ""
    if origin is None or not origin.usable:
        if worker_task or not allow_live:
            origin = SessionOrigin()
        else:
            origin = capture_session_origin()
    profile = (
        delegator_profile
        or origin.profile
        or os.environ.get("HERMES_PROFILE")
        or ""
    )
    oid = _new_id("obj_")
    conn.execute(
        """
        INSERT INTO kanban_objectives (
            id, root_task_id, delegator_profile,
            origin_platform, origin_chat_id, origin_thread_id, origin_session_key,
            status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
        """,
        (
            oid,
            root_task_id,
            profile or None,
            origin.platform or None,
            origin.chat_id or None,
            origin.thread_id or None,
            origin.session_key or None,
            now,
            now,
        ),
    )
    return oid


def upsert_unit(
    conn: sqlite3.Connection,
    *,
    objective_id: str,
    kind: str,
    ref: str,
    owner_profile: Optional[str] = None,
    terminal_predicate: str = "",
    next_gate: Optional[str] = "",
    status: str = "pending",
    proof: Optional[dict] = None,
) -> str:
    if kind not in UNIT_KINDS:
        raise ValueError(f"unknown unit kind: {kind}")
    if status not in UNIT_STATUSES:
        raise ValueError(f"unknown unit status: {status}")
    ensure_supervisor_tables(conn)
    now = _now()
    row = conn.execute(
        "SELECT id FROM kanban_objective_units "
        "WHERE objective_id = ? AND kind = ? AND ref = ?",
        (objective_id, kind, ref),
    ).fetchone()
    proof_json = json.dumps(proof, ensure_ascii=False) if proof else None
    if row:
        conn.execute(
            """
            UPDATE kanban_objective_units
               SET owner_profile = COALESCE(?, owner_profile),
                   last_progress_at = ?,
                   terminal_predicate = COALESCE(NULLIF(?, ''), terminal_predicate),
                   next_gate = COALESCE(NULLIF(?, ''), next_gate),
                   status = ?,
                   proof = COALESCE(?, proof)
             WHERE id = ?
            """,
            (
                owner_profile,
                now,
                terminal_predicate,
                next_gate,
                status,
                proof_json,
                row["id"],
            ),
        )
        return str(row["id"])
    uid = _new_id("ou_")
    conn.execute(
        """
        INSERT INTO kanban_objective_units (
            id, objective_id, kind, ref, owner_profile, last_progress_at,
            terminal_predicate, next_gate, status, proof
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uid,
            objective_id,
            kind,
            ref,
            owner_profile,
            now,
            terminal_predicate or _default_predicate(kind),
            next_gate or None,
            status,
            proof_json,
        ),
    )
    return uid


def _default_predicate(kind: str) -> str:
    if kind == "kanban":
        return "task_done_with_proof"
    if kind == "delegate_task":
        return "child_completed"
    return "bot_chat_terminal"


def list_units(conn: sqlite3.Connection, objective_id: str) -> list[dict]:
    if not _table_exists(conn, "kanban_objective_units"):
        return []
    rows = conn.execute(
        "SELECT * FROM kanban_objective_units WHERE objective_id = ?",
        (objective_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def note_kanban_child(
    conn: sqlite3.Connection,
    child_id: str,
    *,
    parents: Iterable[str] = (),
    owner_profile: Optional[str] = None,
) -> Optional[str]:
    """Upsert a kanban unit for a newly created child. Best-effort."""
    try:
        ensure_supervisor_tables(conn)
        parent_ids = [p for p in parents if p]
        if not parent_ids:
            parent_ids = _parents_of(conn, child_id)
        if not parent_ids:
            return None
        parent_roots: list[str] = []
        for pid in parent_ids:
            root = canonical_root_task_id(conn, pid)
            if root is None:
                logger.warning(
                    "note_kanban_child: refusing child %s; parent %s has "
                    "cross-objective ancestry",
                    child_id,
                    pid,
                )
                return None
            parent_roots.append(root)
        unique_roots = sorted(set(parent_roots))
        if len(unique_roots) != 1:
            logger.warning(
                "note_kanban_child: refusing cross-objective fan-in "
                "child=%s roots=%s",
                child_id,
                unique_roots,
            )
            return None
        root = unique_roots[0]
        # Child delivery follows the durable parent/objective origin.
        # Never fall back to the live process chat — that is how
        # kanban_create from 73c58f750cba subscribed 7779276c4c10.
        origin = _durable_notify_origin(conn, root)
        oid = ensure_objective(conn, root, origin=origin)
        task = conn.execute(
            "SELECT assignee FROM tasks WHERE id = ?", (child_id,)
        ).fetchone()
        assignee = owner_profile or (task["assignee"] if task else None)
        upsert_unit(
            conn,
            objective_id=oid,
            kind="kanban",
            ref=child_id,
            owner_profile=assignee,
            terminal_predicate="task_done_with_proof",
            status="pending",
        )
        if origin and origin.usable:
            _ensure_origin_subscription(conn, child_id, origin)
        return oid
    except Exception:
        logger.debug("note_kanban_child failed for %s", child_id, exc_info=True)
        return None


def note_delegate_spawn(
    *,
    subagent_id: str,
    owner_profile: Optional[str] = None,
    goal: str = "",
) -> Optional[str]:
    if not subagent_id:
        return None
    try:
        from hermes_cli import kanban_db as kb

        conn = kb.connect()
        try:
            ensure_supervisor_tables(conn)
            root = (
                os.environ.get("HERMES_KANBAN_TASK")
                or _synthetic_session_root()
            )
            origin = resolve_notify_origin(conn, root)
            oid = os.environ.get("HERMES_OBJECTIVE_ID") or ensure_objective(
                conn, root, origin=origin
            )
            upsert_unit(
                conn,
                objective_id=oid,
                kind="delegate_task",
                ref=subagent_id,
                owner_profile=owner_profile,
                terminal_predicate="child_completed",
                next_gate=(goal or "")[:200] or None,
                status="running",
            )
            return oid
        finally:
            conn.close()
    except Exception:
        logger.debug("note_delegate_spawn failed for %s", subagent_id, exc_info=True)
        return None


def note_delegate_stop(
    *,
    subagent_id: str,
    status: str = "done",
    summary: Optional[str] = None,
    result: Any = None,
) -> None:
    if not subagent_id:
        return
    from hermes_cli.kanban_supervision_contract import classify_terminal_result

    if result is not None:
        structured = result
    elif summary or (status and status != "done"):
        structured = {"status": status, "summary": summary}
    else:
        # Bare process/CLI exit with the default status is not a result.
        structured = None
    classification = classify_terminal_result(structured)
    try:
        from hermes_cli import kanban_db as kb

        conn = kb.connect()
        try:
            _mark_units_by_ref(
                conn,
                kind="delegate_task",
                ref=subagent_id,
                status="awaiting_verification",
                proof={
                    "summary": (summary or "")[:500],
                    "child_status": status,
                    "classification": classification,
                    "result": structured,
                    "terminal": "process_exit",
                    "verified": False,
                },
            )
            from hermes_cli.kanban_supervision_contract import wake_after_process_exit

            wake_after_process_exit(conn, kind="delegate_task", ref=subagent_id)
        finally:
            conn.close()
    except Exception:
        logger.debug("note_delegate_stop failed for %s", subagent_id, exc_info=True)


def note_bot_chat_handoff(
    *,
    session_id: str,
    title: str = "Bot Chat",
    owner_profile: Optional[str] = None,
) -> Optional[str]:
    if (title or "").strip() != "Bot Chat":
        return None
    if not session_id:
        return None
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli.profiles import get_active_profile_name

        profile = owner_profile or (get_active_profile_name() or "default")
        ref = f"{profile}:{session_id}"
        conn = kb.connect()
        try:
            root = os.environ.get("HERMES_KANBAN_TASK") or _synthetic_session_root()
            origin = resolve_notify_origin(conn, root)
            oid = os.environ.get("HERMES_OBJECTIVE_ID") or ensure_objective(
                conn, root, origin=origin
            )
            upsert_unit(
                conn,
                objective_id=oid,
                kind="bot_chat",
                ref=ref,
                owner_profile=profile,
                terminal_predicate="bot_chat_terminal",
                next_gate=title,
                status="running",
            )
            return oid
        finally:
            conn.close()
    except Exception:
        logger.debug("note_bot_chat_handoff failed for %s", session_id, exc_info=True)
        return None


def note_bot_chat_complete(
    *,
    session_id: str,
    owner_profile: Optional[str] = None,
    result: Any = None,
) -> None:
    """Record Bot Chat process exit. Exit is not unit completion."""
    if not session_id:
        return
    from hermes_cli.kanban_supervision_contract import classify_terminal_result

    classification = classify_terminal_result(result)
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli.profiles import get_active_profile_name

        profile = owner_profile or (get_active_profile_name() or "default")
        ref = f"{profile}:{session_id}"
        conn = kb.connect()
        try:
            _mark_units_by_ref(
                conn,
                kind="bot_chat",
                ref=session_id,
                status="awaiting_verification",
                proof={
                    "session_id": session_id,
                    "ref": ref,
                    "terminal": "process_exit",
                    "classification": classification,
                    "result": result,
                    "verified": False,
                },
            )
            from hermes_cli.kanban_supervision_contract import wake_after_process_exit

            wake_after_process_exit(conn, kind="bot_chat", ref=session_id)
        finally:
            conn.close()
    except Exception:
        logger.debug("note_bot_chat_complete failed for %s", session_id, exc_info=True)


def _synthetic_session_root() -> str:
    key = _session_env("HERMES_SESSION_KEY") or _session_env("HERMES_SESSION_ID") or "adhoc"
    return f"session:{key}"


def _mark_units_by_ref(
    conn: sqlite3.Connection,
    *,
    kind: str,
    ref: str,
    status: str,
    proof: Optional[dict] = None,
    objective_id: Optional[str] = None,
) -> None:
    ensure_supervisor_tables(conn)
    owning = objective_id or os.environ.get("HERMES_OBJECTIVE_ID") or ""
    if not owning and kind == "kanban":
        obj = get_objective_for_root(conn, _root_task_id(conn, ref))
        owning = str(obj["id"]) if obj else ""
    if not owning:
        return
    rows = conn.execute(
        "SELECT id, objective_id, ref FROM kanban_objective_units "
        "WHERE kind = ? AND objective_id = ?",
        (kind, owning),
    ).fetchall()
    now = _now()
    proof_json = json.dumps(proof, ensure_ascii=False) if proof else None
    for row in rows:
        if row["ref"] != ref and not str(row["ref"]).endswith(f":{ref}"):
            continue
        conn.execute(
            """
            UPDATE kanban_objective_units
               SET status = ?, last_progress_at = ?, proof = COALESCE(?, proof)
             WHERE id = ?
            """,
            (status, now, proof_json, row["id"]),
        )


def note_kanban_terminal(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    status: str,
    proof: Optional[dict] = None,
) -> None:
    try:
        ensure_supervisor_tables(conn)
        unit_status = {
            "done": "done",
            "completed": "done",
            "blocked": "blocked",
            "failed": "failed",
            "archived": "done",
        }.get(status, status if status in UNIT_STATUSES else "done")
        _mark_units_by_ref(
            conn,
            kind="kanban",
            ref=task_id,
            status=unit_status,
            proof=proof or {"task_status": status},
        )
        if unit_status == "done":
            _maybe_record_jude_proof(conn, task_id)
    except Exception:
        logger.debug("note_kanban_terminal failed for %s", task_id, exc_info=True)


_FULL_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _full_git_sha(value: Optional[str]) -> Optional[str]:
    text = str(value or "").strip().lower()
    if _FULL_GIT_SHA_RE.fullmatch(text):
        return text
    return None


def _authenticated_review_pass_receipt(
    conn: sqlite3.Connection, task_id: str, live_head: str
) -> Optional[dict]:
    """Canonical review-verdict event bound to this task and exact HEAD.

    Task comments are never authority, even when they contain
    ``jude-verdict: pass`` or a matching ``reviewed_head``.
    """
    if not _table_exists(conn, "kanban_supervisor_events"):
        return None
    rows = conn.execute(
        "SELECT payload FROM kanban_supervisor_events "
        "WHERE kind = 'review_verdict' AND task_id = ? "
        "ORDER BY created_at DESC",
        (task_id,),
    ).fetchall()
    for row in rows:
        raw = row["payload"] if isinstance(row, sqlite3.Row) else row[0]
        try:
            payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("verified") is not True:
            continue
        if payload.get("stale") or payload.get("blockers"):
            continue
        if str(payload.get("verdict") or "").strip().lower() != "pass":
            continue
        reviewed = _full_git_sha(payload.get("head"))
        current = _full_git_sha(payload.get("current_head"))
        generation = payload.get("generation")
        reviewed_generation = payload.get("reviewed_generation", generation)
        if reviewed is None or current is None:
            continue
        if reviewed != live_head or current != live_head or current != reviewed:
            continue
        if generation is not None and reviewed_generation is not None:
            if generation != reviewed_generation:
                continue
        return payload
    return None


def _maybe_record_jude_proof(conn: sqlite3.Connection, task_id: str) -> None:
    live = _full_git_sha(_task_git_head(conn, task_id))
    if not live:
        return
    receipt = _authenticated_review_pass_receipt(conn, task_id, live)
    if receipt is None:
        return
    root = canonical_root_task_id(conn, task_id)
    if root is None:
        return
    obj = get_objective_for_root(conn, root)
    owning = str(obj["id"]) if obj else ""
    if not owning:
        return
    proof = {
        "type": "jude_verdict",
        "verdict": "pass",
        "head": live,
        "verified": True,
        "receipt": "review_verdict",
    }
    conn.execute(
        """
        UPDATE kanban_objective_units
           SET proof = ?, terminal_predicate = 'jude_verdict_pass'
         WHERE kind = 'kanban' AND ref = ? AND objective_id = ?
        """,
        (json.dumps(proof, ensure_ascii=False), task_id, owning),
    )


def _task_git_head(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT workspace_path FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    path = (row["workspace_path"] if row else None) or ""
    return git_head(path)


def git_head(workspace_path: Optional[str]) -> Optional[str]:
    if not workspace_path:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return _full_git_sha((result.stdout or "").strip())


def invalidate_stale_reviews(
    conn: sqlite3.Connection, *, git_head_fn: Callable[[Optional[str]], Optional[str]] = git_head
) -> list[str]:
    """A moved or unreadable git head invalidates a prior jude-verdict: pass."""
    if not _table_exists(conn, "kanban_objective_units"):
        return []
    invalidated: list[str] = []
    rows = conn.execute(
        "SELECT u.id, u.ref, u.proof, u.objective_id, t.workspace_path "
        "FROM kanban_objective_units u "
        "LEFT JOIN tasks t ON t.id = u.ref "
        "WHERE u.kind = 'kanban' AND u.status = 'done' "
        "AND u.terminal_predicate = 'jude_verdict_pass'"
    ).fetchall()
    for row in rows:
        proof = {}
        if row["proof"]:
            try:
                proof = json.loads(row["proof"])
            except Exception:
                proof = {}
        recorded = (proof or {}).get("head")
        if not recorded:
            continue
        current = git_head_fn(row["workspace_path"])
        if not current or current != recorded:
            conn.execute(
                """
                UPDATE kanban_objective_units
                   SET status = 'pending', next_gate = 're-review',
                       last_progress_at = ?
                 WHERE id = ?
                """,
                (_now(), row["id"]),
            )
            obj = row["objective_id"]
            conn.execute(
                "UPDATE kanban_objectives SET status = 'open', updated_at = ? WHERE id = ? AND status = 'done'",
                (_now(), obj),
            )
            invalidated.append(str(row["ref"]))
    return invalidated


def has_active_pr_exemption(conn: sqlite3.Connection, task_id: str) -> bool:
    comments = conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ?",
        (task_id,),
    ).fetchall()
    for row in comments:
        body = (row["body"] or "").lower()
        if any(token in body for token in EXEMPTION_COMMENT_NEEDLES):
            return True
    if _table_exists(conn, "task_runs"):
        for row in conn.execute(
            "SELECT metadata FROM task_runs WHERE task_id = ? AND metadata IS NOT NULL",
            (task_id,),
        ).fetchall():
            try:
                meta = json.loads(row["metadata"]) if row["metadata"] else {}
            except Exception:
                meta = {}
            if isinstance(meta, dict) and meta.get("respawn_guard_exemption") in EXEMPTION_VALUES:
                return True
    if _table_exists(conn, "kanban_respawn_guard_state"):
        row = conn.execute(
            "SELECT exemption FROM kanban_respawn_guard_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row and row["exemption"] in EXEMPTION_VALUES:
            return True
    return False


def set_respawn_exemption(
    conn: sqlite3.Connection, task_id: str, exemption: str
) -> None:
    if exemption not in EXEMPTION_VALUES:
        raise ValueError(f"unknown exemption: {exemption}")
    ensure_supervisor_tables(conn)
    now = _now()
    conn.execute(
        """
        INSERT INTO kanban_respawn_guard_state (
            task_id, last_reason, consecutive_count, first_guard_at,
            last_guard_at, exemption, updated_at
        ) VALUES (?, NULL, 0, NULL, NULL, ?, ?)
        ON CONFLICT(task_id) DO UPDATE SET
            exemption = excluded.exemption,
            updated_at = excluded.updated_at
        """,
        (task_id, exemption, now),
    )


def clear_respawn_guard_streak(conn: sqlite3.Connection, task_id: str) -> None:
    if not _table_exists(conn, "kanban_respawn_guard_state"):
        return
    conn.execute(
        """
        UPDATE kanban_respawn_guard_state
           SET last_reason = NULL,
               consecutive_count = 0,
               first_guard_at = NULL,
               last_guard_at = NULL,
               updated_at = ?
         WHERE task_id = ?
        """,
        (_now(), task_id),
    )


def record_respawn_guard(
    conn: sqlite3.Connection,
    task_id: str,
    reason: str,
    *,
    now: Optional[int] = None,
    append_event: Optional[Callable[..., None]] = None,
) -> Optional[dict]:
    """Persist consecutive identical guard counts. Emit one starvation event."""
    ensure_supervisor_tables(conn)
    now = int(now if now is not None else _now())
    row = conn.execute(
        "SELECT * FROM kanban_respawn_guard_state WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    if row and row["last_reason"] == reason:
        count = int(row["consecutive_count"] or 0) + 1
        first_at = int(row["first_guard_at"] or now)
        last_starvation_at = row["last_starvation_at"]
        last_starvation_reason = row["last_starvation_reason"]
    else:
        count = 1
        first_at = now
        last_starvation_at = row["last_starvation_at"] if row else None
        last_starvation_reason = row["last_starvation_reason"] if row else None
    conn.execute(
        """
        INSERT INTO kanban_respawn_guard_state (
            task_id, last_reason, consecutive_count, first_guard_at,
            last_guard_at, last_starvation_at, last_starvation_reason,
            exemption, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(task_id) DO UPDATE SET
            last_reason = excluded.last_reason,
            consecutive_count = excluded.consecutive_count,
            first_guard_at = excluded.first_guard_at,
            last_guard_at = excluded.last_guard_at,
            updated_at = excluded.updated_at
        """,
        (
            task_id,
            reason,
            count,
            first_at,
            now,
            last_starvation_at,
            last_starvation_reason,
            row["exemption"] if row else None,
            now,
        ),
    )
    _mark_units_guarded(conn, task_id, reason)

    if count < STARVATION_THRESHOLD:
        return None
    cooldown_ok = True
    if last_starvation_at is not None and last_starvation_reason == reason:
        cooldown_ok = (now - int(last_starvation_at)) >= STARVATION_COOLDOWN_SECONDS
    if not cooldown_ok:
        return None
    event_key = f"starvation:{task_id}:{reason}:{first_at}"
    if _supervisor_event_seen(conn, event_key):
        return None
    payload = {
        "reason": reason,
        "count": count,
        "first_guard_at": first_at,
    }
    _record_supervisor_event(
        conn,
        event_key=event_key,
        kind="starvation",
        task_id=task_id,
        payload=payload,
    )
    if append_event is None:
        from hermes_cli.kanban_db import _append_event

        append_event = _append_event
    append_event(conn, task_id, "starvation", payload)
    conn.execute(
        """
        UPDATE kanban_respawn_guard_state
           SET last_starvation_at = ?, last_starvation_reason = ?, updated_at = ?
         WHERE task_id = ?
        """,
        (now, reason, now, task_id),
    )
    return {"task_id": task_id, **payload}


def _mark_units_guarded(conn: sqlite3.Connection, task_id: str, reason: str) -> None:
    if not _table_exists(conn, "kanban_objective_units"):
        return
    conn.execute(
        """
        UPDATE kanban_objective_units
           SET status = CASE WHEN status IN ('done', 'failed') THEN status ELSE 'guarded' END,
               next_gate = ?, last_progress_at = ?
         WHERE kind = 'kanban' AND ref = ?
        """,
        (f"respawn_guard:{reason}", _now(), task_id),
    )


def _supervisor_event_seen(conn: sqlite3.Connection, event_key: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM kanban_supervisor_events WHERE event_key = ?",
        (event_key,),
    ).fetchone()
    return row is not None


def _record_supervisor_event(
    conn: sqlite3.Connection,
    *,
    event_key: str,
    kind: str,
    task_id: Optional[str] = None,
    objective_id: Optional[str] = None,
    payload: Optional[dict] = None,
) -> bool:
    """Insert an idempotent supervisor event. Returns True if newly recorded."""
    ensure_supervisor_tables(conn)
    try:
        conn.execute(
            """
            INSERT INTO kanban_supervisor_events (
                event_key, kind, task_id, objective_id, payload, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event_key,
                kind,
                task_id,
                objective_id,
                json.dumps(payload, ensure_ascii=False) if payload else None,
                _now(),
            ),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def _ensure_origin_subscription(
    conn: sqlite3.Connection,
    task_id: str,
    origin: SessionOrigin,
) -> None:
    if not origin.usable:
        return
    try:
        from hermes_cli.kanban_db import add_notify_sub, list_notify_subs

        existing = list_notify_subs(conn, task_id)
        if existing:
            # Inherit already copied parent routing, including
            # delivery_metadata. Do not add a competing live chat.
            return
        chat_id = origin.notify_chat_id()
        metadata = {}
        if origin.session_key:
            metadata["session_key"] = origin.session_key
        if origin.thread_id:
            metadata["thread_id"] = origin.thread_id
        add_notify_sub(
            conn,
            task_id=task_id,
            platform=origin.platform,
            chat_id=chat_id,
            thread_id=origin.thread_id or None,
            notifier_profile=origin.profile or None,
            delivery_mode="notify+wake",
            delivery_metadata=metadata or None,
        )
    except Exception:
        logger.debug("ensure origin subscription failed for %s", task_id, exc_info=True)


def detect_update_existing_pr_intent(conn: sqlite3.Connection, task_id: str) -> bool:
    if has_active_pr_exemption(conn, task_id):
        return True
    needles = (
        "update existing pr",
        "update-existing-pr",
        "update the existing pr",
        "push to the open pr",
    )
    row = conn.execute("SELECT body, title FROM tasks WHERE id = ?", (task_id,)).fetchone()
    blobs = []
    if row:
        blobs.append(row["title"] or "")
        blobs.append(row["body"] or "")
    for comment in conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ?", (task_id,)
    ).fetchall():
        blobs.append(comment["body"] or "")
    text = "\n".join(blobs).lower()
    return any(n in text for n in needles)


def requeue_with_exemption(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    exemption: str = "update_existing_pr",
    append_event: Optional[Callable[..., None]] = None,
) -> None:
    set_respawn_exemption(conn, task_id, exemption)
    if append_event is None:
        from hermes_cli.kanban_db import _append_event

        append_event = _append_event
    append_event(
        conn,
        task_id,
        "status",
        {"status": "ready", "reason": "supervisor_requeue", "exemption": exemption},
    )
    clear_respawn_guard_streak(conn, task_id)


class RemokoClient:
    """Narrow adapter over the live Remoko inbox. Injected in tests."""

    def request(self, payload: dict) -> dict:
        raise NotImplementedError

    def get_request(self, request_id: str) -> dict:
        raise NotImplementedError

    def find_request(self, external_id: str) -> dict:
        """Look up a live request by durable external_id. Empty if none."""
        return {}


def _unwrap_remoko_payload(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    content = raw.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if item.get("type") == "text" and isinstance(text, str) and text.strip():
                try:
                    parsed = json.loads(text)
                except Exception:
                    continue
                if isinstance(parsed, dict):
                    return parsed
    result = raw.get("result")
    if isinstance(result, dict):
        nested = _unwrap_remoko_payload(result)
        return nested or result
    return raw


def _live_owner_answer(raw: Any) -> Optional[str]:
    data = _unwrap_remoko_payload(raw)
    status = str(data.get("status") or data.get("state") or "").strip().lower()
    if status in {"pending", "waiting", "open", "unanswered", "cancelled", "canceled", "expired"}:
        return None
    for key in ("answer", "choice", "selected", "user_response", "response"):
        value = data.get(key)
        if isinstance(value, list) and value:
            value = value[0]
        if value not in {None, ""}:
            return str(value)
    return None


def _live_remoko_client() -> Optional[RemokoClient]:
    """Use the live Remoko MCP surface when importable. Never invent an inbox."""
    try:
        from tools.mcp_tool import call_mcp_tool  # type: ignore
    except Exception:
        call_mcp_tool = None
    if call_mcp_tool is None:
        return None

    class _McpRemoko(RemokoClient):
        def request(self, payload: dict) -> dict:
            tool = (
                "mcp__remoko__ask_question"
                if payload.get("choices")
                else "mcp__remoko__request_approval"
            )
            result = call_mcp_tool(tool, payload)
            if isinstance(result, dict):
                return result
            return {"raw": result}

        def get_request(self, request_id: str) -> dict:
            result = call_mcp_tool("mcp__remoko__get_response", {"request_id": request_id})
            if isinstance(result, dict):
                return result
            return {"raw": result}

        def find_request(self, external_id: str) -> dict:
            for tool in (
                "mcp__remoko__list_pending",
                "mcp__remoko__list_unprocessed",
            ):
                try:
                    result = call_mcp_tool(tool, {})
                except Exception:
                    logger.debug("remoko %s failed", tool, exc_info=True)
                    continue
                match = _match_remoko_external(result, external_id)
                if match:
                    return match
            return {}

    return _McpRemoko()


def _existing_remoko_request_id(
    conn: sqlite3.Connection, obj: dict[str, Any], external_id: str
) -> Optional[str]:
    if obj.get("remoko_external_id") == external_id and obj.get("remoko_request_id"):
        return str(obj["remoko_request_id"])
    return None


def _parse_event_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _supervisor_event(conn: sqlite3.Connection, event_key: str) -> Optional[dict[str, Any]]:
    if not _table_exists(conn, "kanban_supervisor_events"):
        return None
    row = conn.execute(
        "SELECT event_key, kind, task_id, objective_id, payload, created_at "
        "FROM kanban_supervisor_events WHERE event_key = ?",
        (event_key,),
    ).fetchone()
    return dict(row) if row else None


def _remoko_request_id_from(data: Any) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    request_id = str(data.get("request_id") or data.get("id") or "").strip()
    external_id = str(data.get("external_id") or "").strip()
    if not request_id or request_id == external_id:
        return None
    return request_id


def _iter_remoko_records(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        candidates: Any = raw
    else:
        data = _unwrap_remoko_payload(raw)
        if isinstance(data, list):
            candidates = data
        elif isinstance(data, dict):
            nested = (
                data.get("items")
                or data.get("requests")
                or data.get("pending")
                or data.get("unprocessed")
                or data.get("data")
            )
            if isinstance(nested, list):
                candidates = nested
            elif isinstance(nested, dict):
                candidates = list(nested.values())
            else:
                candidates = [data]
        else:
            candidates = []
    records: list[dict[str, Any]] = []
    for item in candidates or []:
        if not isinstance(item, dict):
            continue
        unwrapped = _unwrap_remoko_payload(item)
        records.append(unwrapped or item)
    return records


def _match_remoko_external(raw: Any, external_id: str) -> dict[str, Any]:
    if not external_id:
        return {}
    for rec in _iter_remoko_records(raw):
        if rec.get("external_id") == external_id and _remoko_request_id_from(rec):
            return rec
    return {}


def _find_live_remoko_request(client: RemokoClient, external_id: str) -> Optional[str]:
    finder = getattr(client, "find_request", None)
    raw: Any = {}
    if callable(finder):
        try:
            raw = finder(external_id)
        except Exception:
            logger.warning("remoko find_request failed for %s", external_id, exc_info=True)
            return None
    rec = _match_remoko_external(raw, external_id)
    if not rec and isinstance(raw, dict):
        rec = raw if raw.get("external_id") == external_id else {}
    return _remoko_request_id_from(rec)


def _commit_owner_blocker_request(
    conn: sqlite3.Connection,
    *,
    objective_id: str,
    task_id: str,
    decision_key: str,
    external_id: str,
    event_key: str,
    request_id: str,
    choices: Optional[list[str]] = None,
) -> str:
    from hermes_cli.kanban_db import _append_event, block_task, write_txn

    with write_txn(conn, allow_nested=True):
        obj = get_objective(conn, objective_id)
        already = _existing_remoko_request_id(conn, obj or {}, external_id)
        if already:
            return already
        event = _supervisor_event(conn, event_key)
        prior = _parse_event_payload((event or {}).get("payload"))
        already_logged = _remoko_request_id_from(prior) == request_id
        conn.execute(
            """
            UPDATE kanban_supervisor_events
               SET payload = ?
             WHERE event_key = ?
            """,
            (
                json.dumps(
                    {
                        "external_id": external_id,
                        "decision_key": decision_key,
                        "request_id": request_id,
                        "status": "sent",
                        "choices": list(choices or prior.get("choices") or [])[:4],
                    },
                    ensure_ascii=False,
                ),
                event_key,
            ),
        )
        conn.execute(
            """
            UPDATE kanban_objectives
               SET remoko_request_id = ?, remoko_external_id = ?,
                   status = 'blocked_owner', updated_at = ?
             WHERE id = ?
            """,
            (request_id, external_id, _now(), objective_id),
        )
        if not already_logged:
            _append_event(
                conn,
                task_id,
                "owner_blocker",
                {
                    "objective_id": objective_id,
                    "external_id": external_id,
                    "request_id": request_id,
                    "decision_key": decision_key,
                },
            )
    try:
        block_task(
            conn,
            task_id,
            reason=f"owner decision required: {decision_key}",
            kind="needs_input",
        )
    except Exception:
        logger.debug("block_task for owner blocker failed", exc_info=True)
    return request_id


def _recover_reserved_owner_blocker(
    conn: sqlite3.Connection,
    *,
    objective_id: str,
    task_id: str,
    decision_key: str,
    external_id: str,
    event_key: str,
    choices: Optional[list[str]] = None,
    remoko: Optional[RemokoClient] = None,
) -> tuple[bool, Optional[str]]:
    """Reconcile a reserving event. reserved=True means do not send again."""
    event = _supervisor_event(conn, event_key)
    if event is None:
        return False, None
    obj = get_objective(conn, objective_id)
    already = _existing_remoko_request_id(conn, obj or {}, external_id)
    if already:
        return True, already
    payload = _parse_event_payload(event.get("payload"))
    request_id = _remoko_request_id_from(payload)
    if request_id is None:
        client = remoko if remoko is not None else _live_remoko_client()
        if client is None:
            return True, None
        request_id = _find_live_remoko_request(client, external_id)
    if not request_id:
        return True, None
    bound = _commit_owner_blocker_request(
        conn,
        objective_id=objective_id,
        task_id=task_id,
        decision_key=decision_key,
        external_id=external_id,
        event_key=event_key,
        request_id=request_id,
        choices=list(choices or payload.get("choices") or [])[:4],
    )
    return True, bound


def reconcile_reserved_owner_blockers(
    conn: sqlite3.Connection,
    *,
    remoko: Optional[RemokoClient] = None,
) -> list[str]:
    """Attach accepted Remoko requests left behind a reserving crash."""
    if not _table_exists(conn, "kanban_supervisor_events"):
        return []
    recovered: list[str] = []
    rows = conn.execute(
        "SELECT event_key, task_id, objective_id, payload "
        "FROM kanban_supervisor_events WHERE kind = 'owner_blocker' "
        "ORDER BY created_at ASC"
    ).fetchall()
    for row in rows:
        payload = _parse_event_payload(row["payload"])
        if payload.get("status") != "reserving":
            continue
        objective_id = row["objective_id"]
        task_id = row["task_id"]
        decision_key = str(payload.get("decision_key") or "")
        external_id = str(payload.get("external_id") or "")
        if not objective_id or not task_id or not decision_key or not external_id:
            continue
        _reserved, request_id = _recover_reserved_owner_blocker(
            conn,
            objective_id=str(objective_id),
            task_id=str(task_id),
            decision_key=decision_key,
            external_id=external_id,
            event_key=str(row["event_key"]),
            choices=list(payload.get("choices") or [])[:4],
            remoko=remoko,
        )
        if request_id:
            recovered.append(request_id)
    return recovered


def _delete_supervisor_event(conn: sqlite3.Connection, event_key: str) -> None:
    if not _table_exists(conn, "kanban_supervisor_events"):
        return
    conn.execute(
        "DELETE FROM kanban_supervisor_events WHERE event_key = ?",
        (event_key,),
    )


def request_owner_blocker(
    conn: sqlite3.Connection,
    *,
    objective_id: str,
    task_id: str,
    decision_key: str,
    purpose: str,
    choices: Optional[list[str]] = None,
    recommendation: str = "",
    consequence: str = "",
    prohibitions: str = "",
    risk: str = "high",
    remoko: Optional[RemokoClient] = None,
) -> Optional[str]:
    """Exactly one deduplicated Remoko request per objective+decision_key."""
    from hermes_cli.kanban_db import write_txn

    ensure_supervisor_tables(conn)
    obj = get_objective(conn, objective_id)
    if obj is None:
        return None
    external_id = f"obj-{objective_id}-{decision_key}"
    event_key = f"remoko:{objective_id}:{decision_key}"
    existing = _existing_remoko_request_id(conn, obj, external_id)
    if existing:
        return existing
    _reserved, recovered = _recover_reserved_owner_blocker(
        conn,
        objective_id=objective_id,
        task_id=task_id,
        decision_key=decision_key,
        external_id=external_id,
        event_key=event_key,
        choices=choices,
        remoko=remoko,
    )
    if recovered:
        return recovered
    if _reserved:
        return _existing_remoko_request_id(conn, obj, external_id)
    with write_txn(conn, allow_nested=True):
        obj = get_objective(conn, objective_id)
        if obj is None:
            return None
        existing = _existing_remoko_request_id(conn, obj, external_id)
        if existing:
            return existing
        _reserved, recovered = _recover_reserved_owner_blocker(
            conn,
            objective_id=objective_id,
            task_id=task_id,
            decision_key=decision_key,
            external_id=external_id,
            event_key=event_key,
            choices=choices,
            remoko=remoko,
        )
        if recovered:
            return recovered
        if _reserved:
            return _existing_remoko_request_id(conn, obj, external_id)
        claimed = _record_supervisor_event(
            conn,
            event_key=event_key,
            kind="owner_blocker",
            task_id=task_id,
            objective_id=objective_id,
            payload={
                "external_id": external_id,
                "decision_key": decision_key,
                "status": "reserving",
                "choices": list(choices or [])[:4],
            },
        )
        if not claimed:
            _reserved, recovered = _recover_reserved_owner_blocker(
                conn,
                objective_id=objective_id,
                task_id=task_id,
                decision_key=decision_key,
                external_id=external_id,
                event_key=event_key,
                choices=choices,
                remoko=remoko,
            )
            return recovered or _existing_remoko_request_id(conn, obj, external_id)

    payload = {
        "question": purpose[:200],
        "context": purpose[:2000],
        "recommendation": (recommendation or "")[:1000],
        "consequence": (consequence or "")[:1000],
        "prohibitions": prohibitions,
        "risk": risk,
        "external_id": external_id,
        "source": task_id,
        "root_task_id": obj.get("root_task_id"),
    }
    if choices:
        payload["choices"] = list(choices)[:4]

    client = remoko if remoko is not None else _live_remoko_client()
    if client is None:
        logger.warning("remoko unavailable; not fabricating request id for %s", external_id)
        with write_txn(conn, allow_nested=True):
            _delete_supervisor_event(conn, event_key)
        return None
    try:
        result = client.request(payload)
    except Exception:
        logger.warning("remoko request failed for %s", external_id, exc_info=True)
        with write_txn(conn, allow_nested=True):
            _delete_supervisor_event(conn, event_key)
        return None
    request_id = str((result or {}).get("request_id") or (result or {}).get("id") or "")
    if not request_id or request_id == external_id:
        logger.warning("remoko returned no real request_id for %s", external_id)
        with write_txn(conn, allow_nested=True):
            _delete_supervisor_event(conn, event_key)
        return None

    return _commit_owner_blocker_request(
        conn,
        objective_id=objective_id,
        task_id=task_id,
        decision_key=decision_key,
        external_id=external_id,
        event_key=event_key,
        request_id=request_id,
        choices=list(choices or [])[:4],
    )


def revalidate_owner_answer(
    conn: sqlite3.Connection,
    *,
    objective_id: str,
    answer: Any,
    expected_external_id: str,
    current_head: Optional[str] = None,
    expected_head: Optional[str] = None,
    remoko: Optional[RemokoClient] = None,
) -> bool:
    """Revalidate a Remoko answer against the live inbox and current repo/task state."""
    obj = get_objective(conn, objective_id)
    if obj is None:
        return False
    if not obj.get("remoko_request_id"):
        return False
    if obj.get("remoko_external_id") != expected_external_id:
        return False
    if expected_head and current_head and expected_head != current_head:
        return False
    if answer in {None, ""}:
        return False
    client = remoko if remoko is not None else _live_remoko_client()
    if client is None:
        return False
    try:
        live_raw = client.get_request(str(obj["remoko_request_id"]))
    except Exception:
        logger.warning("remoko get_request failed for %s", obj.get("remoko_request_id"), exc_info=True)
        return False
    live = _unwrap_remoko_payload(live_raw)
    live_ext = live.get("external_id")
    if live_ext and live_ext != expected_external_id:
        return False
    live_rid = live.get("request_id") or live.get("id")
    if live_rid and str(live_rid) != str(obj["remoko_request_id"]):
        return False
    live_answer = _live_owner_answer(live)
    if live_answer is None or str(answer) != live_answer:
        return False
    choices = _owner_blocker_choices(conn, objective_id, expected_external_id)
    if choices and str(answer) not in choices:
        return False
    return True


_PARK_ANSWERS = frozenset({
    "wait", "leave it parked", "stop", "don't", "do not", "park",
})


def _owner_blocker_choices(
    conn: sqlite3.Connection, objective_id: str, external_id: str
) -> list[str]:
    if not _table_exists(conn, "kanban_supervisor_events"):
        return []
    rows = conn.execute(
        "SELECT payload FROM kanban_supervisor_events "
        "WHERE kind = 'owner_blocker' AND objective_id = ?",
        (objective_id,),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except Exception:
            continue
        if payload.get("external_id") != external_id:
            continue
        choices = payload.get("choices") or []
        return [str(c) for c in choices]
    return []


def resume_after_owner_answer(
    conn: sqlite3.Connection,
    *,
    objective_id: str,
    task_id: str,
    answer: Any,
    expected_external_id: str,
    report_execution: Optional[Callable[..., Any]] = None,
    remoko: Optional[RemokoClient] = None,
) -> bool:
    if not revalidate_owner_answer(
        conn,
        objective_id=objective_id,
        answer=answer,
        expected_external_id=expected_external_id,
        remoko=remoko,
    ):
        return False
    if str(answer).strip().lower() in _PARK_ANSWERS:
        return False
    conn.execute(
        """
        UPDATE kanban_objectives
           SET status = 'open', updated_at = ?
         WHERE id = ?
        """,
        (_now(), objective_id),
    )
    from hermes_cli.kanban_db import _append_event, unblock_task

    try:
        unblock_task(conn, task_id)
    except Exception:
        logger.debug("unblock after remoko answer failed", exc_info=True)
    _append_event(
        conn,
        task_id,
        "status",
        {"status": "ready", "reason": "owner_answer_revalidated"},
    )
    if report_execution is not None:
        try:
            report_execution(
                request_id=get_objective(conn, objective_id).get("remoko_request_id"),
                status="accepted",
            )
        except Exception:
            logger.debug("report_execution failed", exc_info=True)
    return True


def objective_is_complete(conn: sqlite3.Connection, objective_id: str) -> bool:
    units = list_units(conn, objective_id)
    if not units:
        return False
    for unit in units:
        if not _unit_satisfies_predicate(conn, unit):
            return False
    return True


def _unit_satisfies_predicate(conn: sqlite3.Connection, unit: dict) -> bool:
    status = unit.get("status")
    if status != "done":
        return False
    predicate = unit.get("terminal_predicate") or ""
    proof = {}
    if unit.get("proof"):
        try:
            proof = json.loads(unit["proof"]) if isinstance(unit["proof"], str) else (unit["proof"] or {})
        except Exception:
            proof = {}
    if not proof:
        return False
    if predicate == "jude_verdict_pass":
        if proof.get("blockers") or proof.get("stale"):
            return False
        recorded = (proof or {}).get("head")
        current = _task_git_head(conn, unit["ref"])
        if not recorded or not current or current != recorded:
            return False
        return (proof or {}).get("verdict") == "pass" and proof.get("verified") is True
    if predicate == "task_done_with_proof":
        if unit.get("kind") == "kanban":
            task = conn.execute(
                "SELECT status FROM tasks WHERE id = ?",
                (unit["ref"],),
            ).fetchone()
            if task is None or task["status"] not in {"done", "archived"}:
                return False
        return True
    if predicate == "child_completed":
        return (
            proof.get("verified") is True
            and str(proof.get("classification") or "").lower() == "success"
        )
    if predicate == "bot_chat_terminal":
        return (
            proof.get("verified") is True
            and str(proof.get("classification") or "").lower() == "success"
        )
    return False


def reconcile_objective(conn: sqlite3.Connection, objective_id: str) -> str:
    """Refresh unit statuses from durable sources; maybe mark the objective done."""
    ensure_supervisor_tables(conn)
    units = list_units(conn, objective_id)
    for unit in units:
        if unit["kind"] == "kanban":
            task = conn.execute(
                "SELECT status FROM tasks WHERE id = ?",
                (unit["ref"],),
            ).fetchone()
            if task is None:
                continue
            mapped = {
                "done": "done",
                "archived": "done",
                "blocked": "blocked",
                "running": "running",
                "review": "running",
                "ready": "pending",
                "todo": "pending",
            }.get(task["status"])
            if mapped and mapped != unit["status"]:
                if unit["status"] in {"done", "failed"} and mapped != "done":
                    continue
                conn.execute(
                    "UPDATE kanban_objective_units SET status = ?, last_progress_at = ? WHERE id = ?",
                    (mapped, _now(), unit["id"]),
                )
    invalidate_stale_reviews(conn)
    if objective_is_complete(conn, objective_id):
        conn.execute(
            "UPDATE kanban_objectives SET status = 'done', updated_at = ? WHERE id = ?",
            (_now(), objective_id),
        )
        return "done"
    obj = get_objective(conn, objective_id)
    if obj and obj.get("status") == "done":
        conn.execute(
            "UPDATE kanban_objectives SET status = 'open', updated_at = ? WHERE id = ?",
            (_now(), objective_id),
        )
        return "open"
    return (obj or {}).get("status") or "open"


def handle_starvation(
    conn: sqlite3.Connection,
    task_id: str,
    reason: str,
    *,
    remoko: Optional[RemokoClient] = None,
    check_guard: Optional[Callable[..., Optional[str]]] = None,
) -> dict:
    """Revalidate a starved card and take the next safe action."""
    from hermes_cli import kanban_db as kb

    if check_guard is None:
        check_guard = kb.check_respawn_guard
    current = check_guard(conn, task_id)
    action = {"task_id": task_id, "reason": reason, "action": "none"}
    if current is None:
        clear_respawn_guard_streak(conn, task_id)
        action["action"] = "auto_repaired"
        return action
    if reason == "active_pr" and detect_update_existing_pr_intent(conn, task_id):
        requeue_with_exemption(conn, task_id, exemption="update_existing_pr")
        action["action"] = "requeued_exemption"
        return action
    if reason in {"blocker_auth"} or reason == "active_pr":
        root = _root_task_id(conn, task_id)
        origin = resolve_notify_origin(conn, task_id) or capture_session_origin()
        oid = ensure_objective(conn, root, origin=origin)
        _ensure_origin_subscription(conn, task_id, origin)
        if reason == "active_pr":
            request_id = request_owner_blocker(
                conn,
                objective_id=oid,
                task_id=task_id,
                decision_key="active_pr_starvation",
                purpose=(
                    "A card that already has an open pull request is stuck and "
                    "cannot start again without a choice."
                ),
                choices=[
                    "Update existing PR",
                    "Open a new PR",
                    "Leave it parked",
                    "Wait",
                ],
                recommendation="Update existing PR — keep one reviewable change.",
                consequence="The worker will resume on the existing pull request.",
                prohibitions="Do not merge, deploy, or restart the gateway.",
                risk="high",
                remoko=remoko,
            )
            action["action"] = "owner_blocker"
            action["request_id"] = request_id
            return action
        request_id = request_owner_blocker(
            conn,
            objective_id=oid,
            task_id=task_id,
            decision_key=f"guard_{reason}",
            purpose="Delegated work hit a wall that needs an owner decision.",
            choices=["Retry now", "Fix credentials", "Reroute owner", "Stop"],
            recommendation="Fix credentials — retrying the same wall wastes runs.",
            consequence="The card stays parked until that choice is applied.",
            prohibitions="Do not pretend the guard is a finished result.",
            risk="high",
            remoko=remoko,
        )
        action["action"] = "owner_blocker"
        action["request_id"] = request_id
        return action
    action["action"] = "notified"
    return action


def _starvation_handling_is_terminal(action: Optional[dict[str, Any]]) -> bool:
    """A failed Remoko send is not a handled starvation event.

    ``request_owner_blocker()`` deletes the reservation and returns no
    request id. Recording ``handled:{starvation}`` in that state would
    skip later ticks and never retry the owner blocker.
    """
    if not action:
        return False
    if action.get("action") == "owner_blocker" and not action.get("request_id"):
        return False
    return True


def supervise_once(
    conn: sqlite3.Connection,
    *,
    remoko: Optional[RemokoClient] = None,
    dry_run: bool = False,
) -> SupervisorResult:
    """One supervisor tick: reconcile ledger, act on starvation, complete."""
    from hermes_cli.kanban_db import write_txn

    result = SupervisorResult()
    if dry_run:
        return result
    ensure_supervisor_tables(conn)
    result.invalidated_reviews = invalidate_stale_reviews(conn)
    from hermes_cli.kanban_supervision_contract import reconcile_process_exits

    for action in reconcile_process_exits(conn):
        wake = action.get("wake") or {}
        if wake.get("ok"):
            result.wake_retries.append(str(action.get("unit_id") or ""))
    if _table_exists(conn, "kanban_objectives"):
        for row in conn.execute(
            "SELECT id FROM kanban_objectives WHERE status != 'done'"
        ).fetchall():
            status = reconcile_objective(conn, row["id"])
            if status == "done":
                result.completed_objectives.append(row["id"])
    recovered = reconcile_reserved_owner_blockers(conn, remoko=remoko)
    result.remoko_requests.extend(recovered)
    if _table_exists(conn, "kanban_supervisor_events"):
        rows = conn.execute(
            "SELECT event_key, task_id, payload FROM kanban_supervisor_events "
            "WHERE kind = 'starvation' ORDER BY created_at ASC"
        ).fetchall()
        for row in rows:
            payload = {}
            if row["payload"]:
                try:
                    payload = json.loads(row["payload"])
                except Exception:
                    payload = {}
            handled_key = f"handled:{row['event_key']}"
            handled = _supervisor_event(conn, handled_key)
            if handled is not None:
                handled_payload = _parse_event_payload(handled.get("payload"))
                if _starvation_handling_is_terminal(handled_payload):
                    continue
                with write_txn(conn, allow_nested=True):
                    _delete_supervisor_event(conn, handled_key)
            action = handle_starvation(
                conn,
                row["task_id"],
                str(payload.get("reason") or "active_pr"),
                remoko=remoko,
            )
            if _starvation_handling_is_terminal(action):
                _record_supervisor_event(
                    conn,
                    event_key=handled_key,
                    kind="starvation_handled",
                    task_id=row["task_id"],
                    payload=action,
                )
            result.starvation.append(action)
            if action.get("action") == "auto_repaired":
                result.repaired.append(row["task_id"])
            if action.get("request_id"):
                result.remoko_requests.append(str(action["request_id"]))
    return result


def record_wake_failure(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    error: str,
    append_event: Optional[Callable[..., None]] = None,
) -> None:
    """A rewind is a retry, not a delivery. Make the failure visible."""
    payload = {
        "platform": platform,
        "chat_id": chat_id,
        "error": (error or "")[:400],
    }
    event_key = f"wake_failed:{task_id}:{platform}:{chat_id}:{_now() // 60}"
    _record_supervisor_event(
        conn,
        event_key=event_key,
        kind="wake_failed",
        task_id=task_id,
        payload=payload,
    )
    if append_event is None:
        from hermes_cli.kanban_db import _append_event

        append_event = _append_event
    append_event(conn, task_id, "wake_failed", payload)
