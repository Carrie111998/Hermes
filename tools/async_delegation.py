#!/usr/bin/env python3
"""
Async (background) delegation registry.

Backs ``delegate_task(background=true)``: the parent agent dispatches a
subagent that runs on a module-level daemon executor and returns a handle
immediately, so the user and the model can keep working while the child runs.

When the child finishes, a completion event is pushed onto the SHARED
``process_registry.completion_queue`` with ``type="async_delegation"``. The
CLI (``cli.py`` process_loop) and gateway (``_run_process_watcher`` /
``completion_queue`` drain) already poll that queue while the agent is idle
and forge a fresh user/internal turn from each event. We deliberately reuse
that rail rather than reaching into a running agent loop:

  - completions surface as a NEW turn when the agent is idle, never spliced
    between a tool result and an assistant message. That keeps strict
    message-role alternation legal and the prompt cache intact (hard
    invariant: never mutate past context).
  - we inherit the queue's de-dup, crash-recovery checkpoint, and the
    existing CLI + gateway drain wiring for free — no new drain loops in the
    two largest files in the repo.

The completion payload carries a RICH, self-contained task-source block (the
original goal, the context the parent supplied, toolsets, model, dispatch
time, status, and the full result summary). When the result re-enters the
conversation the parent may be deep in unrelated context and won't remember
why the subagent existed; the block lets it either use the result or
re-dispatch if the world has moved on.

This module owns ONLY the async lifecycle. The actual child build + run is
delegated back to ``delegate_tool._run_single_child`` via an injected
runner, so all the credential leasing, heartbeat, timeout, and result-shaping
logic stays in one place.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Optional

from hermes_constants import get_hermes_home
from tools.daemon_pool import DaemonThreadPoolExecutor
from tools.thread_context import propagate_context_to_thread

logger = logging.getLogger(__name__)

# Back-compat alias — the daemon executor now lives in tools.daemon_pool so
# other subsystems (tool_executor, memory_manager, delegate_tool, skills_hub)
# can share it. Existing imports of ``_DaemonThreadPoolExecutor`` keep working.
_DaemonThreadPoolExecutor = DaemonThreadPoolExecutor


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------
# A persistent daemon executor (NOT a `with ThreadPoolExecutor()` block, which
# would join on exit and defeat the whole point of async). Workers are daemon
# threads so a hard process exit doesn't hang on an in-flight child.
_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()
_executor_max_workers: int = 0

_records_lock = threading.Lock()
# delegation_id -> record dict. Kept for the lifetime of the run plus a short
# tail after completion so `list_async_delegations()` can show recent results.
_records: Dict[str, Dict[str, Any]] = {}
_aggregate_enqueue_lock = threading.Lock()
_aggregate_enqueued_delivery_ids: set[tuple[str, str]] = set()

_DEFAULT_MAX_ASYNC_CHILDREN = 3
# How many completed records to retain for status queries before pruning.
_MAX_RETAINED_COMPLETED = 50
_DURABLE_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MAX_DURABLE_PENDING = 1000
# A pending completion whose delivery keeps failing is retried across claim
# cycles (and across restarts via restore_undelivered_completions). Cap the
# attempts so an unroutable row converges to a terminal 'dropped' state
# instead of replaying on every restart forever.
_MAX_DELIVERY_ATTEMPTS = 8
# Staleness cap for restart replay: a pending completion older than this is
# terminally dropped instead of re-run as a fresh full-context turn (see
# restore_undelivered_completions). 48h keeps overnight/weekend results
# deliverable while stopping weeks-old sessions from replaying after upgrades.
_MAX_COMPLETION_REPLAY_AGE_S = 48 * 3600.0
_DB_LOCK = threading.Lock()
_DEFAULT_AGGREGATE_CHAR_BUDGET = 48_000
_MIN_AGGREGATE_CHAR_BUDGET = 1_024
_MAX_AGGREGATE_CHAR_BUDGET = 96_000
_GROUP_CLAIM_STALE_SECONDS = 300.0
_GROUP_DETAIL_RETENTION_SECONDS = 48 * 3600.0
_MAX_GROUP_DETAIL_BYTES = 64_000
_MAX_WORK_ID_BYTES = 256
_MAX_DELEGATION_ID_BYTES = 256
_MAX_OWNER_TURN_ID_BYTES = 256
_MAX_OUTCOME_COUNT = 999_999_999

# ---------------------------------------------------------------------------
# Stale-delegation detection (progress-based, on by default)
# ---------------------------------------------------------------------------
# A detached runner that wedges before returning (e.g. stuck inside its first
# model API call — #60203) never reaches its ``finally`` finalizer, so no
# completion event is ever published: the delegation shows "dispatched"
# forever and the owning session looks silent until a process restart. We do
# NOT fix this with a wall-clock timeout — legitimate heavy subagent work
# (deep reviews, research fan-outs, slow reasoning models) must never be
# killed for taking long (see delegate_tool.DEFAULT_CHILD_TIMEOUT rationale).
# Instead a single monitor thread watches per-dispatch PROGRESS (api-call
# count + current tool, via an injected ``progress_fn``): a child that is
# advancing is left alone forever; a child with NO progress past the stale
# threshold is interrupted, given a grace window to unwind and deliver its
# partial results through the normal finalize path, and only force-finalized
# with a terminal ``stalled`` event if it never returns.
#
# Thresholds mirror the sync-path heartbeat staleness monitor in
# delegate_tool: idle (not inside a tool) stays tight so a wedged first API
# call is caught quickly; in-tool is much higher so legitimately slow tools
# (long terminal commands, big fetches) get time to finish.
_STALE_CHECK_INTERVAL = 30.0  # seconds between monitor sweeps
_STALE_IDLE_SECONDS = 450.0  # no progress, no current tool → stalled
_STALE_IN_TOOL_SECONDS = 1200.0  # no progress while inside a tool → stalled
_STALL_GRACE_SECONDS = 120.0  # after interrupt, time for the runner to return

_monitor_lock = threading.Lock()
_monitor_thread: Optional[threading.Thread] = None
_monitor_stop = threading.Event()


def _db_path():
    return get_hermes_home() / "state.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    try:
        _initialize_schema(conn)
    except Exception:
        # A PRAGMA/DDL failure after a successful connect() must not leak the
        # just-opened connection back to the caller.
        conn.close()
        raise
    return conn


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from hermes_state import apply_durability_barriers

    # state.db's owning SessionDB connection establishes the configured journal
    # mode. This secondary durability ledger must preserve that mode: applying
    # WAL here on every short-lived connection requires an exclusive lock when
    # the file is not already WAL and can collide with live transcript/FTS
    # writers. The ledger works in either WAL or DELETE mode; if it opens a new
    # file first, the default rollback journal remains valid until SessionDB
    # establishes the configured mode. sqlite3.connect(timeout=10) above also
    # gives its small transactions a busy handler for ordinary contention.
    apply_durability_barriers(conn)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS async_delegations (
            delegation_id TEXT PRIMARY KEY,
            origin_session TEXT NOT NULL,
            origin_ui_session_id TEXT NOT NULL DEFAULT '',
            parent_session_id TEXT,
            state TEXT NOT NULL,
            dispatched_at REAL NOT NULL,
            completed_at REAL,
            updated_at REAL NOT NULL,
            event_json TEXT,
            result_json TEXT,
            delivery_state TEXT NOT NULL DEFAULT 'pending',
            delivery_attempts INTEGER NOT NULL DEFAULT 0,
            delivered_at REAL,
            owner_pid INTEGER,
            owner_started_at INTEGER,
            task_json TEXT,
            delivery_claim TEXT,
            delivery_claimed_at REAL,
            origin_session_id TEXT NOT NULL DEFAULT '',
            origin_work_id TEXT NOT NULL DEFAULT '',
            work_generation INTEGER NOT NULL DEFAULT 0
        )"""
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(async_delegations)")}
    for name, sql_type in (
        ("owner_pid", "INTEGER"),
        ("owner_started_at", "INTEGER"),
        ("task_json", "TEXT"),
        ("delivery_claim", "TEXT"),
        ("delivery_claimed_at", "REAL"),
        # Raw api_server session id (X-Hermes-Session-Id) of the ORIGINATING
        # request — the wake self-post target. Without persisting it,
        # completions recovered after a process restart are unroutable on
        # api_server (the in-memory record that carried it is gone).
        ("origin_session_id", "TEXT"),
        ("origin_work_id", "TEXT NOT NULL DEFAULT ''"),
        ("work_generation", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE async_delegations ADD COLUMN {name} {sql_type}")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS async_delegation_work_groups (
            work_id TEXT PRIMARY KEY,
            origin_session TEXT NOT NULL DEFAULT '',
            origin_ui_session_id TEXT NOT NULL DEFAULT '',
            origin_session_id TEXT NOT NULL DEFAULT '',
            parent_session_id TEXT,
            routing_json TEXT NOT NULL DEFAULT '{}',
            owner_turn_id TEXT NOT NULL,
            owner_pid INTEGER,
            owner_started_at INTEGER,
            state TEXT NOT NULL CHECK (state IN ('open','sealed','closing','closed')),
            generation INTEGER NOT NULL DEFAULT 0,
            aggregate_char_budget INTEGER NOT NULL DEFAULT 0,
            closeout_delivery_id TEXT,
            closeout_payload_json TEXT,
            closeout_claim TEXT,
            closeout_claimed_at REAL,
            closeout_turn_id TEXT,
            closeout_owner_pid INTEGER,
            closeout_owner_started_at INTEGER,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            sealed_at REAL,
            closed_at REAL,
            terminal_disposition TEXT,
            terminal_diagnostics TEXT
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_async_delegations_work "
        "ON async_delegations(origin_work_id, work_generation, dispatched_at)"
    )
    group_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(async_delegation_work_groups)")
    }
    for name, sql_type in (
        ("closeout_owner_pid", "INTEGER"),
        ("closeout_owner_started_at", "INTEGER"),
    ):
        if name not in group_columns:
            conn.execute(
                f"ALTER TABLE async_delegation_work_groups ADD COLUMN {name} {sql_type}"
            )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_async_work_groups_state "
        "ON async_delegation_work_groups(state, updated_at)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_async_work_groups_delivery "
        "ON async_delegation_work_groups(closeout_delivery_id) "
        "WHERE closeout_delivery_id IS NOT NULL"
    )


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    """Open a connection, commit/rollback on exit, and ALWAYS close it.

    ``sqlite3.Connection.__enter__``/``__exit__`` only commit or roll back the
    transaction; they do not close the connection. Using ``with _connect()``
    alone therefore leaks a connection — and its WAL/SHM file descriptors — on
    every durable dispatch, completion, and delivery-claim, deferring the close
    to the garbage collector. On a long-running gateway that exhausts
    ``RLIMIT_NOFILE`` (the cron-ledger sibling of this bug was #69567 / PR #69594).
    """
    conn = _connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Task-scoped closeout ledger (creation-gated; not surface-integrated yet)
# ---------------------------------------------------------------------------

def task_scoped_closeout_enabled(config: Optional[Dict[str, Any]] = None) -> bool:
    """Resolve the creation-only gate, defaulting closed on every failure.

    Callers recovering or draining an existing group must not consult this
    resolver.  The gate controls only creation of the first generation.
    """
    if config is None:
        try:
            from hermes_cli.config import load_config

            config = load_config()
        except Exception:  # noqa: BLE001 - a safety gate fails closed
            return False
    delegation = config.get("delegation") if isinstance(config, dict) else None
    return bool(
        isinstance(delegation, dict)
        and delegation.get("task_scoped_closeout", False) is True
    )


def _process_identity() -> tuple[int, Optional[int]]:
    try:
        from gateway.status import get_process_start_time

        return os.getpid(), get_process_start_time(os.getpid())
    except Exception:  # noqa: BLE001 - PID remains a useful weaker identity
        return os.getpid(), None


def _process_identity_is_live(pid: Any, started_at: Any) -> bool:
    if not pid or started_at is None:
        return False
    try:
        from gateway.status import _pid_exists, get_process_start_time

        if not _pid_exists(int(pid)):
            return False
        return get_process_start_time(int(pid)) == int(started_at)
    except Exception:  # noqa: BLE001 - recovery must fail closed
        return False


def _bounded_budget(value: Optional[int]) -> int:
    if value is None:
        return _DEFAULT_AGGREGATE_CHAR_BUDGET
    return max(_MIN_AGGREGATE_CHAR_BUDGET, min(int(value), _MAX_AGGREGATE_CHAR_BUDGET))


def _delivery_id(work_id: str, generation: int) -> str:
    digest = hashlib.sha256(f"{work_id}\0{generation}".encode()).hexdigest()
    return f"delegation-closeout-{digest[:32]}"


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


_STATUS_ALIASES = {
    "complete": "completed", "completed": "completed", "success": "completed",
    "succeeded": "completed", "ok": "completed",
    "failed": "failed", "failure": "failed", "error": "failed",
    "cancelled": "cancelled", "canceled": "cancelled", "aborted": "cancelled",
    "timeout": "timeout", "timed_out": "timeout", "timed out": "timeout",
    "stalled": "stalled", "stale": "stalled", "hung": "stalled",
    "unknown": "unknown", "dropped": "dropped", "blocked": "blocked",
}


def _status_category(value: Any) -> str:
    """Map untrusted status text to one fixed, truthful outcome category."""
    normalized = str(value or "unknown").strip().lower().replace("-", "_")
    return _STATUS_ALIASES.get(normalized, "unknown")


def _bounded_count(value: Any) -> int:
    try:
        return max(0, min(int(value or 0), _MAX_OUTCOME_COUNT))
    except (TypeError, ValueError, OverflowError):
        return 0


def _mandatory_member(
    delegation_id: str, *, status: Any = "unknown", result: Optional[Dict[str, Any]] = None,
    event: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Canonical registration-tested fallback; contains no arbitrary detail text."""
    result, event = result or {}, event or {}
    errors = result.get("schema_errors", event.get("schema_errors"))
    error_count = len(errors) if isinstance(errors, list) else int(errors is not None)
    schema_valid = result.get(
        "schema_valid", result.get("valid", event.get("schema_valid", event.get("valid")))
    )
    schema_verdict = result.get(
        "schema_verdict", result.get("verdict", event.get("schema_verdict", event.get("verdict")))
    )
    category = _status_category(status)
    return {
        "delegation_id": delegation_id,
        "status": category,
        "detail_ref": f"async_delegation:{delegation_id}",
        "detail_truncated": True,
        "detail_lost": bool(result.get("detail_lost", event.get("detail_lost", False))),
        "error_present": result.get("error", event.get("error")) not in (None, ""),
        "diagnostic_present": result.get("diagnostic", event.get("diagnostic")) not in (None, ""),
        "schema_verdict_present": schema_verdict is not None,
        "schema_valid": None if schema_valid is None else bool(schema_valid),
        "schema_error_count": _bounded_count(error_count),
        "schema_retries": _bounded_count(result.get("schema_retries", event.get("schema_retries"))),
        "timed_out": category == "timeout" or any(
            result.get(key, event.get(key)) is not None
            for key in ("timeout_seconds", "timed_out_after_seconds")
        ),
        "stalled": category == "stalled" or any(
            result.get(key, event.get(key)) is not None
            for key in ("stalled_after_quiet_seconds", "stall_threshold_seconds")
        ),
    }


def _capacity_member(delegation_id: str) -> Dict[str, Any]:
    """True byte upper bound for the canonical mandatory member shape."""
    item = _mandatory_member(delegation_id, status="completed")
    item.update({
        # JSON ``false`` is one byte longer than ``true``.
        "detail_lost": False, "error_present": False, "diagnostic_present": False,
        "schema_verdict_present": False, "schema_valid": False,
        "schema_error_count": _MAX_OUTCOME_COUNT, "schema_retries": _MAX_OUTCOME_COUNT,
        "timed_out": False, "stalled": False,
    })
    return item


def _identifier_fits(value: Any, byte_limit: int) -> bool:
    return isinstance(value, str) and bool(value) and len(value.encode("utf-8")) <= byte_limit


def _minimal_envelope_size(work_id: str, generation: int, member_ids: List[str]) -> int:
    envelope = {
        "type": "async_delegation_work_closeout",
        "work_id": work_id,
        "generation": generation,
        "delivery_id": _delivery_id(work_id, generation),
        "members": [_capacity_member(member_id) for member_id in sorted(member_ids)],
    }
    return len(_json_bytes(envelope))


def _group_row(conn: sqlite3.Connection, work_id: str):
    return conn.execute(
        "SELECT * FROM async_delegation_work_groups WHERE work_id=?", (work_id,)
    ).fetchone()


def register_work_group_member(
    *,
    work_id: str,
    owner_turn_id: str,
    delegation_id: str,
    generation: int = 0,
    routing: Optional[Dict[str, Any]] = None,
    task: Optional[Dict[str, Any]] = None,
    dispatched_at: Optional[float] = None,
    aggregate_char_budget: Optional[int] = None,
    feature_config: Optional[Dict[str, Any]] = None,
) -> bool:
    """Atomically create an open group (if gated on) and register a member.

    Additional members require the exact open owner turn and generation.
    This direct ledger API is intentionally not wired into dispatch in Stage 1.
    """
    if not (
        _identifier_fits(work_id, _MAX_WORK_ID_BYTES)
        and _identifier_fits(owner_turn_id, _MAX_OWNER_TURN_ID_BYTES)
        and _identifier_fits(delegation_id, _MAX_DELEGATION_ID_BYTES)
    ):
        return False
    routing = dict(routing or {})
    task = dict(task or {})
    now = time.time()
    dispatched = float(dispatched_at or now)
    pid, started = _process_identity()
    with _DB_LOCK, _transaction() as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        group = _group_row(conn, work_id)
        created_group = False
        if group is None:
            if generation != 0 or not task_scoped_closeout_enabled(feature_config):
                return False
            conn.execute(
                """INSERT INTO async_delegation_work_groups
                   (work_id, origin_session, origin_ui_session_id,
                    origin_session_id, parent_session_id, routing_json,
                    owner_turn_id, owner_pid, owner_started_at, state,
                    generation, aggregate_char_budget, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', 0, ?, ?, ?)""",
                (
                    work_id, routing.get("origin_session", ""),
                    routing.get("origin_ui_session_id", ""),
                    routing.get("origin_session_id", ""),
                    routing.get("parent_session_id"),
                    json.dumps(routing, sort_keys=True, separators=(",", ":")),
                    owner_turn_id, pid, started,
                    _bounded_budget(aggregate_char_budget), now, now,
                ),
            )
            created_group = True
        elif not (
            group["state"] == "open"
            and group["owner_turn_id"] == owner_turn_id
            and int(group["generation"]) == generation
        ):
            return False
        budget = _bounded_budget(
            aggregate_char_budget if group is None else group["aggregate_char_budget"]
        )
        existing_ids = [
            str(row[0])
            for row in conn.execute(
                "SELECT delegation_id FROM async_delegations "
                "WHERE origin_work_id=? AND work_generation=?",
                (work_id, generation),
            ).fetchall()
        ]
        if _minimal_envelope_size(
            work_id, generation, existing_ids + [delegation_id]
        ) > budget:
            if created_group:
                conn.execute(
                    "DELETE FROM async_delegation_work_groups WHERE work_id=?",
                    (work_id,),
                )
            return False
        task_json = json.dumps(task, sort_keys=True, separators=(",", ":"))
        cur = conn.execute(
            """INSERT OR IGNORE INTO async_delegations
               (delegation_id, origin_session, origin_ui_session_id,
                parent_session_id, state, dispatched_at, updated_at,
                delivery_state, delivery_attempts, owner_pid,
                owner_started_at, task_json, origin_session_id,
                origin_work_id, work_generation)
               VALUES (?, ?, ?, ?, 'running', ?, ?, 'pending', 0, ?, ?, ?, ?, ?, ?)""",
            (
                delegation_id, routing.get("origin_session", ""),
                routing.get("origin_ui_session_id", ""),
                routing.get("parent_session_id"), dispatched, now, pid, started,
                task_json, routing.get("origin_session_id", ""), work_id, generation,
            ),
        )
        if cur.rowcount:
            conn.execute(
                "UPDATE async_delegation_work_groups SET updated_at=? WHERE work_id=?",
                (now, work_id),
            )
        elif created_group:
            conn.execute(
                "DELETE FROM async_delegation_work_groups WHERE work_id=?",
                (work_id,),
            )
        return cur.rowcount == 1


def _unregister_unsubmitted_work_group_member(delegation_id: str) -> None:
    """Undo registration when executor submission never accepted the child."""
    with _DB_LOCK, _transaction() as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT origin_work_id, work_generation FROM async_delegations "
            "WHERE delegation_id=? AND state='running'",
            (delegation_id,),
        ).fetchone()
        if row is None:
            return
        work_id = str(row["origin_work_id"] or "")
        generation = int(row["work_generation"] or 0)
        conn.execute("DELETE FROM async_delegations WHERE delegation_id=?", (delegation_id,))
        if work_id:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM async_delegations WHERE origin_work_id=? "
                "AND work_generation=?",
                (work_id, generation),
            ).fetchone()[0]
            if not remaining:
                conn.execute(
                    "DELETE FROM async_delegation_work_groups WHERE work_id=? "
                    "AND state='open' AND generation=?",
                    (work_id, generation),
                )


def seal_work_group(work_id: str, owner_turn_id: str) -> bool:
    """Seal membership only for the exact owning turn."""
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        cur = conn.execute(
            """UPDATE async_delegation_work_groups
               SET state='sealed', sealed_at=?, updated_at=?
               WHERE work_id=? AND state='open' AND owner_turn_id=?""",
            (now, now, work_id, owner_turn_id),
        )
        return cur.rowcount == 1


def seal_work_group_result(
    work_id: str, owner_turn_id: str, *, diagnostics: Optional[str] = None
) -> Dict[str, Any]:
    """Seal an owning turn and return an explicit, fail-closed diagnosis.

    The boolean helper remains for compatibility, but turn finalization must
    distinguish an idempotent already-sealed group from a missing group or an
    owner mismatch.  Otherwise a bad identity silently becomes an eternal
    "waiting" turn.
    """
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        row = _group_row(conn, work_id)
        if row is None:
            return {"ok": False, "code": "work_group_missing", "work_id": work_id}
        if row["state"] != "open":
            if row["state"] in {"sealed", "closing", "closed"}:
                return {"ok": True, "code": "already_sealed", "state": row["state"]}
            return {"ok": False, "code": "invalid_group_state", "state": row["state"]}
        if str(row["owner_turn_id"] or "") != str(owner_turn_id or ""):
            return {
                "ok": False,
                "code": "owner_turn_mismatch",
                "expected_owner_turn_id": str(row["owner_turn_id"] or ""),
                "actual_owner_turn_id": str(owner_turn_id or ""),
            }
        cur = conn.execute(
            """UPDATE async_delegation_work_groups
               SET state='sealed', sealed_at=?, updated_at=?,
                   terminal_diagnostics=COALESCE(?, terminal_diagnostics)
               WHERE work_id=? AND state='open' AND owner_turn_id=?""",
            (now, now, diagnostics, work_id, owner_turn_id),
        )
        if cur.rowcount != 1:
            return {"ok": False, "code": "seal_cas_failed"}
        return {"ok": True, "code": "sealed", "state": "sealed"}


def persist_group_member_completion(
    delegation_id: str, event: Dict[str, Any], result: Dict[str, Any]
) -> bool:
    """Persist a terminal member and report duplicate-safe sealed readiness.

    Stage 1 deliberately does not publish or suppress the legacy completion
    event. Integration will choose which persistence helper to call later.
    """
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        member = conn.execute(
            "SELECT origin_work_id, work_generation FROM async_delegations "
            "WHERE delegation_id=?",
            (delegation_id,),
        ).fetchone()
        if member is None or not member["origin_work_id"]:
            return False
        conn.execute(
            """UPDATE async_delegations SET state=?, completed_at=?, updated_at=?,
               event_json=?, result_json=?, delivery_state='pending'
               WHERE delegation_id=?""",
            (
                event.get("status", result.get("status", "completed")),
                event.get("completed_at", now), now,
                json.dumps(event, sort_keys=True), json.dumps(result, sort_keys=True),
                delegation_id,
            ),
        )
        return _group_ready(conn, member["origin_work_id"], int(member["work_generation"]))


def _group_ready(conn: sqlite3.Connection, work_id: str, generation: int) -> bool:
    group = conn.execute(
        "SELECT state, generation, terminal_diagnostics FROM async_delegation_work_groups "
        "WHERE work_id=?",
        (work_id,),
    ).fetchone()
    if group is None or group[0] != "sealed" or int(group[1]) != generation:
        return False
    counts = conn.execute(
        """SELECT COUNT(*),
                  SUM(CASE WHEN state IN ('running','finalizing') THEN 1 ELSE 0 END),
                  SUM(CASE WHEN state NOT IN ('running','finalizing')
                            AND event_json IS NULL AND result_json IS NULL THEN 1 ELSE 0 END)
           FROM async_delegations WHERE origin_work_id=? AND work_generation=?""",
        (work_id, generation),
    ).fetchone()
    total, live, missing = (int(counts[0]), int(counts[1] or 0), int(counts[2] or 0))
    return total > 0 and live == 0 and (missing == 0 or bool(group[2]))


def group_is_ready(work_id: str) -> bool:
    with _DB_LOCK, _transaction() as conn:
        row = conn.execute(
            "SELECT generation FROM async_delegation_work_groups WHERE work_id=?",
            (work_id,),
        ).fetchone()
        return bool(row and _group_ready(conn, work_id, int(row[0])))


_PRESERVED_RESULT_KEYS = (
    "status", "timeout_seconds", "timed_out_after_seconds",
    "timeout_phase", "stalled_after_quiet_seconds", "stall_threshold_seconds",
    "stall_phase", "stall_grace_seconds", "exit_reason", "schema_valid",
    "schema_retries", "detail_lost", "diagnostic", "api_calls",
    "duration_seconds", "model",
)


def _compact_scalar(value: Any, limit: int = 160) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (dict, list, tuple)):
        text = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
    else:
        text = str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _schema_metadata(result: Dict[str, Any], event: Dict[str, Any]) -> Dict[str, Any]:
    verdict = result.get(
        "schema_verdict",
        result.get("verdict", event.get("schema_verdict", event.get("verdict"))),
    )
    valid = result.get(
        "schema_valid",
        result.get("valid", event.get("schema_valid", event.get("valid"))),
    )
    errors = result.get("schema_errors", event.get("schema_errors"))
    metadata: Dict[str, Any] = {}
    if valid is not None:
        metadata["schema_valid"] = bool(valid)
    if errors is not None:
        metadata["schema_error_count"] = len(errors) if isinstance(errors, list) else 1
    if verdict is not None:
        if isinstance(verdict, dict):
            compact = {}
            for key in ("valid", "status", "verdict", "reason", "error_count", "retries"):
                if key in verdict:
                    compact[key] = _compact_scalar(verdict[key], 80)
            metadata["schema_verdict"] = compact or "detail_truncated"
        else:
            metadata["schema_verdict"] = _compact_scalar(verdict, 80)
    return metadata


def _build_group_envelope(
    work_id: str, generation: int, delivery_id: str, budget: int, rows: List[sqlite3.Row]
) -> Dict[str, Any]:
    members: List[Dict[str, Any]] = []
    mandatory_by_id: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        task = json.loads(row["task_json"] or "{}")
        event = json.loads(row["event_json"] or "{}")
        result = json.loads(row["result_json"] or "{}")
        status = result.get("status", event.get("status", row["state"] or "unknown"))
        delegation_id = str(row["delegation_id"])
        item = _mandatory_member(
            delegation_id, status=status, result=result, event=event
        )
        mandatory_by_id[delegation_id] = dict(item)
        item["detail_truncated"] = False
        item["dispatch_index"] = int(task.get("dispatch_index", task.get("task_index", 0)) or 0)
        item["task_index"] = int(task.get("task_index", 0) or 0)
        for key in _PRESERVED_RESULT_KEYS:
            if key in {"status", "detail_lost"}:
                continue
            if key in result:
                item[key] = _compact_scalar(result[key])
            elif key in event:
                item[key] = _compact_scalar(event[key])
        item.update(_schema_metadata(result, event))
        errors = result.get("schema_errors", event.get("schema_errors"))
        if errors is not None and "schema_error_count" not in item:
            item["schema_error_count"] = len(errors) if isinstance(errors, list) else 1
        for key, value in (
            ("goal", task.get("goal", event.get("goal"))),
            ("error", result.get("error", event.get("error"))),
        ):
            if value not in (None, ""):
                item[key] = _compact_scalar(value, 240)
        summary = result.get("summary", event.get("summary"))
        if summary is not None:
            item["summary"] = _compact_scalar(summary, 480)
        members.append(item)
    members.sort(key=lambda item: (item["dispatch_index"], item["task_index"], item["delegation_id"]))
    envelope = {
        "type": "async_delegation_work_closeout",
        "work_id": work_id,
        "generation": generation,
        "delivery_id": delivery_id,
        "members": members,
    }
    # Fail down deterministically to the registration-tested mandatory form.
    if len(_json_bytes(envelope)) > budget:
        optional_order = (
            "summary", "goal", "error", "schema_verdict", "model",
            "duration_seconds", "api_calls", "exit_reason", "timeout_phase",
            "stall_phase", "dispatch_index", "task_index",
        )
        for key in optional_order:
            for item in reversed(members):
                item.pop(key, None)
            if len(_json_bytes(envelope)) <= budget:
                break
    if len(_json_bytes(envelope)) > budget:
        # This is the exact representation registration sized. Rebuild it rather
        # than pruning rich data in place, so no untrusted scalar can leak into
        # the mandatory envelope.
        envelope["members"] = [
            mandatory_by_id[item["delegation_id"]] for item in members
        ]
    if len(_json_bytes(envelope)) > budget:
        # Only corrupt/legacy rows can reach this state: admitted Stage-1 rows
        # were checked against the shape above. Return an honest bounded marker
        # instead of crashing the claimant or emitting oversized bytes.
        envelope = {
            "type": "async_delegation_work_closeout_tombstone",
            "work_ref": hashlib.sha256(work_id.encode("utf-8")).hexdigest()[:32],
            "generation": generation,
            "delivery_id": delivery_id,
            "member_count": len(rows),
            "member_identities_lost": True,
            "detail_lost": True,
        }
    return envelope


def claim_ready_work_group(work_id: str, consumer: str) -> Optional[Dict[str, Any]]:
    """Atomically claim one sealed-ready generation and persist its envelope."""
    now = time.time()
    claim = f"{consumer}:{os.getpid()}:{uuid.uuid4().hex}"
    claim_pid, claim_started = _process_identity()
    with _DB_LOCK, _transaction() as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        group = _group_row(conn, work_id)
        if group is None or not _group_ready(conn, work_id, int(group["generation"])):
            return None
        generation = int(group["generation"])
        rows = conn.execute(
            """SELECT * FROM async_delegations
               WHERE origin_work_id=? AND work_generation=?
               ORDER BY dispatched_at, delegation_id""",
            (work_id, generation),
        ).fetchall()
        delivery_id = _delivery_id(work_id, generation)
        envelope = _build_group_envelope(
            work_id, generation, delivery_id,
            _bounded_budget(group["aggregate_char_budget"]), rows,
        )
        payload_bytes = _json_bytes(envelope)
        if len(payload_bytes) > _bounded_budget(group["aggregate_char_budget"]):
            return None
        payload = payload_bytes.decode("utf-8")
        cur = conn.execute(
            """UPDATE async_delegation_work_groups
               SET state='closing', closeout_delivery_id=?,
                   closeout_payload_json=?, closeout_claim=?,
                   closeout_claimed_at=?, closeout_owner_pid=?,
                   closeout_owner_started_at=?, updated_at=?
               WHERE work_id=? AND state='sealed' AND generation=?
                 AND closeout_payload_json IS NULL""",
            (
                delivery_id, payload, claim, now, claim_pid, claim_started,
                now, work_id, generation,
            ),
        )
        if cur.rowcount != 1:
            return None
        return {
            "envelope": envelope,
            "claim_id": claim,
            "routing": {
                "session_key": group["origin_session"],
                "origin_ui_session_id": group["origin_ui_session_id"],
                "origin_session_id": group["origin_session_id"],
                "parent_session_id": group["parent_session_id"],
            },
        }


def _aggregate_ready_event(claimed: Dict[str, Any]) -> Dict[str, Any]:
    """Build the one typed queue item used by live and recovery producers."""
    envelope = dict(claimed["envelope"])
    routing = dict(claimed.get("routing") or {})
    return {
        "type": "async_delegation_work_closeout",
        "delivery_id": envelope["delivery_id"],
        "origin_work_id": envelope["work_id"],
        "work_generation": envelope["generation"],
        "claim_id": claimed["claim_id"],
        "envelope": envelope,
        # Internal-only provenance for profile-scoped ledger mutations.  This
        # is injected by the ledger itself from the active runtime scope, never
        # accepted from delegation/user payloads.
        "_ledger_profile_home": str(get_hermes_home().resolve()),
        **routing,
    }


def _enqueue_claimed_work_group(
    claimed: Dict[str, Any], *, target_queue: Any = None
) -> Optional[Dict[str, Any]]:
    """Publish a claimed durable envelope; release ownership on queue failure."""
    event = _aggregate_ready_event(claimed)
    delivery_id = event["delivery_id"]
    delivery_key = (str(event["_ledger_profile_home"]), str(delivery_id))
    with _aggregate_enqueue_lock:
        if delivery_key in _aggregate_enqueued_delivery_ids:
            return None
        _aggregate_enqueued_delivery_ids.add(delivery_key)
    try:
        if target_queue is None:
            from tools.process_registry import process_registry

            target_queue = process_registry.completion_queue
        target_queue.put(event)
    except Exception:
        with _aggregate_enqueue_lock:
            _aggregate_enqueued_delivery_ids.discard(delivery_key)
        release_work_group_claim(event["origin_work_id"], event["claim_id"])
        raise
    return event


def claim_and_enqueue_ready_work_group(
    work_id: str, *, consumer: str = "async-delegation-producer"
) -> Optional[Dict[str, Any]]:
    """Atomically claim a sealed-ready group and enqueue its aggregate once."""
    claimed = claim_ready_work_group(work_id, consumer)
    return _enqueue_claimed_work_group(claimed) if claimed is not None else None


def seal_and_enqueue_work_group(
    work_id: str, owner_turn_id: str, *, consumer: str = "turn-seal",
    diagnostics: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Later turn gates call this to seal membership and publish if ready.

    Duplicate seal callers are harmless: a group that is already sealed may
    still be claimed, while a closing group has already won that atomic claim.
    """
    sealed = seal_work_group_result(
        work_id, owner_turn_id, diagnostics=diagnostics
    )
    if not sealed["ok"]:
        return {"type": "async_delegation_work_seal_error", **sealed}
    return claim_and_enqueue_ready_work_group(work_id, consumer=consumer)


def release_work_group_claim(work_id: str, claim_id: str) -> bool:
    """Release scheduling ownership without deleting the durable envelope."""
    with _DB_LOCK, _transaction() as conn:
        cur = conn.execute(
            """UPDATE async_delegation_work_groups
               SET closeout_claim=NULL, closeout_claimed_at=NULL,
                   closeout_owner_pid=NULL, closeout_owner_started_at=NULL,
                   updated_at=?
               WHERE work_id=? AND state='closing' AND closeout_claim=?
                 AND closeout_turn_id IS NULL""",
            (time.time(), work_id, claim_id),
        )
        return cur.rowcount == 1


def release_enqueued_work_group_event(event: Dict[str, Any]) -> bool:
    """Release a failed aggregate injection so the same envelope can retry.

    The gateway calls this only after the synthetic turn unwinds. Clearing the
    exact bound turn prevents a transient failure from stranding a closing
    group while the gateway PID remains healthy.
    """
    work_id = str(event.get("origin_work_id") or "")
    delivery_id = str(event.get("delivery_id") or "")
    claim_id = str(event.get("claim_id") or "")
    if not (work_id and delivery_id and claim_id):
        return False
    delivery_key = (
        str(event.get("_ledger_profile_home") or get_hermes_home().resolve()),
        delivery_id,
    )
    with _aggregate_enqueue_lock:
        _aggregate_enqueued_delivery_ids.discard(delivery_key)
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            """UPDATE async_delegation_work_groups
               SET closeout_turn_id=NULL, updated_at=?
               WHERE work_id=? AND state='closing'
                 AND closeout_delivery_id=? AND closeout_claim=?""",
            (now, work_id, delivery_id, claim_id),
        )
    return release_work_group_claim(work_id, claim_id)


def reclaim_stale_work_group_claim(work_id: str, consumer: str) -> Optional[Dict[str, Any]]:
    """Take a stale/unowned closing claim while retaining the same envelope."""
    now = time.time()
    claim = f"{consumer}:{os.getpid()}:{uuid.uuid4().hex}"
    claim_pid, claim_started = _process_identity()
    with _DB_LOCK, _transaction() as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        row = _group_row(conn, work_id)
        if row is None or row["state"] != "closing" or not row["closeout_payload_json"]:
            return None
        claimed_at = row["closeout_claimed_at"]
        bound_live = bool(
            row["closeout_turn_id"]
            and _process_identity_is_live(
                row["closeout_owner_pid"], row["closeout_owner_started_at"]
            )
        )
        if bound_live:
            return None
        was_bound = bool(row["closeout_turn_id"])
        unbound_owner_known = row["closeout_owner_pid"] is not None
        unbound_owner_live = bool(
            unbound_owner_known
            and _process_identity_is_live(
                row["closeout_owner_pid"], row["closeout_owner_started_at"]
            )
        )
        if not was_bound and row["closeout_claim"]:
            if unbound_owner_live:
                return None
            if (
                not unbound_owner_known
                and claimed_at
                and claimed_at >= now - _GROUP_CLAIM_STALE_SECONDS
            ):
                return None
        old_claim = row["closeout_claim"]
        old_turn = row["closeout_turn_id"]
        old_delivery = row["closeout_delivery_id"]
        old_generation = int(row["generation"])
        old_owner_pid = row["closeout_owner_pid"]
        old_owner_started = row["closeout_owner_started_at"]
        cur = conn.execute(
            """UPDATE async_delegation_work_groups
               SET closeout_claim=?, closeout_claimed_at=?, closeout_turn_id=NULL,
                   closeout_owner_pid=?, closeout_owner_started_at=?, updated_at=?
               WHERE work_id=? AND state='closing'
                 AND generation=? AND closeout_delivery_id IS ?
                 AND closeout_claim IS ? AND closeout_turn_id IS ?
                 AND closeout_owner_pid IS ?
                 AND closeout_owner_started_at IS ?""",
            (
                claim, now, claim_pid, claim_started, now,
                work_id, old_generation, old_delivery, old_claim, old_turn,
                old_owner_pid, old_owner_started,
            ),
        )
        if cur.rowcount != 1:
            return None
        return {
            "envelope": json.loads(row["closeout_payload_json"]),
            "claim_id": claim,
            "routing": {
                "session_key": row["origin_session"],
                "origin_ui_session_id": row["origin_ui_session_id"],
                "origin_session_id": row["origin_session_id"],
                "parent_session_id": row["parent_session_id"],
            },
        }


def release_bound_work_group_closeout(
    work_id: str, generation: int, delivery_id: str, claim_id: str,
    closeout_turn_id: str,
) -> bool:
    """Release only the exact bound turn, retaining its durable envelope.

    A closeout turn calls this from its outer ``finally`` when it did not
    durably close the generation or reopen it with replacement work. Rotating
    away from the old claim fences the failed actor even though its process is
    still alive; recovery can then claim and enqueue the unchanged envelope.
    """
    pid, started = _process_identity()
    with _DB_LOCK, _transaction() as conn:
        cur = conn.execute(
            """UPDATE async_delegation_work_groups
               SET closeout_claim=NULL, closeout_claimed_at=NULL,
                   closeout_turn_id=NULL,
                   closeout_owner_pid=NULL, closeout_owner_started_at=NULL,
                   updated_at=?
               WHERE work_id=? AND state='closing' AND generation=?
                 AND closeout_delivery_id=? AND closeout_claim=?
                 AND closeout_turn_id=? AND closeout_owner_pid=?
                 AND (closeout_owner_started_at IS ?
                      OR closeout_owner_started_at=?)""",
            (
                time.time(), work_id, generation, delivery_id, claim_id,
                closeout_turn_id, pid, started, started,
            ),
        )
    if cur.rowcount == 1:
        with _aggregate_enqueue_lock:
            _aggregate_enqueued_delivery_ids.discard(
                (str(get_hermes_home().resolve()), delivery_id)
            )
        return True
    return False


def bind_work_group_closeout_turn(
    work_id: str, delivery_id: str, claim_id: str, closeout_turn_id: str
) -> bool:
    pid, started = _process_identity()
    with _DB_LOCK, _transaction() as conn:
        cur = conn.execute(
            """UPDATE async_delegation_work_groups SET closeout_turn_id=?,
                      closeout_owner_pid=?, closeout_owner_started_at=?, updated_at=?
               WHERE work_id=? AND state='closing' AND closeout_delivery_id=?
                 AND closeout_claim=? AND closeout_turn_id IS NULL""",
            (closeout_turn_id, pid, started, time.time(), work_id, delivery_id, claim_id),
        )
        return cur.rowcount == 1


def renew_work_group_claim(
    work_id: str, generation: int, delivery_id: str, claim_id: str,
    closeout_turn_id: str,
) -> bool:
    """Heartbeat only the exact live bound closeout identity."""
    pid, started = _process_identity()
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        cur = conn.execute(
            """UPDATE async_delegation_work_groups
               SET closeout_claimed_at=?, updated_at=?
               WHERE work_id=? AND state='closing' AND generation=?
                 AND closeout_delivery_id=? AND closeout_claim=?
                 AND closeout_turn_id=? AND closeout_owner_pid=?
                 AND (closeout_owner_started_at IS ? OR closeout_owner_started_at=?)""",
            (now, now, work_id, generation, delivery_id, claim_id,
             closeout_turn_id, pid, started, started),
        )
        return cur.rowcount == 1


def reopen_work_group_with_member(
    *, work_id: str, generation: int, delivery_id: str, claim_id: str,
    closeout_turn_id: str,
    delegation_id: str, task: Optional[Dict[str, Any]] = None,
    dispatched_at: Optional[float] = None,
) -> bool:
    """Reopen closing N as open N+1 with its first replacement atomically."""
    if not (
        _identifier_fits(work_id, _MAX_WORK_ID_BYTES)
        and _identifier_fits(closeout_turn_id, _MAX_OWNER_TURN_ID_BYTES)
        and _identifier_fits(delegation_id, _MAX_DELEGATION_ID_BYTES)
    ):
        return False
    now = time.time()
    pid, started = _process_identity()
    task_json = json.dumps(task or {}, sort_keys=True, separators=(",", ":"))
    with _DB_LOCK, _transaction() as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        group = _group_row(conn, work_id)
        if group is None or not (
            group["state"] == "closing"
            and int(group["generation"]) == generation
            and group["closeout_delivery_id"] == delivery_id
            and group["closeout_claim"] == claim_id
            and group["closeout_turn_id"] == closeout_turn_id
        ):
            return False
        if _minimal_envelope_size(
            work_id, generation + 1, [delegation_id]
        ) > _bounded_budget(group["aggregate_char_budget"]):
            return False
        cur = conn.execute(
            """INSERT OR IGNORE INTO async_delegations
               (delegation_id, origin_session, origin_ui_session_id,
                parent_session_id, state, dispatched_at, updated_at,
                delivery_state, owner_pid, owner_started_at, task_json,
                origin_session_id, origin_work_id, work_generation)
               VALUES (?, ?, ?, ?, 'running', ?, ?, 'pending', ?, ?, ?, ?, ?, ?)""",
            (
                delegation_id, group["origin_session"], group["origin_ui_session_id"],
                group["parent_session_id"], dispatched_at or now, now, pid, started,
                task_json, group["origin_session_id"], work_id, generation + 1,
            ),
        )
        if cur.rowcount != 1:
            return False
        conn.execute(
            """UPDATE async_delegations SET delivery_state='delivered',
                      delivered_at=?, updated_at=?
               WHERE origin_work_id=? AND work_generation=?""",
            (now, now, work_id, generation),
        )
        conn.execute(
            """UPDATE async_delegation_work_groups
               SET state='open', generation=?, owner_turn_id=?, owner_pid=?,
                   owner_started_at=?, closeout_delivery_id=NULL,
                   closeout_payload_json=NULL, closeout_claim=NULL,
                   closeout_claimed_at=NULL, closeout_turn_id=NULL,
                   closeout_owner_pid=NULL, closeout_owner_started_at=NULL,
                   sealed_at=NULL, updated_at=? WHERE work_id=?""",
            (generation + 1, closeout_turn_id, pid, started, now, work_id),
        )
    with _aggregate_enqueue_lock:
        _aggregate_enqueued_delivery_ids.discard(
            (str(get_hermes_home().resolve()), delivery_id)
        )
    return True


def close_work_group(
    work_id: str, generation: int, delivery_id: str, claim_id: str,
    closeout_turn_id: str,
    *, disposition: str = "success", diagnostics: Optional[str] = None,
) -> bool:
    """Commit closeout only after the caller confirms transcript persistence."""
    if disposition not in {"success", "blocked", "failed", "cancelled", "dropped"}:
        return False
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            """UPDATE async_delegation_work_groups
               SET state='closed', terminal_disposition=?, terminal_diagnostics=?,
                   closed_at=?, updated_at=?, closeout_claim=NULL,
                   closeout_claimed_at=NULL, closeout_owner_pid=NULL,
                   closeout_owner_started_at=NULL
               WHERE work_id=? AND state='closing' AND generation=?
                 AND closeout_delivery_id=? AND closeout_claim=?
                 AND closeout_turn_id=?""",
            (disposition, diagnostics, now, now, work_id, generation,
             delivery_id, claim_id, closeout_turn_id),
        )
        if cur.rowcount != 1:
            return False
        conn.execute(
            """UPDATE async_delegations SET delivery_state='delivered',
                      delivered_at=?, updated_at=?
               WHERE origin_work_id=? AND work_generation=?""",
            (now, now, work_id, generation),
        )
    with _aggregate_enqueue_lock:
        _aggregate_enqueued_delivery_ids.discard(
            (str(get_hermes_home().resolve()), delivery_id)
        )
    return True


def close_work_groups_for_session(
    *, origin_session: str = "", origin_ui_session_id: str = "",
    parent_session_id: str = "", disposition: str = "cancelled",
    diagnostics: str = "session boundary",
) -> int:
    """Apply an explicit terminal session-boundary disposition."""
    if disposition not in {"cancelled", "dropped"}:
        return 0
    clauses, values = [], []
    for column, value in (
        ("origin_session", origin_session),
        ("origin_ui_session_id", origin_ui_session_id),
        ("parent_session_id", parent_session_id),
    ):
        if value:
            clauses.append(f"{column}=?")
            values.append(value)
    if not clauses:
        return 0
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        cur = conn.execute(
            "UPDATE async_delegation_work_groups SET state='closed', "
            "terminal_disposition=?, terminal_diagnostics=?, closed_at=?, updated_at=?, "
            "closeout_claim=NULL, closeout_claimed_at=NULL, closeout_turn_id=NULL, "
            "closeout_owner_pid=NULL, closeout_owner_started_at=NULL "
            "WHERE work_id<>'' AND state IN ('open','sealed','closing') AND ("
            + " OR ".join(clauses) + ")",
            (disposition, diagnostics, now, now, *values),
        )
        return cur.rowcount


def _reveal_closed_closeout_provisionals(conn: sqlite3.Connection) -> int:
    """Reveal durable final rows left hidden by a crash after group close."""
    try:
        rows = conn.execute(
            "SELECT id, display_metadata FROM messages "
            "WHERE display_kind='delegation_closeout_provisional' ORDER BY id"
        ).fetchall()
    except sqlite3.OperationalError:
        # The standalone delegation ledger tests intentionally create no
        # transcript schema.
        return 0
    revealed = 0
    revealed_deliveries: set[tuple[str, str]] = set()
    for row in rows:
        try:
            metadata = json.loads(row["display_metadata"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(metadata, dict):
            continue
        work_id = str(metadata.get("work_id") or "")
        delivery_id = str(metadata.get("delivery_id") or "")
        if not (work_id and delivery_id):
            continue
        group = conn.execute(
            "SELECT state, closeout_delivery_id FROM async_delegation_work_groups "
            "WHERE work_id=?",
            (work_id,),
        ).fetchone()
        if (
            group is None
            or group["state"] != "closed"
            or str(group["closeout_delivery_id"] or "") != delivery_id
        ):
            continue
        identity = (work_id, delivery_id)
        if identity in revealed_deliveries:
            # Legacy crash/replay races may already have appended duplicates.
            # Keep only the oldest row canonical and presentation-hidden.
            continue
        cur = conn.execute(
            "UPDATE messages SET display_kind=NULL, display_metadata=NULL "
            "WHERE id=? AND display_kind='delegation_closeout_provisional'",
            (row["id"],),
        )
        revealed += cur.rowcount
        if cur.rowcount:
            revealed_deliveries.add(identity)
    return revealed


def find_closeout_provisional(
    work_id: str, delivery_id: str,
) -> Optional[Dict[str, Any]]:
    """Return the canonical durable provisional for one delivery identity."""
    if not (work_id and delivery_id):
        return None
    with _DB_LOCK, _transaction() as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, content, display_metadata FROM messages "
                "WHERE display_kind='delegation_closeout_provisional' "
                "ORDER BY id"
            ).fetchall()
        except sqlite3.OperationalError:
            return None
        for row in rows:
            try:
                metadata = json.loads(row["display_metadata"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if (
                isinstance(metadata, dict)
                and str(metadata.get("work_id") or "") == work_id
                and str(metadata.get("delivery_id") or "") == delivery_id
            ):
                return {"row_id": int(row["id"]), "content": row["content"]}
    return None


def reconcile_closed_closeout_provisionals() -> int:
    with _DB_LOCK, _transaction() as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        return _reveal_closed_closeout_provisionals(conn)


def recover_work_groups() -> List[Dict[str, Any]]:
    """Recover ready sealed/closing work and diagnose dead open owners."""
    try:
        from gateway.status import _pid_exists, get_process_start_time
    except Exception:
        def _pid_exists(_pid: int) -> bool:
            return False

        def get_process_start_time(_pid: int) -> Optional[int]:
            return None
    now = time.time()
    recovered: List[Dict[str, Any]] = []
    with _DB_LOCK, _transaction() as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        _reveal_closed_closeout_provisionals(conn)
        groups = conn.execute(
            "SELECT * FROM async_delegation_work_groups WHERE state IN ('open','sealed','closing')"
        ).fetchall()
        for group in groups:
            state = group["state"]
            if state == "open":
                pid, started = group["owner_pid"], group["owner_started_at"]
                live = bool(pid and _pid_exists(int(pid)))
                if live and started is not None:
                    live = get_process_start_time(int(pid)) == int(started)
                if not live:
                    diagnostic = "Owner process/turn cannot resume; membership sealed with outcome unknown."
                    conn.execute(
                        """UPDATE async_delegation_work_groups
                           SET state='sealed', sealed_at=?, updated_at=?,
                               terminal_diagnostics=? WHERE work_id=? AND state='open'""",
                        (now, now, diagnostic, group["work_id"]),
                    )
                    conn.execute(
                        """UPDATE async_delegations SET state='unknown', completed_at=?,
                                  updated_at=?, result_json=?
                           WHERE origin_work_id=? AND work_generation=?
                             AND state IN ('running','finalizing')""",
                        (
                            now, now, json.dumps({"status": "unknown", "error": diagnostic}),
                            group["work_id"], group["generation"],
                        ),
                    )
                    state = "sealed"
            if state == "closing" and group["closeout_payload_json"]:
                recovered.append({
                    "work_id": group["work_id"], "state": "closing",
                    "delivery_id": group["closeout_delivery_id"],
                    "envelope": json.loads(group["closeout_payload_json"]),
                    "claim_id": group["closeout_claim"],
                    "routing": {
                        "session_key": group["origin_session"],
                        "origin_ui_session_id": group["origin_ui_session_id"],
                        "origin_session_id": group["origin_session_id"],
                        "parent_session_id": group["parent_session_id"],
                    },
                })
            elif state == "sealed" and _group_ready(conn, group["work_id"], int(group["generation"])):
                recovered.append({"work_id": group["work_id"], "state": "sealed_ready"})
    return recovered


def recover_and_enqueue_work_groups(
    *, consumer: str = "async-delegation-recovery", target_queue: Any = None
) -> List[Dict[str, Any]]:
    """Publish recoverable envelopes through the same idempotent aggregate rail."""
    enqueued: List[Dict[str, Any]] = []
    for item in recover_work_groups():
        delivery_id = item.get("delivery_id")
        if delivery_id:
            delivery_key = (
                str(get_hermes_home().resolve()),
                str(delivery_id),
            )
            with _aggregate_enqueue_lock:
                if delivery_key in _aggregate_enqueued_delivery_ids:
                    continue
        claimed: Optional[Dict[str, Any]]
        if item["state"] == "sealed_ready":
            claimed = claim_ready_work_group(item["work_id"], consumer)
        else:
            # A recoverable closing row must be reclaimed through the same
            # PID+process-start fenced CAS regardless of whether the stale
            # actor left a claim id behind. The helper skips live bound or
            # live unbound owners, and rotates dead claims while clearing the
            # old closeout_turn_id before the replacement event is enqueued.
            claimed = reclaim_stale_work_group_claim(item["work_id"], consumer)
        if claimed is not None:
            event = _enqueue_claimed_work_group(claimed, target_queue=target_queue)
            if event is not None:
                enqueued.append(event)
    return enqueued


def _capture_routing_origin() -> Dict[str, Any]:
    """Snapshot the dispatching turn's routing origin for the completion event.

    Captured on the PARENT thread at dispatch time (the daemon worker doesn't
    carry the contextvars) and persisted with the durable record, so a
    completion replayed after a restart can reconstruct a full SessionSource
    even when the session-store origin and in-memory source cache are gone.
    scope_id matters most: on a relay-fronted deployment the connector's
    fail-closed egress guard needs the tenant discriminator (or a user
    binding) to route a scoped reply; without it, post-restart scoped
    completions bounce with "target not routed to an onboarded tenant"
    (staging 2026-08-09 defect #4). Best-effort — empty values are simply
    omitted so CLI/contextvar-unaware paths persist nothing new.
    """
    origin: Dict[str, Any] = {}
    try:
        from gateway.session_context import get_session_env

        for evt_key, env_name in (
            ("scope_id", "HERMES_SESSION_SCOPE_ID"),
            ("user_id", "HERMES_SESSION_USER_ID"),
            ("user_name", "HERMES_SESSION_USER_NAME"),
        ):
            value = get_session_env(env_name, "")
            if value:
                origin[evt_key] = value
    except Exception:  # noqa: BLE001 - routing origin is additive, never fatal
        pass
    return origin


def _persist_dispatch(record: Dict[str, Any]) -> None:
    now = time.time()
    try:
        from gateway.status import get_process_start_time
        owner_started_at = get_process_start_time(__import__("os").getpid())
    except Exception:
        owner_started_at = None
    task_payload = {
        key: record.get(key)
        for key in (
            "goal", "goals", "context", "toolsets", "role", "model", "is_batch",
            # Routing origin (scope_id/user_id/user_name): persisted so a
            # restart-recovered completion can reconstruct a full
            # SessionSource — see _capture_routing_origin.
            "scope_id", "user_id", "user_name",
        )
        if key in record
    }
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO async_delegations
               (delegation_id, origin_session, origin_ui_session_id,
                parent_session_id, state, dispatched_at, updated_at,
                delivery_state, delivery_attempts, owner_pid,
                owner_started_at, task_json, origin_session_id)
               VALUES (?, ?, ?, ?, 'running', ?, ?, 'pending', 0, ?, ?, ?, ?)""",
            (record["delegation_id"], record.get("session_key", ""),
             record.get("origin_ui_session_id", ""), record.get("parent_session_id"),
             record["dispatched_at"], now, __import__("os").getpid(),
             owner_started_at, json.dumps(task_payload),
             record.get("origin_session_id", "")),
        )
    _prune_durable_records()


def _delete_durable_delegation(delegation_id: str) -> None:
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            """DELETE FROM async_delegations WHERE delegation_id=? AND (
                   origin_work_id='' OR EXISTS (
                       SELECT 1 FROM async_delegation_work_groups g
                       WHERE g.work_id=async_delegations.origin_work_id
                         AND g.state='closed'))""",
            (delegation_id,),
        )


def _prune_durable_records() -> None:
    """Bound terminal history, preferring delivered records for deletion."""
    now = time.time()
    cutoff = now - _DURABLE_RETENTION_SECONDS
    with _DB_LOCK, _transaction() as conn:
        unresolved_terminal = conn.execute(
            """SELECT d.delegation_id, d.state, d.result_json, d.event_json,
                      d.task_json, d.updated_at, d.origin_work_id
               FROM async_delegations d
               JOIN async_delegation_work_groups g ON g.work_id=d.origin_work_id
               WHERE g.state!='closed' AND d.state NOT IN ('running','finalizing')"""
        ).fetchall()
        grouped_bytes = sum(
            len((row[2] or "").encode("utf-8"))
            + len((row[3] or "").encode("utf-8"))
            + len((row[4] or "").encode("utf-8"))
            for row in unresolved_terminal
        )
        pressure = grouped_bytes > _MAX_GROUP_DETAIL_BYTES
        for (delegation_id, state, result_json, event_json, task_json,
             updated_at, work_id) in unresolved_terminal:
            detail_bytes = len((result_json or "").encode("utf-8")) + len(
                (event_json or "").encode("utf-8")
            ) + len((task_json or "").encode("utf-8"))
            if not pressure and updated_at >= now - _GROUP_DETAIL_RETENTION_SECONDS \
                    and detail_bytes <= _MAX_GROUP_DETAIL_BYTES:
                continue
            status = str(state or "unknown")
            tombstone = {
                "status": status,
                "detail_lost": True,
                "diagnostic": "terminal detail compacted by bounded ledger retention",
                "detail_ref": f"async_delegation:{delegation_id}",
            }
            compact = json.dumps(tombstone, sort_keys=True, separators=(",", ":"))
            conn.execute(
                """UPDATE async_delegations SET result_json=?, event_json=?,
                          task_json='{}', updated_at=?
                   WHERE delegation_id=? AND state NOT IN ('running','finalizing')""",
                (compact, compact, now, delegation_id),
            )
            conn.execute(
                """UPDATE async_delegation_work_groups
                   SET terminal_diagnostics=CASE
                     WHEN terminal_diagnostics IS NULL OR terminal_diagnostics=''
                     THEN ? ELSE terminal_diagnostics || '; ' || ? END,
                     updated_at=? WHERE work_id=? AND state!='closed'""",
                ("Terminal member detail was compacted; outcome detail is unknown.",
                 "Terminal member detail was compacted; outcome detail is unknown.",
                 now, work_id),
            )
        conn.execute(
            """DELETE FROM async_delegations
               WHERE delivery_state='delivered' AND updated_at < ? AND (
                   origin_work_id='' OR EXISTS (
                       SELECT 1 FROM async_delegation_work_groups g
                       WHERE g.work_id=async_delegations.origin_work_id
                         AND g.state='closed'))""",
            (cutoff,),
        )
        terminal_count = conn.execute(
            """SELECT COUNT(*) FROM async_delegations
               WHERE state NOT IN ('running','finalizing') AND (
                   origin_work_id='' OR EXISTS (
                       SELECT 1 FROM async_delegation_work_groups g
                       WHERE g.work_id=async_delegations.origin_work_id
                         AND g.state='closed'))"""
        ).fetchone()[0]
        excess = max(0, terminal_count - _MAX_RETAINED_COMPLETED)
        if excess:
            conn.execute(
                """DELETE FROM async_delegations WHERE delegation_id IN (
                     SELECT delegation_id FROM async_delegations
                     WHERE state NOT IN ('running','finalizing') AND (
                       origin_work_id='' OR EXISTS (
                         SELECT 1 FROM async_delegation_work_groups g
                         WHERE g.work_id=async_delegations.origin_work_id
                           AND g.state='closed'))
                     ORDER BY CASE delivery_state WHEN 'delivered' THEN 0 ELSE 1 END,
                              updated_at ASC LIMIT ?
                   )""",
                (excess,),
            )
        pending_count = conn.execute(
            """SELECT COUNT(*) FROM async_delegations
               WHERE state NOT IN ('running','finalizing') AND delivery_state='pending'
                 AND (origin_work_id='' OR EXISTS (
                       SELECT 1 FROM async_delegation_work_groups g
                       WHERE g.work_id=async_delegations.origin_work_id
                         AND g.state='closed'))"""
        ).fetchone()[0]
        overflow = max(0, pending_count - _MAX_DURABLE_PENDING)
        if overflow:
            conn.execute(
                """DELETE FROM async_delegations WHERE delegation_id IN (
                     SELECT delegation_id FROM async_delegations
                     WHERE state NOT IN ('running','finalizing') AND delivery_state='pending'
                       AND (origin_work_id='' OR EXISTS (
                         SELECT 1 FROM async_delegation_work_groups g
                         WHERE g.work_id=async_delegations.origin_work_id
                           AND g.state='closed'))
                     ORDER BY updated_at ASC LIMIT ?
                   )""",
                (overflow,),
            )


def _persist_completion(event: Dict[str, Any], result: Dict[str, Any]) -> None:
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            """UPDATE async_delegations SET state=?, completed_at=?, updated_at=?,
               event_json=?, result_json=?, delivery_state='pending'
               WHERE delegation_id=?""",
            (event.get("status", "completed"), event.get("completed_at", now), now,
             json.dumps(event), json.dumps(result), event["delegation_id"]),
        )


def _note_delivery_attempt(delegation_id: str) -> None:
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            "UPDATE async_delegations SET delivery_attempts=delivery_attempts+1, updated_at=? WHERE delegation_id=?",
            (time.time(), delegation_id),
        )


def recover_abandoned_delegations() -> int:
    """Classify records whose owning process disappeared as outcome unknown."""
    try:
        from gateway.status import _pid_exists, get_process_start_time
    except Exception:
        return 0
    now = time.time()
    recovered = 0
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            """SELECT delegation_id, origin_session, origin_ui_session_id,
                      parent_session_id, dispatched_at, owner_pid,
                      owner_started_at, task_json, origin_session_id
               FROM async_delegations WHERE state IN ('running','finalizing')"""
        ).fetchall()
        for row in rows:
            (delegation_id, session_key, origin_ui, parent_id, dispatched_at,
             pid, started, task_json, origin_session_id) = row
            live = False
            if pid:
                live = _pid_exists(int(pid))
                if live and started is not None:
                    live = get_process_start_time(int(pid)) == int(started)
            if live:
                continue
            task = json.loads(task_json or "{}")
            event = {
                "type": "async_delegation", "delegation_id": delegation_id,
                "session_key": session_key, "origin_ui_session_id": origin_ui,
                # Restore the durable wake target so completions recovered
                # after a restart remain routable to api_server sessions.
                "origin_session_id": origin_session_id or "",
                "parent_session_id": parent_id, "goal": task.get("goal", ""),
                "goals": task.get("goals"), "context": task.get("context"),
                "toolsets": task.get("toolsets"), "role": task.get("role"),
                "model": task.get("model"), "is_batch": bool(task.get("is_batch")),
                "status": "unknown", "summary": None,
                "error": "Delegation owner exited before recording a terminal result; outcome unknown.",
                "dispatched_at": dispatched_at, "completed_at": now,
            }
            # Routing origin persisted at dispatch (see _capture_routing_origin):
            # restores scope_id/user_id for the reconstructed SessionSource so
            # relay egress priming works after a restart.
            for _k in ("scope_id", "user_id", "user_name"):
                if task.get(_k):
                    event[_k] = task[_k]
            result = {"status": "unknown", "summary": None, "error": event["error"]}
            conn.execute(
                """UPDATE async_delegations SET state='unknown', completed_at=?,
                   updated_at=?, event_json=?, result_json=?, delivery_state='pending'
                   WHERE delegation_id=?""",
                (now, now, json.dumps(event), json.dumps(result), delegation_id),
            )
            recovered += 1
    return recovered


def restore_undelivered_completions(target_queue) -> int:
    """Enqueue durable pending completions as fresh turns after process start.

    Every restored event is stamped ``restored=True`` (in-memory only — the
    stamp is added after the durable payload is deserialized and is never
    persisted). Restored events originate from a *previous* process, so no
    consumer in THIS process implicitly owns them: drain paths that run
    without an ownership filter (the legacy single-session behavior) must
    leave them queued for a consumer that can positively prove ownership,
    otherwise a brand-new session adopts a dead session's delegation
    results seconds after boot (#64484).

    Staleness cap: a pending completion older than
    ``_MAX_COMPLETION_REPLAY_AGE_S`` is terminally dropped instead of
    replayed. Replaying a weeks-old completion re-runs its parent session as
    a full-context turn (a July session replayed in August burned a
    102K-token context on the staging fleet) for a result nobody is waiting
    on anymore; the payload stays queryable on the dropped row.
    """
    recover_abandoned_delegations()
    now = time.time()
    restored = 0
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            """SELECT delegation_id, event_json, completed_at, dispatched_at
               FROM async_delegations
               WHERE state != 'running' AND delivery_state='pending'
                 AND event_json IS NOT NULL AND origin_work_id=''
               ORDER BY completed_at, delegation_id"""
        ).fetchall()
        for delegation_id, payload, completed_at, dispatched_at in rows:
            age_basis = completed_at or dispatched_at
            if age_basis and (now - age_basis) > _MAX_COMPLETION_REPLAY_AGE_S:
                conn.execute(
                    """UPDATE async_delegations SET delivery_state='dropped',
                              delivery_claim=NULL, delivery_claimed_at=NULL,
                              updated_at=?
                       WHERE delegation_id=? AND delivery_state='pending'""",
                    (now, delegation_id),
                )
                logger.warning(
                    "Async delegation %s: pending completion is %.1fh old "
                    "(cap %.1fh); terminally dropping the replay (result "
                    "remains queryable).",
                    delegation_id, (now - age_basis) / 3600.0,
                    _MAX_COMPLETION_REPLAY_AGE_S / 3600.0,
                )
                continue
            evt = json.loads(payload)
            if isinstance(evt, dict):
                evt["restored"] = True
            target_queue.put(evt)
            restored += 1
    return restored


def mark_completion_delivered(delegation_id: str) -> bool:
    """Atomically acknowledge successful injection of a durable completion."""
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_state='delivered', delivered_at=?, updated_at=?
               WHERE delegation_id=? AND delivery_state!='delivered'
                 AND (origin_work_id='' OR EXISTS (
                       SELECT 1 FROM async_delegation_work_groups g
                       WHERE g.work_id=async_delegations.origin_work_id
                         AND g.state='closed'))""",
            (now, now, delegation_id),
        )
        return cur.rowcount == 1


def claim_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Claim one pending completion across competing consumers/processes."""
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        row = conn.execute(
            "SELECT delivery_state FROM async_delegations WHERE delegation_id=?",
            (delegation_id,),
        ).fetchone()
        if row is None:
            return True  # legacy event created before durable dispatch
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_claim=?, delivery_claimed_at=?,
                      delivery_attempts=delivery_attempts+1, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'
                 AND origin_work_id=''
                 AND (delivery_claim IS NULL OR delivery_claimed_at < ?)""",
            (claim_id, now, now, delegation_id, now - 300),
        )
        return cur.rowcount == 1


def claim_event_delivery(evt: Dict[str, Any], consumer: str) -> Optional[str]:
    """Claim a durable delegation event; non-durable events need no token."""
    if evt.get("type") != "async_delegation":
        return ""
    delegation_id = str(evt.get("delegation_id") or "")
    if not delegation_id:
        return ""
    claim_id = f"{consumer}:{__import__('os').getpid()}:{uuid.uuid4().hex}"
    return claim_id if claim_completion_delivery(delegation_id, claim_id) else None


def release_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Release a failed delivery claim so another consumer may retry.

    Attempts are counted at claim time, so a row that keeps being claimed and
    released has burned real delivery attempts. Once the budget is exhausted
    the row converges to a terminal ``dropped`` state instead of returning to
    ``pending`` — otherwise an undeliverable completion replays on every
    gateway restart forever (restore_undelivered_completions only restores
    pending rows).
    """
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        capped = conn.execute(
            """UPDATE async_delegations SET delivery_state='dropped',
                      delivery_claim=NULL, delivery_claimed_at=NULL, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=? AND delivery_attempts>=?
                 AND origin_work_id=''""",
            (now, delegation_id, claim_id, _MAX_DELIVERY_ATTEMPTS),
        )
        if capped.rowcount == 1:
            logger.warning(
                "Async delegation %s exhausted its %d delivery attempts; "
                "marking terminally dropped (result remains queryable).",
                delegation_id, _MAX_DELIVERY_ATTEMPTS,
            )
            return True
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_claim=NULL,
                      delivery_claimed_at=NULL, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=? AND origin_work_id=''""",
            (now, delegation_id, claim_id),
        )
        return cur.rowcount == 1


def drop_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Terminally drop a claimed completion that can never be delivered.

    Used when the delivery target is permanently gone — the spawning session
    ended at an explicit user boundary (/new, reset) rather than a compression
    rotation. Marking the row ``dropped`` (not ``delivered``) keeps the ack
    honest, and (not ``pending``) keeps restart recovery from replaying a
    completion that will be fail-closed dropped again every time.
    """
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_state='dropped',
                      updated_at=?, delivery_claim=NULL,
                      delivery_claimed_at=NULL
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=?
                 AND (origin_work_id='' OR EXISTS (
                       SELECT 1 FROM async_delegation_work_groups g
                       WHERE g.work_id=async_delegations.origin_work_id
                         AND g.state='closed'))""",
            (now, delegation_id, claim_id),
        )
        return cur.rowcount == 1


def complete_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Acknowledge acceptance for the consumer holding this claim."""
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_state='delivered',
                      delivered_at=?, updated_at=?, delivery_claim=NULL,
                      delivery_claimed_at=NULL
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=? AND origin_work_id=''""",
            (now, now, delegation_id, claim_id),
        )
        return cur.rowcount == 1


def complete_event_delivery(evt: Dict[str, Any], claim_id: str) -> None:
    if claim_id and evt.get("type") == "async_delegation":
        complete_completion_delivery(str(evt.get("delegation_id") or ""), claim_id)


def release_event_delivery(evt: Dict[str, Any], claim_id: str) -> None:
    if claim_id and evt.get("type") == "async_delegation":
        release_completion_delivery(str(evt.get("delegation_id") or ""), claim_id)


def get_durable_delegation(delegation_id: str) -> Optional[Dict[str, Any]]:
    with _DB_LOCK, _transaction() as conn:
        row = conn.execute(
            """SELECT origin_session, state, dispatched_at, completed_at,
                      result_json, delivery_state, delivery_attempts,
                      origin_session_id
               FROM async_delegations WHERE delegation_id=?""", (delegation_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "delegation_id": delegation_id, "origin_session": row[0], "state": row[1],
        "dispatched_at": row[2], "completed_at": row[3],
        "result": json.loads(row[4]) if row[4] else None,
        "delivery_state": row[5], "delivery_attempts": row[6],
        "origin_session_id": row[7] or "",
    }


def _get_executor(max_workers: int) -> ThreadPoolExecutor:
    """Lazily create (or grow) the shared daemon executor.

    We never shrink — ThreadPoolExecutor can't resize — but if the configured
    cap grows between calls we rebuild a larger pool. Existing in-flight
    futures keep running on the old pool until it's garbage collected.
    """
    global _executor, _executor_max_workers
    with _executor_lock:
        if _executor is None or max_workers > _executor_max_workers:
            # Daemon threads: thread_name_prefix aids debugging in stack dumps.
            _executor = _DaemonThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="async-delegate",
            )
            _executor_max_workers = max_workers
        return _executor


def active_count() -> int:
    """Number of async delegation UNITS currently running.

    A unit is one dispatch: a single subagent OR a whole fan-out batch. A batch
    counts as ONE here because it occupies one async-pool slot (the capacity
    semantics ``dispatch_async_delegation_batch`` relies on). For the count of
    actual concurrent child subagents (batch expanded), use
    ``active_task_count()``.
    """
    with _records_lock:
        return sum(
            1 for r in _records.values()
            if r.get("status") in {"running", "stalling", "finalizing"}
        )


def active_for_session(origin_ui_session_id: str) -> int:
    """Number of live async delegations owned by one UI session."""
    if not origin_ui_session_id:
        return 0
    with _records_lock:
        return sum(
            1
            for r in _records.values()
            if r.get("status") in {"running", "stalling", "finalizing"}
            and str(r.get("origin_ui_session_id") or "")
            == origin_ui_session_id
        )


def active_task_count() -> int:
    """Number of async delegation TASKS (child subagents) currently running.

    Unlike ``active_count()`` (units/slots), this expands a batch to its child
    count: a running batch of N tasks contributes N, a single subagent
    contributes 1. This is the truthful "how many subagents are actually
    working right now" figure for observability, where a 3-task batch shown as
    "1" undercounts real concurrent work. Falls back to counting a batch as 1
    if its goal list is missing.
    """
    with _records_lock:
        total = 0
        for r in _records.values():
            if r.get("status") not in {"running", "finalizing"}:
                continue
            if r.get("is_batch"):
                goals = r.get("goals")
                total += len(goals) if isinstance(goals, (list, tuple)) and goals else 1
            else:
                total += 1
        return total


def _matches_session_selectors(
    record: Dict[str, Any],
    *,
    session_key: str = "",
    origin_ui_session_id: str = "",
    parent_session_id: str = "",
) -> bool:
    return (
        (origin_ui_session_id and str(record.get("origin_ui_session_id") or "") == origin_ui_session_id)
        or (session_key and str(record.get("session_key") or "") == session_key)
        or (parent_session_id and str(record.get("parent_session_id") or "") == parent_session_id)
    )


def has_live_for_session(
    session_key: str = "",
    origin_ui_session_id: str = "",
    parent_session_id: str = "",
) -> bool:
    """Whether a session still owns any live async delegation.

    Live = running / stalling / finalizing — the same states the reapers'
    keepalive treats as active work.
    """
    if not session_key and not origin_ui_session_id and not parent_session_id:
        return False
    with _records_lock:
        return any(
            r.get("status") in {"running", "stalling", "finalizing"}
            and _matches_session_selectors(
                r,
                session_key=session_key,
                origin_ui_session_id=origin_ui_session_id,
                parent_session_id=parent_session_id,
            )
            for r in _records.values()
        )


def _new_delegation_id() -> str:
    return f"deleg_{uuid.uuid4().hex[:8]}"


def _prune_completed_locked() -> None:
    """Drop the oldest completed records beyond the retention cap.

    Caller must hold ``_records_lock``.
    """
    completed = [
        (rid, r)
        for rid, r in _records.items()
        if r.get("status") != "running"
    ]
    if len(completed) <= _MAX_RETAINED_COMPLETED:
        return
    # Oldest-first by completion time (fall back to dispatch time).
    completed.sort(key=lambda kv: kv[1].get("completed_at") or kv[1].get("dispatched_at") or 0)
    for rid, _ in completed[: len(completed) - _MAX_RETAINED_COMPLETED]:
        _records.pop(rid, None)


def _current_origin_session_id() -> str:
    """Raw session id of the ORIGINATING api_server request, or ``""``.

    The obvious source — ``HERMES_SESSION_ID`` via ``get_session_env`` — is
    NOT safe to read at dispatch time: constructing a child agent
    (``agent/agent_init.py``) calls ``set_current_session_id(child.session_id)``,
    clobbering that ContextVar *and* ``os.environ`` with the subagent's
    internal ``{timestamp}_{uuid}`` id moments before the dispatch code reads
    it, so the completion wake would self-post into the subagent's own
    (unread) session instead of the spawner's.

    The request-scoped ``HERMES_SESSION_CHAT_ID`` binding survives child
    construction: ``_bind_api_server_session`` binds ``chat_id`` to the raw
    ``X-Hermes-Session-Id``, and its only writer is ``set_session_vars`` —
    ``set_current_session_id`` never touches it. Gate on the platform: on
    push platforms ``chat_id`` is a chat, not a session, so yield ``""``
    there.
    """
    try:
        from gateway.session_context import get_session_env

        if get_session_env("HERMES_SESSION_PLATFORM", "") != "api_server":
            return ""
        return get_session_env("HERMES_SESSION_CHAT_ID", "") or ""
    except Exception:
        return ""


def dispatch_async_delegation(
    *,
    goal: str,
    context: Optional[str],
    toolsets: Optional[List[str]],
    role: str,
    model: Optional[str],
    session_key: str,
    parent_session_id: Optional[str] = None,
    runner: Callable[[], Dict[str, Any]],
    origin_ui_session_id: str = "",
    origin_session_id: str = "",
    interrupt_fn: Optional[Callable[[], None]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN,
    progress_fn: Optional[Callable[[], tuple]] = None,
    origin_work_id: str = "",
    work_generation: int = 0,
    owner_turn_id: str = "",
    closeout_delivery_id: str = "",
    closeout_claim_id: str = "",
) -> Dict[str, Any]:
    """Spawn ``runner`` on the daemon executor and return a handle immediately.

    Parameters
    ----------
    goal, context, toolsets, role, model
        The dispatch-time task spec, captured verbatim for the rich
        completion block.
    session_key
        The gateway session_key (from ``tools.approval.get_current_session_key``)
        captured on the parent thread BEFORE dispatch, because the daemon
        worker thread won't carry the contextvar. Used to route the
        completion back to the originating session.
    parent_session_id
        The durable ``state.db`` session id of the parent agent that spawned
        the delegation. Carried on the completion event so the gateway can
        pin routing to the spawning session instead of recovering the latest
        ``ended_at IS NULL`` row for the peer tuple (#57498).
    runner
        Zero-arg callable that builds + runs the child and returns the same
        result dict ``_run_single_child`` produces. Runs on the worker thread.
    interrupt_fn
        Optional callable to signal the child to stop (used on shutdown /
        explicit cancel).
    progress_fn
        Optional zero-arg callable returning ``(token, in_tool)`` where
        ``token`` is any comparable snapshot of the child's progress (api
        call count + current tool) and ``in_tool`` says whether the child is
        currently inside a tool call. Sampled by the stale monitor; a frozen
        token past the stale threshold marks the delegation stuck (see the
        stale-detection block at the top of this module). When omitted, the
        delegation is not monitored.
    max_async_children
        Concurrency cap. When at capacity the dispatch is REJECTED (the caller
        should fall back to sync or tell the user) rather than queued, so a
        runaway model can't pile up unbounded background work.

    Returns
    -------
    dict
        ``{"status": "dispatched", "delegation_id": ...}`` on success, or
        ``{"status": "rejected", "error": ...}`` when at capacity.
    """
    delegation_id = _new_delegation_id()
    dispatched_at = time.time()
    record: Dict[str, Any] = {
        "delegation_id": delegation_id,
        "goal": goal,
        "context": context,
        "toolsets": list(toolsets) if toolsets else None,
        "role": role,
        "model": model,
        "session_key": session_key,
        "origin_ui_session_id": origin_ui_session_id,
        "origin_session_id": origin_session_id,
        "parent_session_id": parent_session_id,
        "origin_work_id": origin_work_id,
        "work_generation": work_generation,
        **_capture_routing_origin(),
        "status": "running",
        "dispatched_at": dispatched_at,
        "completed_at": None,
        "interrupt_fn": interrupt_fn,
        "progress_fn": progress_fn,
        # Stale-monitor bookkeeping (see _stale_monitor_loop).
        "_progress_token": None,
        "_progress_ts": dispatched_at,
        "_interrupted_at": None,
    }
    # Capacity check and record insert under ONE lock hold — checking
    # active_count() separately would let two concurrent dispatches (e.g.
    # from different gateway sessions) both pass the check and exceed the cap.
    with _records_lock:
        running = sum(
            1 for r in _records.values()
            if r.get("status") in ("running", "stalling")
        )
        if running >= max_async_children:
            return {
                "status": "rejected",
                "error": (
                    f"Async delegation capacity reached ({max_async_children} "
                    f"running). Wait for one to finish (its result will re-enter "
                    f"the chat), or run this task synchronously "
                    f"(background=false). Raise delegation.max_concurrent_children in "
                    f"config.yaml to allow more concurrent background subagents."
                ),
            }
        _records[delegation_id] = record

    if origin_work_id:
        routing = {
            "origin_session": session_key,
            "origin_ui_session_id": origin_ui_session_id,
            "origin_session_id": origin_session_id,
            "parent_session_id": parent_session_id,
            **_capture_routing_origin(),
        }
        task = {"goal": goal, "context": context, "role": role, "model": model}
        if closeout_delivery_id and closeout_claim_id:
            registered = reopen_work_group_with_member(
                work_id=origin_work_id,
                generation=work_generation - 1,
                delivery_id=closeout_delivery_id,
                claim_id=closeout_claim_id,
                closeout_turn_id=owner_turn_id,
                delegation_id=delegation_id,
                task=task,
                dispatched_at=dispatched_at,
            )
        else:
            registered = register_work_group_member(
                work_id=origin_work_id,
                owner_turn_id=owner_turn_id,
                delegation_id=delegation_id,
                generation=work_generation,
                routing=routing,
                task=task,
                dispatched_at=dispatched_at,
            )
        if not registered:
            with _records_lock:
                _records.pop(delegation_id, None)
            return {
                "status": "rejected",
                "error": "Could not durably register delegated work; no background child was submitted.",
            }
    else:
        _persist_dispatch(record)
    executor = _get_executor(max_async_children)

    def _worker() -> None:
        result: Dict[str, Any] = {}
        status = "error"
        try:
            result = runner() or {}
            status = result.get("status") or "completed"
        except Exception as exc:  # noqa: BLE001 — must never crash the worker
            logger.exception("Async delegation %s crashed", delegation_id)
            result = {
                "status": "error",
                "summary": None,
                "error": f"{type(exc).__name__}: {exc}",
                "api_calls": 0,
                "duration_seconds": round(time.time() - dispatched_at, 2),
            }
            status = "error"
        finally:
            _finalize(delegation_id, result, status)

    try:
        # Propagate the dispatching profile so the detached child resolves
        # get_hermes_home() under the right profile.
        executor.submit(propagate_context_to_thread(_worker))
    except Exception as exc:  # pragma: no cover — pool submit failure is rare
        with _records_lock:
            _records.pop(delegation_id, None)
        if origin_work_id:
            _unregister_unsubmitted_work_group_member(delegation_id)
        else:
            _delete_durable_delegation(delegation_id)
        return {
            "status": "rejected",
            "error": f"Failed to schedule async delegation: {exc}",
        }
    if progress_fn is not None:
        _ensure_stale_monitor()

    logger.info(
        "Dispatched async delegation %s (session_key=%s): %s",
        delegation_id, session_key or "<cli>", (goal or "")[:80],
    )
    return {"status": "dispatched", "delegation_id": delegation_id}


def _finalize(delegation_id: str, result: Dict[str, Any], status: str) -> None:
    """Mark a record complete and push the completion event onto the queue."""
    claimed = _begin_finalization(delegation_id)
    if claimed is None:
        return
    event_record, _interrupt_fn = claimed

    _push_completion_event(event_record, result, status)
    _finish_finalization(delegation_id, status)


def _begin_finalization(
    delegation_id: str,
) -> Optional[tuple[Dict[str, Any], Optional[Callable[[], None]]]]:
    """Atomically claim terminal delivery while keeping the record active."""
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None or record.get("status") not in ("running", "stalling"):
            return
        # Stay active until durable persistence and queue publication finish;
        # otherwise process shutdown can kill this daemon worker in the narrow
        # gap after status flips but before SQLite is committed.
        record["status"] = "finalizing"
        record["completed_at"] = time.time()
        interrupt_fn = record.get("interrupt_fn")
        record["interrupt_fn"] = None  # drop the closure; child is done
        record["progress_fn"] = None  # stop stale-monitor sampling
        event_record = dict(record)

    return event_record, interrupt_fn


def _finish_finalization(delegation_id: str, status: str) -> None:
    with _records_lock:
        record = _records.get(delegation_id)
        if record is not None:
            record["status"] = status
        _prune_completed_locked()


def _push_completion_event(
    record: Dict[str, Any], result: Dict[str, Any], status: str
) -> None:
    """Push a type='async_delegation' event onto the shared completion queue.

    Best-effort: a failure here must not crash the worker, but it WOULD mean a
    silently-lost result, so we log loudly.
    """
    try:
        from tools.process_registry import process_registry
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation %s finished but process_registry import failed; "
            "result lost: %s",
            record.get("delegation_id"), exc,
        )
        return

    summary = result.get("summary")
    error = result.get("error")
    dispatched_at = record.get("dispatched_at") or time.time()
    completed_at = record.get("completed_at") or time.time()

    evt = {
        "type": "async_delegation",
        "delegation_id": record.get("delegation_id"),
        # session_key routes the completion back to the originating gateway
        # session; empty string => CLI (single-session) path.
        "session_key": record.get("session_key", ""),
        "origin_ui_session_id": record.get("origin_ui_session_id", ""),
        "origin_session_id": record.get("origin_session_id", ""),
        "parent_session_id": record.get("parent_session_id"),
        "goal": record.get("goal", ""),
        "context": record.get("context"),
        "toolsets": record.get("toolsets"),
        "role": record.get("role"),
        "model": result.get("model") or record.get("model"),
        "status": status,
        "summary": summary,
        "error": error,
        "api_calls": result.get("api_calls", 0),
        "duration_seconds": result.get(
            "duration_seconds", round(completed_at - dispatched_at, 2)
        ),
        "dispatched_at": dispatched_at,
        "completed_at": completed_at,
        "exit_reason": result.get("exit_reason"),
    }
    # Routing origin captured at dispatch (see _capture_routing_origin):
    # additive, lets the gateway reconstruct a full SessionSource (incl.
    # scope_id for relay tenant egress) when its own caches are cold.
    for _k in ("scope_id", "user_id", "user_name"):
        if record.get(_k):
            evt[_k] = record[_k]
    # Structured stall metadata (#51690) — additive, present only on
    # stall-monitor finalizations.
    for _k in (
        "stalled_after_quiet_seconds",
        "stall_threshold_seconds",
        "stall_phase",
        "stall_grace_seconds",
    ):
        if _k in result:
            evt[_k] = result[_k]
    work_id = str(record.get("origin_work_id") or "")
    if work_id:
        evt["origin_work_id"] = work_id
        evt["work_generation"] = int(record.get("work_generation") or 0)
        ready = persist_group_member_completion(
            str(record.get("delegation_id") or ""), evt, result
        )
        if ready:
            try:
                claim_and_enqueue_ready_work_group(work_id)
            except Exception:
                logger.exception("Failed to enqueue ready work group %s", work_id)
        return
    _persist_completion(evt, result)
    try:
        process_registry.completion_queue.put(evt)
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation %s: failed to enqueue completion event; "
            "result lost: %s",
            record.get("delegation_id"), exc,
        )


def dispatch_async_delegation_batch(
    *,
    goals: List[str],
    context: Optional[str],
    toolsets: Optional[List[str]],
    role: str,
    model: Optional[str],
    session_key: str,
    parent_session_id: Optional[str] = None,
    runner: Callable[[], Dict[str, Any]],
    origin_ui_session_id: str = "",
    origin_session_id: str = "",
    interrupt_fn: Optional[Callable[[], None]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN,
    delegation_id: Optional[str] = None,
    progress_fn: Optional[Callable[[], tuple]] = None,
    origin_work_id: str = "",
    work_generation: int = 0,
    owner_turn_id: str = "",
    closeout_delivery_id: str = "",
    closeout_claim_id: str = "",
) -> Dict[str, Any]:
    """Dispatch a WHOLE fan-out batch as ONE background unit.

    Unlike ``dispatch_async_delegation`` (which backs a single subagent),
    ``runner`` here runs the entire batch — it builds and joins on every child
    in parallel and returns the combined ``{"results": [...],
    "total_duration_seconds": N}`` dict that the synchronous path would have
    returned. We occupy ONE async slot for the whole batch (the in-batch
    parallelism is bounded separately by ``max_concurrent_children``), so a
    single ``delegate_task`` fan-out never exhausts the async pool by itself.

    When the batch finishes, a SINGLE completion event is pushed onto the
    shared ``process_registry.completion_queue`` carrying the full per-task
    ``results`` list, so the consolidated summaries re-enter the conversation
    as one message once every child is done — the chat is never blocked while
    they run.

    Returns ``{"status": "dispatched", "delegation_id": ...}`` on success or
    ``{"status": "rejected", "error": ...}`` when the async pool is at
    capacity.
    """
    delegation_id = delegation_id or _new_delegation_id()
    dispatched_at = time.time()
    n = len(goals)
    # A combined goal label for status listings / the completion header.
    combined_goal = (
        goals[0] if n == 1 else f"{n} parallel subagents: " + "; ".join(g[:40] for g in goals)
    )
    record: Dict[str, Any] = {
        "delegation_id": delegation_id,
        "goal": combined_goal,
        "goals": list(goals),
        "context": context,
        "toolsets": list(toolsets) if toolsets else None,
        "role": role,
        "model": model,
        "session_key": session_key,
        "origin_ui_session_id": origin_ui_session_id,
        "origin_session_id": origin_session_id,
        "parent_session_id": parent_session_id,
        "origin_work_id": origin_work_id,
        "work_generation": work_generation,
        **_capture_routing_origin(),
        "status": "running",
        "dispatched_at": dispatched_at,
        "completed_at": None,
        "interrupt_fn": interrupt_fn,
        "is_batch": True,
        "progress_fn": progress_fn,
        "_progress_token": None,
        "_progress_ts": dispatched_at,
        "_interrupted_at": None,
    }
    with _records_lock:
        running = sum(
            1 for r in _records.values()
            if r.get("status") in ("running", "stalling")
        )
        if running >= max_async_children:
            return {
                "status": "rejected",
                "error": (
                    f"Async delegation capacity reached ({max_async_children} "
                    f"running). Wait for one to finish (its result will re-enter "
                    f"the chat), or raise delegation.max_concurrent_children in "
                    f"config.yaml to allow more concurrent background units."
                ),
            }
        _records[delegation_id] = record

    if origin_work_id:
        routing = {
            "origin_session": session_key,
            "origin_ui_session_id": origin_ui_session_id,
            "origin_session_id": origin_session_id,
            "parent_session_id": parent_session_id,
            **_capture_routing_origin(),
        }
        task = {"goals": list(goals), "context": context, "role": role, "model": model,
                "is_batch": True}
        if closeout_delivery_id and closeout_claim_id:
            registered = reopen_work_group_with_member(
                work_id=origin_work_id,
                generation=work_generation - 1,
                delivery_id=closeout_delivery_id,
                claim_id=closeout_claim_id,
                closeout_turn_id=owner_turn_id,
                delegation_id=delegation_id,
                task=task,
                dispatched_at=dispatched_at,
            )
        else:
            registered = register_work_group_member(
                work_id=origin_work_id,
                owner_turn_id=owner_turn_id,
                delegation_id=delegation_id,
                generation=work_generation,
                routing=routing,
                task=task,
                dispatched_at=dispatched_at,
            )
        if not registered:
            with _records_lock:
                _records.pop(delegation_id, None)
            return {
                "status": "rejected",
                "error": "Could not durably register delegated work; no background child was submitted.",
            }
    else:
        _persist_dispatch(record)
    executor = _get_executor(max_async_children)

    def _worker() -> None:
        combined: Dict[str, Any] = {}
        status = "error"
        try:
            combined = runner() or {}
            # Batch status: completed unless every child errored/was interrupted.
            child_results = combined.get("results") or []
            if child_results and all(
                (r.get("status") not in ("completed", "success"))
                for r in child_results
            ):
                status = "error"
            else:
                status = "completed"
        except Exception as exc:  # noqa: BLE001 — must never crash the worker
            logger.exception("Async delegation batch %s crashed", delegation_id)
            combined = {
                "results": [],
                "error": f"{type(exc).__name__}: {exc}",
                "total_duration_seconds": round(time.time() - dispatched_at, 2),
            }
            status = "error"
        finally:
            _finalize_batch(delegation_id, combined, status)

    try:
        # Propagate the dispatching profile to the detached batch children.
        executor.submit(propagate_context_to_thread(_worker))
    except Exception as exc:  # pragma: no cover
        with _records_lock:
            _records.pop(delegation_id, None)
        if origin_work_id:
            _unregister_unsubmitted_work_group_member(delegation_id)
        else:
            _delete_durable_delegation(delegation_id)
        return {
            "status": "rejected",
            "error": f"Failed to schedule async delegation batch: {exc}",
        }
    if progress_fn is not None:
        _ensure_stale_monitor()

    logger.info(
        "Dispatched async delegation batch %s (%d task(s), session_key=%s)",
        delegation_id, n, session_key or "<cli>",
    )
    return {"status": "dispatched", "delegation_id": delegation_id}


def _finalize_batch(
    delegation_id: str, combined: Dict[str, Any], status: str
) -> None:
    """Mark a batch record complete and push ONE combined completion event."""
    claimed = _begin_finalization(delegation_id)
    if claimed is None:
        return
    event_record, _interrupt_fn = claimed

    _push_batch_completion_event(event_record, combined, status)
    _finish_finalization(delegation_id, status)


def _push_batch_completion_event(
    event_record: Dict[str, Any], combined: Dict[str, Any], status: str
) -> None:
    """Push a combined async-delegation batch completion event."""
    try:
        from tools.process_registry import process_registry
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation batch %s finished but process_registry import "
            "failed; result lost: %s",
            event_record.get("delegation_id"), exc,
        )
        return

    dispatched_at = event_record.get("dispatched_at") or time.time()
    completed_at = event_record.get("completed_at") or time.time()
    evt = {
        "type": "async_delegation",
        "delegation_id": event_record.get("delegation_id"),
        "session_key": event_record.get("session_key", ""),
        "origin_ui_session_id": event_record.get("origin_ui_session_id", ""),
        "origin_session_id": event_record.get("origin_session_id", ""),
        "parent_session_id": event_record.get("parent_session_id"),
        "goal": event_record.get("goal", ""),
        "goals": event_record.get("goals"),
        "context": event_record.get("context"),
        "toolsets": event_record.get("toolsets"),
        "role": event_record.get("role"),
        "model": event_record.get("model"),
        "status": status,
        "is_batch": True,
        # The full per-task results list — the formatter renders a
        # consolidated multi-task block from this.
        "results": combined.get("results") or [],
        # Per-task live transcript log paths (cache/delegation/live/...).
        # They persist after completion and double as the full-fidelity
        # operational record of each child's run.
        "live_transcripts": combined.get("live_transcripts"),
        "error": combined.get("error"),
        "total_duration_seconds": combined.get("total_duration_seconds"),
        "dispatched_at": dispatched_at,
        "completed_at": completed_at,
    }
    # Routing origin captured at dispatch (see _capture_routing_origin).
    for _k in ("scope_id", "user_id", "user_name"):
        if event_record.get(_k):
            evt[_k] = event_record[_k]
    # Structured stall metadata (#51690) — additive, present only on
    # stall-monitor finalizations.
    for _k in (
        "stalled_after_quiet_seconds",
        "stall_threshold_seconds",
        "stall_phase",
        "stall_grace_seconds",
    ):
        if _k in combined:
            evt[_k] = combined[_k]
    work_id = str(event_record.get("origin_work_id") or "")
    if work_id:
        evt["origin_work_id"] = work_id
        evt["work_generation"] = int(event_record.get("work_generation") or 0)
        ready = persist_group_member_completion(
            str(event_record.get("delegation_id") or ""), evt, combined
        )
        if ready:
            try:
                claim_and_enqueue_ready_work_group(work_id)
            except Exception:
                logger.exception("Failed to enqueue ready work group %s", work_id)
        return
    _persist_completion(evt, combined)
    try:
        process_registry.completion_queue.put(evt)
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation batch %s: failed to enqueue completion event; "
            "result lost: %s",
            event_record.get("delegation_id"), exc,
        )


def _ensure_stale_monitor() -> None:
    """Start (once) the module-level stale-delegation monitor thread.

    One daemon thread serves every dispatch; it exits on its own when no
    monitorable records remain, and is restarted by the next dispatch that
    carries a ``progress_fn``.
    """
    global _monitor_thread
    with _monitor_lock:
        if _monitor_thread is not None and _monitor_thread.is_alive():
            return
        _monitor_stop.clear()
        _monitor_thread = threading.Thread(
            target=_stale_monitor_loop,
            name="async-delegate-stale-monitor",
            daemon=True,
        )
        _monitor_thread.start()


def _stale_monitor_loop() -> None:
    """Sweep running delegations for stalled progress.

    Per sweep, for every running record with a ``progress_fn``:

    - Sample ``(token, in_tool)``. A changed token refreshes the record's
      progress timestamp — a child that keeps advancing is never touched, no
      matter how long it runs.
    - A frozen token past the idle/in-tool threshold marks the record
      ``stalling``: we call ``interrupt_fn`` so a responsive-but-slow child
      can unwind and deliver its (partial) result through the normal
      ``_finalize`` path with full fidelity.
    - A ``stalling`` record whose runner still hasn't returned after the
      grace window is force-finalized with one terminal ``stalled`` event so
      the owning session hears an outcome and the async slot frees. A late
      runner return after that is ignored by ``_begin_finalization``.
    """
    while not _monitor_stop.wait(_STALE_CHECK_INTERVAL):
        now = time.time()
        stalled: List[tuple] = []  # (delegation_id, is_batch, quiet_for, in_tool)
        expired: List[str] = []  # stalling past grace → force-finalize
        any_monitorable = False
        with _records_lock:
            for record in _records.values():
                status = record.get("status")
                if status == "stalling":
                    any_monitorable = True
                    interrupted_at = record.get("_interrupted_at") or now
                    if now - interrupted_at >= _STALL_GRACE_SECONDS:
                        expired.append(record["delegation_id"])
                    continue
                if status != "running":
                    continue
                progress_fn = record.get("progress_fn")
                if progress_fn is None:
                    continue
                any_monitorable = True
                try:
                    token, in_tool = progress_fn()
                except Exception:
                    # An unreadable child must not look permanently healthy —
                    # keep the last timestamp running instead of refreshing it.
                    token, in_tool = record.get("_progress_token"), False
                if token != record.get("_progress_token"):
                    record["_progress_token"] = token
                    record["_progress_ts"] = now
                    continue
                quiet_for = now - (record.get("_progress_ts") or now)
                limit = (
                    _STALE_IN_TOOL_SECONDS if in_tool else _STALE_IDLE_SECONDS
                )
                if quiet_for >= limit:
                    record["status"] = "stalling"
                    record["_interrupted_at"] = now
                    # Structured stall context for the terminal event and
                    # status listings (#51690): how long progress was frozen,
                    # which threshold applied, and whether the child was
                    # inside a tool when it went quiet.
                    record["_stall_quiet_seconds"] = round(quiet_for, 2)
                    record["_stall_threshold_seconds"] = limit
                    record["_stall_in_tool"] = bool(in_tool)
                    stalled.append(
                        (
                            record["delegation_id"],
                            bool(record.get("is_batch")),
                            quiet_for,
                            in_tool,
                        )
                    )
        for delegation_id, _is_batch, quiet_for, in_tool in stalled:
            logger.warning(
                "Async delegation %s made no progress for %.0fs "
                "(in_tool=%s) — interrupting; grace window %.0fs",
                delegation_id, quiet_for, in_tool, _STALL_GRACE_SECONDS,
            )
            with _records_lock:
                record = _records.get(delegation_id)
                fn = record.get("interrupt_fn") if record else None
            if callable(fn):
                try:
                    fn()
                except Exception as exc:
                    logger.debug(
                        "Async delegation %s stall interrupt failed: %s",
                        delegation_id, exc,
                    )
        for delegation_id in expired:
            _finalize_stalled(delegation_id)
        if not any_monitorable:
            return


def _finalize_stalled(delegation_id: str) -> None:
    """Force-finalize a stalling delegation whose runner never returned."""
    claimed = _begin_finalization(delegation_id)
    if claimed is None:
        return
    event_record, _interrupt_fn = claimed

    completed_at = event_record.get("completed_at") or time.time()
    duration = round(
        completed_at - (event_record.get("dispatched_at") or completed_at),
        2,
    )
    quiet_seconds = event_record.get("_stall_quiet_seconds")
    threshold_seconds = event_record.get("_stall_threshold_seconds")
    stall_in_tool = event_record.get("_stall_in_tool")
    error = (
        f"Async delegation {delegation_id} stalled: the detached subagent "
        "stopped making progress (no new API calls, tool activity, or "
        "streamed tokens), did not respond to interruption, and never "
        "produced a completion event. The worker may be wedged inside a "
        "model API call — this is a known failure mode of long-lived "
        "gateway processes (#60203). Re-dispatch the task if it is still "
        "needed."
    )
    logger.error(
        "Async delegation %s force-finalized as stalled after %.0fs",
        delegation_id, duration,
    )
    # Structured stall metadata (#51690): lets parents and UIs distinguish
    # a stall-monitor kill from other failures without parsing the error
    # string, mirroring the sync path's timeout_seconds/timed_out_after_
    # seconds/timeout_phase fields.
    stall_meta = {
        "stalled_after_quiet_seconds": quiet_seconds,
        "stall_threshold_seconds": threshold_seconds,
        "stall_phase": (
            "in_tool" if stall_in_tool
            else "idle" if stall_in_tool is not None
            else None
        ),
        "stall_grace_seconds": _STALL_GRACE_SECONDS,
    }
    if event_record.get("is_batch"):
        _push_batch_completion_event(
            event_record,
            {
                "results": [],
                "error": error,
                "total_duration_seconds": duration,
                **stall_meta,
            },
            "stalled",
        )
    else:
        _push_completion_event(
            event_record,
            {
                "status": "stalled",
                "summary": None,
                "error": error,
                "api_calls": 0,
                "duration_seconds": duration,
                "exit_reason": "stalled",
                **stall_meta,
            },
            "stalled",
        )
    _finish_finalization(delegation_id, "stalled")


def _children_activity_from_token(token: Any, now: float) -> Optional[List]:
    """Parse a progress token into per-child activity dicts (best-effort).

    delegate_tool's ``_batch_progress`` emits one ``(api_call_count,
    current_tool, last_activity_ts)`` tuple per child. Foreign token shapes
    (custom dispatchers) degrade to ``None`` entries rather than raising —
    the token contract is intentionally opaque to the registry.
    """
    try:
        parts = list(token)
    except TypeError:
        return None
    out: List[Optional[Dict[str, Any]]] = []
    for part in parts:
        if isinstance(part, (list, tuple)) and len(part) >= 2:
            entry: Dict[str, Any] = {
                "api_calls": part[0],
                "current_tool": part[1],
            }
            if len(part) >= 3 and isinstance(part[2], (int, float)):
                entry["seconds_since_activity"] = round(
                    max(0.0, now - float(part[2])), 1
                )
            out.append(entry)
        else:
            out.append(None)
    return out


def list_async_delegations() -> List[Dict[str, Any]]:
    """Snapshot of async delegations (running + recently completed).

    Safe to call from any thread. Excludes the non-serialisable callables
    and private monitor bookkeeping, but exposes computed live-status
    fields for UIs (#51690):

    - ``seconds_since_progress``: how long the stale monitor has seen a
      frozen progress token (running/stalling records).
    - ``children_activity``: per-child ``{api_calls, current_tool,
      seconds_since_activity}`` sampled live from the dispatch's
      ``progress_fn``.
    - ``stalled_after_quiet_seconds`` / ``stall_threshold_seconds`` /
      ``stall_in_tool``: stall context once the monitor has tripped.
    """
    now = time.time()
    samplers: Dict[str, Callable] = {}
    with _records_lock:
        items = []
        for r in _records.values():
            item = {
                k: v
                for k, v in r.items()
                if k not in {"interrupt_fn", "progress_fn"}
                and not k.startswith("_")
            }
            status = r.get("status")
            if status in ("running", "stalling"):
                ts = r.get("_progress_ts")
                if ts:
                    item["seconds_since_progress"] = round(now - ts, 1)
                fn = r.get("progress_fn")
                if callable(fn):
                    samplers[r["delegation_id"]] = fn
            if status in ("stalling", "stalled"):
                for src, dst in (
                    ("_stall_quiet_seconds", "stalled_after_quiet_seconds"),
                    ("_stall_threshold_seconds", "stall_threshold_seconds"),
                    ("_stall_in_tool", "stall_in_tool"),
                ):
                    if r.get(src) is not None:
                        item[dst] = r.get(src)
            items.append(item)

    # Sample live activity OUTSIDE the lock — progress_fn reads child-agent
    # attributes and must never run under _records_lock (a slow or broken
    # sampler would block every dispatch/finalize in the process).
    for item in items:
        fn = samplers.get(item.get("delegation_id"))
        if fn is None:
            continue
        try:
            token, in_tool = fn()
        except Exception:
            continue
        activity = _children_activity_from_token(token, now)
        if activity is not None:
            item["children_activity"] = activity
        item["in_tool"] = bool(in_tool)
    return items


def interrupt_all(reason: str = "shutdown") -> int:
    """Signal every running async delegation to stop. Returns how many.

    Used on ``/stop`` and gateway shutdown so a dangling background subagent
    can't keep burning tokens with no one listening. The child still emits a
    completion event (status='interrupted') via the normal finalize path.
    """
    count = 0
    with _records_lock:
        targets = [
            r for r in _records.values()
            if r.get("status") in ("running", "stalling")
        ]
    for r in targets:
        fn = r.get("interrupt_fn")
        if callable(fn):
            try:
                fn()
                count += 1
            except Exception as exc:
                logger.debug(
                    "interrupt_all: %s interrupt failed: %s",
                    r.get("delegation_id"), exc,
                )
    if count:
        logger.info("Interrupted %d async delegation(s) (%s)", count, reason)
    return count


def interrupt_for_session(
    session_key: str = "",
    origin_ui_session_id: str = "",
    parent_session_id: str = "",
    reason: str = "session_end",
) -> int:
    """Signal running async delegations owned by ONE session to stop.

    A delegation's lifecycle is bound to the session that spawned it: when
    that session ends, its in-flight background subagents must end with it —
    a completed orphan would otherwise sit on the shared completion queue
    with no live owner, either leaking into another chat or burning tokens
    with no one listening (#55578).

    Selectors (any matching field claims the record):
    - ``origin_ui_session_id``: the live TUI tab/window that commissioned it.
    - ``session_key``: the durable routing key captured at dispatch.
    - ``parent_session_id``: the spawning agent's durable session-db id —
      the right selector for gateway chats, whose ``session_key`` (the
      platform conversation key) SURVIVES a ``/new`` reset while the
      session id rotates.

    Returns how many were interrupted.
    """
    if not session_key and not origin_ui_session_id and not parent_session_id:
        return 0
    count = 0
    with _records_lock:
        targets = [
            r for r in _records.values()
            if r.get("status") in ("running", "stalling")
            and _matches_session_selectors(
                r,
                session_key=session_key,
                origin_ui_session_id=origin_ui_session_id,
                parent_session_id=parent_session_id,
            )
        ]
    for r in targets:
        fn = r.get("interrupt_fn")
        if callable(fn):
            try:
                fn()
                count += 1
            except Exception as exc:
                logger.debug(
                    "interrupt_for_session: %s interrupt failed: %s",
                    r.get("delegation_id"), exc,
                )
    if count:
        logger.info(
            "Interrupted %d async delegation(s) for ending session (%s)",
            count, reason,
        )
    return count


def _reset_for_tests() -> None:
    """Test-only: clear all state and tear down the executor + monitor."""
    global _executor, _executor_max_workers, _monitor_thread
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=False)
        _executor = None
        _executor_max_workers = 0
    _monitor_stop.set()
    with _monitor_lock:
        thread = _monitor_thread
        _monitor_thread = None
    if thread is not None and thread.is_alive():
        thread.join(timeout=2)
    with _records_lock:
        _records.clear()
    with _aggregate_enqueue_lock:
        _aggregate_enqueued_delivery_ids.clear()
