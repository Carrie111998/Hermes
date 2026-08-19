"""Durable delegation supervisor for Kanban / delegate_task / Bot Chat.

Keeps an objective ledger alive after the creating session returns. A
guard, an open PR, a completed child, or parent-loop exhaustion is never
a terminal success. See LS-2776.
"""

from __future__ import annotations

import json
import logging
import os
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
    {"pending", "running", "guarded", "blocked", "done", "failed"}
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
        "SELECT parent_id FROM task_links WHERE child_id = ?",
        (task_id,),
    ).fetchall()
    return [r["parent_id"] if isinstance(r, sqlite3.Row) else r[0] for r in rows]


def _root_task_id(conn: sqlite3.Connection, task_id: str) -> str:
    seen: set[str] = set()
    current = task_id
    while current and current not in seen:
        seen.add(current)
        parents = _parents_of(conn, current)
        if not parents:
            return current
        current = parents[0]
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


def resolve_notify_origin(
    conn: sqlite3.Connection, task_id: str
) -> Optional[SessionOrigin]:
    """Authoritative delivery origin for ``task_id``.

    Prefer the durable objective origin (copied from the human/root session)
    over the current process session. A worker or auto-decomposer must not
    replace that origin with its own WebUI chat.
    """
    ensure_supervisor_tables(conn)
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
    live = capture_session_origin()
    # A leftover notify+wake row is not proof the current chat will wake.
    # Only reuse a stored sub when this process has no live session
    # (dispatcher-spawned worker / supervisor tick).
    if not live.usable:
        if parent_task:
            origin = _origin_from_notify_subs(conn, parent_task)
            if origin and origin.usable:
                return origin
        origin = _origin_from_notify_subs(conn, root)
        if origin and origin.usable:
            return origin
    if parent_task or _parents_of(conn, task_id):
        # Worker-created child with no inherited origin: do not invent one
        # from a possibly-stale current chat.
        return None
    return live if live.usable else None


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
) -> str:
    ensure_supervisor_tables(conn)
    existing = get_objective_for_root(conn, root_task_id)
    if existing:
        return str(existing["id"])
    now = _now()
    origin = origin or resolve_notify_origin(conn, root_task_id) or capture_session_origin()
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
            return None
        root = _root_task_id(conn, parent_ids[0])
        origin = resolve_notify_origin(conn, root) or capture_session_origin()
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
        _ensure_origin_subscription(conn, child_id, origin)
        _ensure_origin_subscription(conn, root, origin)
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
    if not supervisor_context_active() and not os.environ.get("HERMES_OBJECTIVE_ID"):
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
            origin = resolve_notify_origin(conn, root) or capture_session_origin()
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
) -> None:
    if not subagent_id:
        return
    unit_status = "done" if status in {"completed", "done", "ok", "success"} else (
        "failed" if status in {"failed", "error", "timeout", "cancelled"} else "done"
    )
    try:
        from hermes_cli import kanban_db as kb

        conn = kb.connect()
        try:
            _mark_units_by_ref(
                conn,
                kind="delegate_task",
                ref=subagent_id,
                status=unit_status,
                proof={"summary": (summary or "")[:500], "child_status": status},
            )
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
    if not supervisor_context_active() and not os.environ.get("HERMES_OBJECTIVE_ID"):
        return None
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli.profiles import get_active_profile_name

        profile = owner_profile or (get_active_profile_name() or "default")
        ref = f"{profile}:{session_id}"
        conn = kb.connect()
        try:
            root = os.environ.get("HERMES_KANBAN_TASK") or _synthetic_session_root()
            origin = resolve_notify_origin(conn, root) or capture_session_origin()
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


def note_bot_chat_complete(*, session_id: str, owner_profile: Optional[str] = None) -> None:
    if not session_id:
        return
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
                status="done",
                proof={"session_id": session_id, "terminal": "cli_return"},
            )
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
) -> None:
    ensure_supervisor_tables(conn)
    rows = conn.execute(
        "SELECT id, objective_id, ref FROM kanban_objective_units WHERE kind = ?",
        (kind,),
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


def _maybe_record_jude_proof(conn: sqlite3.Connection, task_id: str) -> None:
    comments = conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? ORDER BY created_at DESC",
        (task_id,),
    ).fetchall()
    has_pass = any(
        (c["body"] or "") and "jude-verdict: pass" in (c["body"] or "").lower()
        for c in comments
    )
    if not has_pass:
        return
    head = _task_git_head(conn, task_id)
    proof = {"type": "jude_verdict", "verdict": "pass", "head": head}
    conn.execute(
        """
        UPDATE kanban_objective_units
           SET proof = ?, terminal_predicate = 'jude_verdict_pass'
         WHERE kind = 'kanban' AND ref = ?
        """,
        (json.dumps(proof, ensure_ascii=False), task_id),
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
    head = (result.stdout or "").strip()
    return head or None


def invalidate_stale_reviews(
    conn: sqlite3.Connection, *, git_head_fn: Callable[[Optional[str]], Optional[str]] = git_head
) -> list[str]:
    """A moved git head invalidates a prior jude-verdict: pass."""
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
        if current and current != recorded:
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
        from hermes_cli.kanban_db import add_notify_sub

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

    return _McpRemoko()


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
    ensure_supervisor_tables(conn)
    obj = get_objective(conn, objective_id)
    if obj is None:
        return None
    external_id = f"obj-{objective_id}-{decision_key}"
    if obj.get("remoko_external_id") == external_id and obj.get("remoko_request_id"):
        return str(obj["remoko_request_id"])
    event_key = f"remoko:{objective_id}:{decision_key}"
    if not _record_supervisor_event(
        conn,
        event_key=event_key,
        kind="owner_blocker",
        task_id=task_id,
        objective_id=objective_id,
        payload={"external_id": external_id, "decision_key": decision_key},
    ) and obj.get("remoko_request_id"):
        return str(obj["remoko_request_id"])

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
    request_id = None
    if client is not None:
        try:
            result = client.request(payload)
            request_id = str(
                (result or {}).get("request_id")
                or (result or {}).get("id")
                or external_id
            )
        except Exception:
            logger.warning("remoko request failed for %s", external_id, exc_info=True)
            request_id = external_id
    else:
        request_id = external_id

    conn.execute(
        """
        UPDATE kanban_objectives
           SET remoko_request_id = ?, remoko_external_id = ?,
               status = 'blocked_owner', updated_at = ?
         WHERE id = ?
        """,
        (request_id, external_id, _now(), objective_id),
    )
    from hermes_cli.kanban_db import _append_event, block_task

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


def revalidate_owner_answer(
    conn: sqlite3.Connection,
    *,
    objective_id: str,
    answer: Any,
    expected_external_id: str,
    current_head: Optional[str] = None,
    expected_head: Optional[str] = None,
) -> bool:
    """Revalidate a Remoko answer against current repo/task state."""
    obj = get_objective(conn, objective_id)
    if obj is None:
        return False
    if obj.get("remoko_external_id") != expected_external_id:
        return False
    if expected_head and current_head and expected_head != current_head:
        return False
    if answer in {None, ""}:
        return False
    return True


def resume_after_owner_answer(
    conn: sqlite3.Connection,
    *,
    objective_id: str,
    task_id: str,
    answer: Any,
    expected_external_id: str,
    report_execution: Optional[Callable[..., Any]] = None,
) -> bool:
    if not revalidate_owner_answer(
        conn,
        objective_id=objective_id,
        answer=answer,
        expected_external_id=expected_external_id,
    ):
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
    if status not in {"done", "failed"}:
        return False
    predicate = unit.get("terminal_predicate") or ""
    if predicate == "jude_verdict_pass":
        proof = {}
        if unit.get("proof"):
            try:
                proof = json.loads(unit["proof"])
            except Exception:
                proof = {}
        recorded = (proof or {}).get("head")
        if recorded:
            current = _task_git_head(conn, unit["ref"])
            if current and current != recorded:
                return False
        return status == "done"
    if predicate == "task_done_with_proof":
        if unit.get("kind") == "kanban":
            task = conn.execute(
                "SELECT status FROM tasks WHERE id = ?",
                (unit["ref"],),
            ).fetchone()
            if task is None or task["status"] not in {"done", "archived"}:
                return False
        return status == "done"
    return status in {"done", "failed"}


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


def supervise_once(
    conn: sqlite3.Connection,
    *,
    remoko: Optional[RemokoClient] = None,
    dry_run: bool = False,
) -> SupervisorResult:
    """One supervisor tick: reconcile ledger, act on starvation, complete."""
    result = SupervisorResult()
    if dry_run:
        return result
    ensure_supervisor_tables(conn)
    result.invalidated_reviews = invalidate_stale_reviews(conn)
    if _table_exists(conn, "kanban_objectives"):
        for row in conn.execute(
            "SELECT id FROM kanban_objectives WHERE status != 'done'"
        ).fetchall():
            status = reconcile_objective(conn, row["id"])
            if status == "done":
                result.completed_objectives.append(row["id"])
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
            if _supervisor_event_seen(conn, handled_key):
                continue
            action = handle_starvation(
                conn,
                row["task_id"],
                str(payload.get("reason") or "active_pr"),
                remoko=remoko,
            )
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
