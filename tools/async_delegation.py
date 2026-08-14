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

Governing delivery invariant
----------------------------
The durable SQLite rows are the AUTHORITATIVE delivery state; the in-memory
``completion_queue`` is only a low-latency wake. Every persisted completion
whose ``delivery_state`` is ``pending`` must converge — during live operation
(``sweep_undelivered_completions``, driven by the gateway watcher), not just
after a process restart (``restore_undelivered_completions``) — to exactly one
of: ``delivered`` (one accepted user turn), ``dropped`` (attempt cap,
staleness cap, or permanently-gone target), or ``superseded`` (an explicitly
declared newer stream event was delivered first). A lost or dropped queue wake
therefore delays delivery by at most a few seconds; it can never lose the
completion. Duplicate queue copies of one completion are legal — the atomic
per-row delivery claim guarantees at most one user turn per completion per
consumer lifecycle, and a refused duplicate claim does not burn the retry
budget.

Producers other than ``delegate_task`` (plugins, service integrations) must
use :func:`publish_background_notification` — the supported persist+wake API —
instead of reaching into the private ``_persist_*`` helpers. It reuses this
same durable table and delivery rail; there is no second ledger.
"""

from __future__ import annotations

import json
import logging
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
# A delivery claim older than this is considered abandoned and may be
# re-claimed by another consumer (see claim_completion_delivery).
_DELIVERY_CLAIM_TTL_S = 300.0
_DB_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Live durable-truth sweep (lost-wake recovery without a restart)
# ---------------------------------------------------------------------------
# The in-memory completion_queue wake can be lost (queue.put raced a consumer
# crash, a producer in this process persisted the row but its wake never
# happened, a swept copy was dropped mid-flight). Production evidence: two
# durable rows sat delivery_state='pending' with delivery_attempts=0 for ~21h
# until a gateway restart, because restore_undelivered_completions only runs
# in the ProcessRegistry constructor. The live sweep makes the durable store
# authoritative WHILE the process is running.
#
# Timing contract: these are operational notifications (a 1-minute ETA cannot
# spend a quarter of its lifetime waiting on recovery). A lost wake must be
# rediscovered within single-digit seconds: grace + the ~2s gateway watcher
# tick bounds first recovery at ~5s, and a swept wake that is itself lost is
# re-armed after _SWEEP_REWAKE_INTERVAL_S, bounding repeat recovery the same
# way. Tests may patch these lower; production values must keep the
# single-digit-seconds bound.
_SWEEP_MIN_PENDING_AGE_S = 3.0  # grace before a pending row counts as "wake lost"
_SWEEP_REWAKE_INTERVAL_S = 5.0  # min spacing between re-wakes of the SAME row
# recover_abandoned_delegations() probes owner PIDs; throttle it inside the
# high-frequency sweep so liveness probing stays cheap.
_SWEEP_ABANDONED_RECOVERY_INTERVAL_S = 30.0
_last_abandoned_recovery_at = 0.0

# Process-global SQLite scan throttle (monotonic clock), separate from the
# per-ID re-wake tracker above. Sweep entry points are per-consumer and can be
# hot — the gateway watcher ticks every ~2s, the CLI drains per idle loop, and
# a multi-session Desktop runs ONE TUI poller per session at 0.5s each — so an
# unthrottled sweep would multiply identical DB scans by consumer count.
# Every periodic entry point shares this one gate: N pollers inside one
# interval trigger exactly one scan. Explicit callers (tests, one-shot
# recovery) can bypass with ``force_scan=True``. The interval matches the
# gateway tick so throttling never worsens the single-digit-seconds recovery
# bound.
_SWEEP_SCAN_INTERVAL_S = 2.0
_sweep_scan_lock = threading.Lock()
_last_sweep_scan_monotonic = float("-inf")

# delegation_id -> last wake/claim activity timestamp (monotonic wall clock).
# Precise queued-ID tracking: an entry means "a queue copy for this row was
# enqueued, or a consumer claimed it, within the last re-arm window", so the
# sweep does not flood the queue with copies of a row that is already in
# flight. Entries are cleared on terminal transitions and expire after the
# re-arm window — a swept wake that is itself lost is re-woken on the next
# sweep past that window, never suppressed for a blind minute.
_wake_tracker_lock = threading.Lock()
_recent_wake_activity: Dict[str, float] = {}
_WAKE_TRACKER_PRUNE_AGE_S = 600.0


def _note_wake_activity(delegation_id: str, ts: Optional[float] = None) -> None:
    if not delegation_id:
        return
    with _wake_tracker_lock:
        _recent_wake_activity[delegation_id] = ts if ts is not None else time.time()


def _clear_wake_activity(delegation_id: str) -> None:
    if not delegation_id:
        return
    with _wake_tracker_lock:
        _recent_wake_activity.pop(delegation_id, None)


def _wake_recently_active(delegation_id: str, now: float) -> bool:
    with _wake_tracker_lock:
        last = _recent_wake_activity.get(delegation_id)
    return last is not None and (now - last) < _SWEEP_REWAKE_INTERVAL_S


def _prune_wake_tracker(now: float) -> None:
    with _wake_tracker_lock:
        stale = [
            key for key, ts in _recent_wake_activity.items()
            if now - ts > _WAKE_TRACKER_PRUNE_AGE_S
        ]
        for key in stale:
            _recent_wake_activity.pop(key, None)


def _owner_permits_adoption(owner_pid, owner_started_at) -> bool:
    """Whether THIS process may adopt a durable row's delivery.

    One ownership predicate shared by boot restore and the live sweep, so the
    two recovery paths cannot drift: adoptable when the row has no recorded
    owner, is owned by this process, or is owned by a provably dead process /
    incarnation (recycled PID detected via the persisted start time). NOT
    adoptable when the recorded owner is a live foreign process — e.g. a TUI
    or sibling gateway sharing this state.db — whose own drain/restore owns
    the row; adopting it here would steal routing and burn its delivery
    attempts (#64484-class). Unverifiable liveness fails closed.
    """
    if not owner_pid:
        return True
    owner_pid = int(owner_pid)
    if owner_pid == __import__("os").getpid():
        return True
    try:
        from gateway.status import _pid_exists, get_process_start_time
    except Exception:
        return False  # cannot prove the owner is dead — leave the row alone
    if not _pid_exists(owner_pid):
        return True
    if owner_started_at is not None and (
        get_process_start_time(owner_pid) != int(owner_started_at)
    ):
        return True  # recycled PID: the owning incarnation is gone
    return False

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
    from hermes_state import apply_wal_with_fallback

    apply_wal_with_fallback(conn, db_label="state.db (async_delegation)")
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
            origin_session_id TEXT NOT NULL DEFAULT ''
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
        # Optional producer stream contract (publish_background_notification):
        # rows sharing a stream_id with a monotonic stream_seq are delivered
        # low→high; supersedes_before declares that THIS event, once
        # delivered, makes every pending event in the stream with
        # stream_seq < supersedes_before obsolete ('superseded'). Ordering
        # and supersession are distinct: sequence alone never drops events.
        ("stream_id", "TEXT"),
        ("stream_seq", "INTEGER"),
        ("supersedes_before", "INTEGER"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE async_delegations ADD COLUMN {name} {sql_type}")


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
        conn.execute("DELETE FROM async_delegations WHERE delegation_id=?", (delegation_id,))


def _prune_durable_records() -> None:
    """Bound delivery-terminal history; NEVER delete pending durable truth.

    The retention caps apply only to rows whose delivery already reached a
    terminal disposition (delivered / dropped / superseded) — history, not
    obligations. A completed-but-undelivered row is the authoritative record
    of a delivery the user is still owed; deleting it (the pre-fix behavior:
    the 50-row history cap counted every non-running row, so publishing 51
    undelivered notifications silently deleted the oldest) is data loss.
    When the pending backlog exceeds ``_MAX_DURABLE_PENDING``, overflow rows
    are explicitly TRANSITIONED to 'dropped' (payload still queryable, loss
    logged) rather than deleted.
    """
    now = time.time()
    cutoff = now - _DURABLE_RETENTION_SECONDS
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            "DELETE FROM async_delegations WHERE delivery_state='delivered' AND updated_at < ?",
            (cutoff,),
        )
        terminal_count = conn.execute(
            """SELECT COUNT(*) FROM async_delegations
               WHERE delivery_state IN ('delivered','dropped','superseded')"""
        ).fetchone()[0]
        excess = max(0, terminal_count - _MAX_RETAINED_COMPLETED)
        if excess:
            conn.execute(
                """DELETE FROM async_delegations WHERE delegation_id IN (
                     SELECT delegation_id FROM async_delegations
                     WHERE delivery_state IN ('delivered','dropped','superseded')
                     ORDER BY CASE delivery_state WHEN 'delivered' THEN 0 ELSE 1 END,
                              updated_at ASC LIMIT ?
                   )""",
                (excess,),
            )
        pending_count = conn.execute(
            """SELECT COUNT(*) FROM async_delegations
               WHERE state NOT IN ('running','finalizing') AND delivery_state='pending'"""
        ).fetchone()[0]
        overflow = max(0, pending_count - _MAX_DURABLE_PENDING)
        if overflow:
            overflow_ids = [
                r[0] for r in conn.execute(
                    """SELECT delegation_id FROM async_delegations
                       WHERE state NOT IN ('running','finalizing') AND delivery_state='pending'
                       ORDER BY updated_at ASC LIMIT ?""",
                    (overflow,),
                ).fetchall()
            ]
            conn.execute(
                f"""UPDATE async_delegations SET delivery_state='dropped',
                           delivery_claim=NULL, delivery_claimed_at=NULL,
                           updated_at=?
                    WHERE delegation_id IN ({','.join('?' * len(overflow_ids))})""",
                (now, *overflow_ids),
            )
            logger.warning(
                "Async delegation pending backlog exceeded %d rows; %d oldest "
                "undelivered completion(s) transitioned to terminal 'dropped' "
                "(payloads remain queryable): %s",
                _MAX_DURABLE_PENDING, len(overflow_ids),
                ", ".join(overflow_ids[:10]),
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

    Ownership: rows recorded as owned by a provably LIVE foreign process
    (``_owner_permits_adoption``, shared with the live sweep) are left alone
    — a gateway booting next to a live TUI must not steal the TUI's pending
    completions or burn their delivery attempts.
    """
    recover_abandoned_delegations()
    now = time.time()
    restored = 0
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            """SELECT delegation_id, event_json, completed_at, dispatched_at,
                      owner_pid, owner_started_at
               FROM async_delegations
               WHERE state != 'running' AND delivery_state='pending' AND event_json IS NOT NULL
               ORDER BY completed_at, delegation_id"""
        ).fetchall()
        for (delegation_id, payload, completed_at, dispatched_at,
             owner_pid, owner_started_at) in rows:
            if not _owner_permits_adoption(owner_pid, owner_started_at):
                continue
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
            _note_wake_activity(delegation_id, now)
            restored += 1
    return restored


def sweep_undelivered_completions(
    target_queue, *, now: Optional[float] = None, force_scan: bool = False
) -> int:
    """Re-wake due pending durable completions during LIVE operation.

    Called periodically by every live consumer — the gateway
    async-delegation watcher tick, the CLI idle drain, and each Desktop/TUI
    session's notification poller. Makes the durable store authoritative
    while the process runs: a completion whose in-memory queue wake was
    missed or lost is rediscovered here within single-digit seconds and
    re-enters the exact same delivery rail (enqueue → claim → inject →
    ack/release) instead of waiting for a process restart.

    Scan cost: the periodic entry points share ONE process-global monotonic
    throttle (``_SWEEP_SCAN_INTERVAL_S``) — N concurrent pollers inside an
    interval trigger exactly one SQLite scan; throttled calls return 0
    without touching the DB. ``force_scan=True`` bypasses the throttle for
    explicit one-shot callers (tests, manual recovery). The per-ID re-wake
    tracker below is separate and governs how often one ROW may be re-woken.

    Due = ``delivery_state='pending'`` with a persisted event, no active
    delivery claim (an in-flight consumer owns claimed rows), quiet for at
    least ``_SWEEP_MIN_PENDING_AGE_S`` (grace so the normal immediate wake
    wins the common case), and no wake/claim activity recorded within
    ``_SWEEP_REWAKE_INTERVAL_S`` (precise queued-ID tracking — duplicates are
    claim-safe, but the sweep still must not flood the queue while a copy is
    already in flight). Rows are enqueued deterministically in occurrence
    order (``completed_at, delegation_id``); opt-in stream ordering and
    supersession are enforced downstream at the claim chokepoint.

    Ownership: only rows owned by THIS process, by a dead process, or with no
    recorded owner are swept. A pending row owned by a live sibling process
    (e.g. a TUI sharing the same state.db) belongs to that process's own
    drain — adopting it here would steal routing (#64484-class). Every swept
    event is stamped ``restored=True`` for the same reason boot-restored
    events are: it is a durable replay, and an unfiltered legacy drain must
    not adopt another session's addressed event; consumers with positive
    ownership proof (gateway routing, TUI/CLI ownership callbacks) still
    consume it normally.

    Shares the restore path's terminal hygiene: rows past the 48h staleness
    cap are terminally dropped here too, and abandoned 'running' rows of dead
    owners are classified (throttled) so live operation gets the same crash
    recovery a restart would perform.
    """
    global _last_abandoned_recovery_at, _last_sweep_scan_monotonic
    mono = time.monotonic()
    with _sweep_scan_lock:
        if not force_scan and (
            mono - _last_sweep_scan_monotonic < _SWEEP_SCAN_INTERVAL_S
        ):
            return 0
        _last_sweep_scan_monotonic = mono
    now = time.time() if now is None else now
    if now - _last_abandoned_recovery_at >= _SWEEP_ABANDONED_RECOVERY_INTERVAL_S:
        _last_abandoned_recovery_at = now
        try:
            recover_abandoned_delegations()
        except Exception:  # noqa: BLE001 — recovery is additive, never fatal
            logger.debug("Sweep abandoned-delegation recovery failed", exc_info=True)
    _prune_wake_tracker(now)
    enqueued = 0
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            """SELECT delegation_id, event_json, completed_at, dispatched_at,
                      owner_pid, owner_started_at
               FROM async_delegations
               WHERE state != 'running' AND delivery_state='pending'
                 AND event_json IS NOT NULL AND updated_at <= ?
                 AND (delivery_claim IS NULL OR delivery_claimed_at < ?)
               ORDER BY completed_at, delegation_id""",
            (now - _SWEEP_MIN_PENDING_AGE_S, now - _DELIVERY_CLAIM_TTL_S),
        ).fetchall()
        for (delegation_id, payload, completed_at, dispatched_at,
             owner_pid, owner_started_at) in rows:
            if not _owner_permits_adoption(owner_pid, owner_started_at):
                continue  # a live sibling process owns this row
            age_basis = completed_at or dispatched_at
            if age_basis and (now - age_basis) > _MAX_COMPLETION_REPLAY_AGE_S:
                conn.execute(
                    """UPDATE async_delegations SET delivery_state='dropped',
                              delivery_claim=NULL, delivery_claimed_at=NULL,
                              updated_at=?
                       WHERE delegation_id=? AND delivery_state='pending'""",
                    (now, delegation_id),
                )
                _clear_wake_activity(delegation_id)
                logger.warning(
                    "Async delegation %s: pending completion is %.1fh old "
                    "(cap %.1fh); terminally dropping the live re-wake "
                    "(result remains queryable).",
                    delegation_id, (now - age_basis) / 3600.0,
                    _MAX_COMPLETION_REPLAY_AGE_S / 3600.0,
                )
                continue
            if _wake_recently_active(delegation_id, now):
                continue  # a copy is already queued/claimed — no flood
            evt = json.loads(payload)
            if isinstance(evt, dict):
                # Durable replay: same fail-closed ownership stamp as boot
                # restore, so unfiltered legacy drains leave it for its owner.
                evt["restored"] = True
            target_queue.put(evt)
            _note_wake_activity(delegation_id, now)
            enqueued += 1
            logger.info(
                "Async delegation %s: durable pending completion had no live "
                "queue wake; re-woken by the durable sweep (%.1fs after last "
                "activity).",
                delegation_id,
                max(0.0, now - (age_basis or now)),
            )
    return enqueued


def mark_completion_delivered(delegation_id: str) -> bool:
    """Atomically acknowledge successful injection of a durable completion."""
    now = time.time()
    superseded: List[str] = []
    with _DB_LOCK, _transaction() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_state='delivered', delivered_at=?, updated_at=?
               WHERE delegation_id=? AND delivery_state!='delivered'""",
            (now, now, delegation_id),
        )
        delivered = cur.rowcount == 1
        if delivered:
            superseded = _apply_supersession_watermark_locked(conn, delegation_id, now)
    if delivered:
        _clear_wake_activity(delegation_id)
        for sid in superseded:
            _clear_wake_activity(sid)
    return delivered


def _stream_superseded_locked(
    conn: sqlite3.Connection, delegation_id: str, stream_id: str, stream_seq: int
) -> bool:
    """Whether a DELIVERED stream event's watermark covers this sequence."""
    return conn.execute(
        """SELECT 1 FROM async_delegations
           WHERE stream_id=? AND delegation_id!=? AND delivery_state='delivered'
             AND supersedes_before IS NOT NULL AND supersedes_before > ?
           LIMIT 1""",
        (stream_id, delegation_id, stream_seq),
    ).fetchone() is not None


def _stream_order_blocked_locked(
    conn: sqlite3.Connection,
    delegation_id: str,
    stream_id: str,
    stream_seq: int,
    supersedes_before: Optional[int],
    now: float,
) -> bool:
    """Whether an undelivered lower-sequence stream sibling blocks this row.

    Opt-in strict stream order, enforced at the claim chokepoint (queue wakes
    can race and present a higher sequence first — deterministic sweep
    ordering alone cannot guarantee order). A sequenced row may not deliver
    while any nonterminal (still-pending, including claimed/running) row with
    a lower sequence remains in the stream, with ONE carve-out: a lower row
    covered by this event's own ``supersedes_before`` watermark is scheduled
    for supersession rather than delivery and does not gate the superseding
    event — UNLESS that covered row holds a live (unexpired) delivery claim.
    A live claim means another consumer may already be injecting the older
    event's user turn; the superseding event must defer until that claim
    completes (old→terminal order stays honest) or releases (the terminal
    then retires it), never race past it.
    """
    return conn.execute(
        """SELECT 1 FROM async_delegations
           WHERE stream_id=? AND delegation_id!=? AND stream_seq IS NOT NULL
             AND stream_seq < ? AND delivery_state='pending'
             AND (
                   (? IS NULL OR stream_seq >= ?)
                   OR (delivery_claim IS NOT NULL AND delivery_claimed_at >= ?)
                 )
           LIMIT 1""",
        (stream_id, delegation_id, stream_seq, supersedes_before,
         supersedes_before, now - _DELIVERY_CLAIM_TTL_S),
    ).fetchone() is not None


def _covered_by_live_superseding_claim_locked(
    conn: sqlite3.Connection,
    delegation_id: str,
    stream_id: str,
    stream_seq: int,
    now: float,
) -> bool:
    """Whether a live-claimed superseding sibling covers this row.

    The symmetric half of the claimed-old-vs-terminal protocol: while a
    pending stream sibling whose ``supersedes_before`` watermark covers this
    row holds a live (unexpired) delivery claim, this row must defer — if it
    claimed and delivered now, the in-flight superseding event's acceptance
    would immediately obsolete a turn the user just received (or the ack-time
    retirement would have to rewrite a live claim, which is forbidden).
    After the superseding claim completes, this row is retired as superseded;
    after it releases, this row claims normally.
    """
    return conn.execute(
        """SELECT 1 FROM async_delegations
           WHERE stream_id=? AND delegation_id!=? AND delivery_state='pending'
             AND delivery_claim IS NOT NULL AND delivery_claimed_at >= ?
             AND supersedes_before IS NOT NULL AND supersedes_before > ?
           LIMIT 1""",
        (stream_id, delegation_id, now - _DELIVERY_CLAIM_TTL_S, stream_seq),
    ).fetchone() is not None


def _apply_supersession_watermark_locked(
    conn: sqlite3.Connection,
    delegation_id: str,
    now: float,
) -> List[str]:
    """After delivering ``delegation_id``, retire the stream rows it supersedes.

    Returns the superseded delegation ids (for wake-tracker cleanup). Only
    rows the producer explicitly declared obsolete (stream_seq strictly below
    the delivered event's ``supersedes_before`` watermark) are touched;
    plain sequenced rows without a watermark never retire siblings.

    A covered row holding a LIVE (unexpired) delivery claim is never
    rewritten — its consumer may be mid-injection, and yanking the row out
    from under an in-flight claim would fake a 'superseded' disposition for
    a turn the user actually received. (The claim gates normally prevent a
    live covered claim from coexisting with a granted superseding claim;
    this filter is the transactional backstop for claim-TTL expiry races.)
    Such a row is retired later, by the delivered-watermark check on its
    next claim attempt.
    """
    meta = conn.execute(
        """SELECT stream_id, supersedes_before FROM async_delegations
           WHERE delegation_id=?""",
        (delegation_id,),
    ).fetchone()
    if not meta or not meta[0] or meta[1] is None:
        return []
    stream_id, watermark = meta
    superseded = [
        r[0] for r in conn.execute(
            """SELECT delegation_id FROM async_delegations
               WHERE stream_id=? AND delegation_id!=? AND stream_seq IS NOT NULL
                 AND stream_seq < ? AND delivery_state='pending'
                 AND (delivery_claim IS NULL OR delivery_claimed_at < ?)""",
            (stream_id, delegation_id, watermark, now - _DELIVERY_CLAIM_TTL_S),
        ).fetchall()
    ]
    if superseded:
        conn.execute(
            f"""UPDATE async_delegations SET delivery_state='superseded',
                       delivery_claim=NULL, delivery_claimed_at=NULL, updated_at=?
                WHERE delegation_id IN ({','.join('?' * len(superseded))})""",
            (now, *superseded),
        )
        logger.info(
            "Stream %s: %d pending event(s) superseded by delivered watermark "
            "of %s: %s",
            stream_id, len(superseded), delegation_id, ", ".join(superseded),
        )
    return superseded


def claim_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Claim one pending completion across competing consumers/processes.

    This is the single delivery chokepoint — immediate wakes, the live
    durable sweep, and restart restore all pass through it, so duplicate
    queue copies of one completion collapse to at most one granted claim.
    A refused claim never increments the delivery-attempt budget.

    Sequenced rows (optional producer stream contract) are additionally
    gated here: a row superseded by an already-delivered watermark is
    terminally retired instead of claimed, and an in-order row is refused
    while an undelivered lower sequence in its stream remains (see
    ``_stream_order_blocked_locked``).
    """
    now = time.time()
    granted = False
    with _DB_LOCK, _transaction() as conn:
        row = conn.execute(
            """SELECT delivery_state, stream_id, stream_seq, supersedes_before
               FROM async_delegations WHERE delegation_id=?""",
            (delegation_id,),
        ).fetchone()
        if row is None:
            return True  # legacy event created before durable dispatch
        delivery_state, stream_id, stream_seq, supersedes_before = row
        if delivery_state == "pending" and stream_id and stream_seq is not None:
            if _stream_superseded_locked(conn, delegation_id, stream_id, stream_seq):
                conn.execute(
                    """UPDATE async_delegations SET delivery_state='superseded',
                              delivery_claim=NULL, delivery_claimed_at=NULL,
                              updated_at=?
                       WHERE delegation_id=? AND delivery_state='pending'""",
                    (now, delegation_id),
                )
                logger.info(
                    "Stream %s: refusing delivery of %s (seq %s) — a delivered "
                    "event's supersession watermark already covers it.",
                    stream_id, delegation_id, stream_seq,
                )
                _clear_wake_activity(delegation_id)
                return False
            if _covered_by_live_superseding_claim_locked(
                conn, delegation_id, stream_id, stream_seq, now
            ) or _stream_order_blocked_locked(
                conn, delegation_id, stream_id, stream_seq, supersedes_before, now
            ):
                # DEFERRED, not failed: no attempt burned, row stays pending.
                # The durable sweep re-wakes it once the blocking sibling's
                # claim resolves (completes, releases, or expires).
                _note_wake_activity(delegation_id, now)
                return False
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_claim=?, delivery_claimed_at=?,
                      delivery_attempts=delivery_attempts+1, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'
                 AND (delivery_claim IS NULL OR delivery_claimed_at < ?)""",
            (claim_id, now, now, delegation_id, now - _DELIVERY_CLAIM_TTL_S),
        )
        granted = cur.rowcount == 1
    # A claim attempt (granted or raced) proves a queue copy is in flight in
    # some consumer right now — the sweep need not add another for a re-arm
    # window.
    _note_wake_activity(delegation_id, now)
    return granted


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
                 AND delivery_claim=? AND delivery_attempts>=?""",
            (now, delegation_id, claim_id, _MAX_DELIVERY_ATTEMPTS),
        )
        if capped.rowcount == 1:
            logger.warning(
                "Async delegation %s exhausted its %d delivery attempts; "
                "marking terminally dropped (result remains queryable).",
                delegation_id, _MAX_DELIVERY_ATTEMPTS,
            )
            _clear_wake_activity(delegation_id)
            return True
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_claim=NULL,
                      delivery_claimed_at=NULL, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=?""",
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
                 AND delivery_claim=?""",
            (now, delegation_id, claim_id),
        )
        dropped = cur.rowcount == 1
    if dropped:
        _clear_wake_activity(delegation_id)
    return dropped


def complete_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Acknowledge acceptance for the consumer holding this claim.

    If the delivered event declared a supersession watermark
    (``supersedes_before``), every pending event in its stream below that
    watermark is atomically retired as ``superseded`` in the same
    transaction — a delivered terminal/superseding event guarantees the
    older events it obsoletes can never surface afterwards.
    """
    now = time.time()
    superseded: List[str] = []
    with _DB_LOCK, _transaction() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_state='delivered',
                      delivered_at=?, updated_at=?, delivery_claim=NULL,
                      delivery_claimed_at=NULL
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=?""",
            (now, now, delegation_id, claim_id),
        )
        delivered = cur.rowcount == 1
        if delivered:
            superseded = _apply_supersession_watermark_locked(conn, delegation_id, now)
    if delivered:
        _clear_wake_activity(delegation_id)
        for sid in superseded:
            _clear_wake_activity(sid)
    return delivered


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


# The fields an idempotent notification_id binds immutably. Timestamps are
# excluded on purpose: a producer retry regenerates "now" defaults, and the
# durable row's first-publish times remain the truth.
_IMMUTABLE_NOTIFICATION_FIELDS = (
    "summary", "goal", "context", "status",
    "session_key", "origin_ui_session_id", "origin_session_id",
    "parent_session_id",
    "stream_id", "sequence", "supersedes_before_sequence",
)


def _notification_payload_conflicts(
    persisted_evt: Dict[str, Any], new_evt: Dict[str, Any]
) -> List[str]:
    """Names of immutable notification fields that differ, empty when none."""
    return [
        field
        for field in _IMMUTABLE_NOTIFICATION_FIELDS
        if persisted_evt.get(field) != new_evt.get(field)
    ]


def _stream_position_verdict_locked(
    conn: sqlite3.Connection,
    notification_id: str,
    stream_id: str,
    sequence: int,
) -> tuple:
    """Publish-time stream contract checks for a NEW sequenced row.

    Returns ``(verdict, detail)``:

    - ``("conflict", msg)`` — the (stream_id, sequence) slot is already
      taken by a different notification_id. A sequence is a stable position;
      two producers (or a buggy retry with a new id) claiming the same slot
      is a contract violation, refused before any durable write.
    - ``("superseded", None)`` — an already-DELIVERED watermark covers this
      late-published sequence; the caller persists it born-terminal.
    - ``("rejected", msg)`` — a strictly higher sequence in the stream was
      already delivered without a covering watermark. Delivering this row
      now would violate the low→high order the stream declared, and claim
      ordering can no longer repair it (the higher turn already reached the
      user). Refused with no durable write. Late lower sequences remain
      legal while every higher sibling is still pending — claim ordering
      then delivers low→high.
    - ``(None, None)`` — position is valid; proceed.
    """
    duplicate = conn.execute(
        """SELECT delegation_id FROM async_delegations
           WHERE stream_id=? AND stream_seq=? AND delegation_id!=?
           LIMIT 1""",
        (stream_id, sequence, notification_id),
    ).fetchone()
    if duplicate:
        return (
            "conflict",
            f"sequence {sequence} in stream '{stream_id}' is already bound "
            f"to notification '{duplicate[0]}'",
        )
    if conn.execute(
        """SELECT 1 FROM async_delegations
           WHERE stream_id=? AND delivery_state='delivered'
             AND supersedes_before IS NOT NULL AND supersedes_before > ?
           LIMIT 1""",
        (stream_id, sequence),
    ).fetchone():
        return ("superseded", None)
    delivered_higher = conn.execute(
        """SELECT delegation_id, stream_seq FROM async_delegations
           WHERE stream_id=? AND stream_seq IS NOT NULL AND stream_seq > ?
             AND delivery_state='delivered'
           LIMIT 1""",
        (stream_id, sequence),
    ).fetchone()
    if delivered_higher:
        return (
            "rejected",
            f"out-of-order publication: sequence {delivered_higher[1]} "
            f"('{delivered_higher[0]}') in stream '{stream_id}' was already "
            f"delivered; a late lower sequence {sequence} can no longer be "
            "delivered in order",
        )
    return (None, None)


def publish_background_notification(
    *,
    summary: str,
    session_key: str = "",
    title: str = "",
    notification_id: Optional[str] = None,
    status: str = "completed",
    parent_session_id: Optional[str] = None,
    origin_ui_session_id: str = "",
    origin_session_id: str = "",
    context: Optional[str] = None,
    occurred_at: Optional[float] = None,
    stream_id: str = "",
    sequence: Optional[int] = None,
    supersedes_before_sequence: Optional[int] = None,
) -> Dict[str, Any]:
    """Persist + wake one background notification (supported producer API).

    This is the public rail for plugins and service integrations that need a
    durable, restart-surviving notification to re-enter a conversation as a
    fresh agent turn — instead of importing the private ``_persist_dispatch``
    / ``_persist_completion`` helpers or inventing a second ledger. The
    notification reuses the async-delegation durable table and the exact
    delivery pipeline (queue wake → live durable sweep → atomic claim →
    inject → ack/release), so it inherits crash recovery, routing ownership,
    retry caps, the 48h staleness cap, and message-role-alternation-safe
    injection for free.

    Parameters
    ----------
    summary
        The notification body the agent receives. Required, non-empty.
    session_key / parent_session_id / origin_ui_session_id / origin_session_id
        Routing, identical semantics to delegation completions: the gateway
        parses ``session_key``; ``parent_session_id`` pins the spawning
        durable session (enables the compression-continuation resolver and
        the user-boundary terminal drop); ``origin_session_id`` is the raw
        api_server wake target; ``origin_ui_session_id`` binds TUI/desktop
        ownership.
    title / context / status
        Rendered in the re-injection block. ``status`` is free-form producer
        state (e.g. ``"completed"``, ``"driver_assigned"``).
    notification_id
        Stable producer identity (e.g. ``"chauffeur/<ride>/13"``). Publishing
        is idempotent per id, and the id binds IMMUTABLE content: 
        re-publishing a still-pending id with identical payload/routing/
        stream fields only re-wakes the persisted event (the durable truth is
        the only payload ever enqueued); divergent immutable fields return
        ``{"status": "conflict"}`` without touching the row; an id that
        already reached a terminal delivery state is a no-op returning
        ``{"status": "duplicate"}``. Auto-generated when omitted.
    occurred_at
        Event occurrence time (defaults to now). Drives deterministic
        recovery ordering, the staleness cap, and honest delivery-age
        rendering.
    stream_id / sequence / supersedes_before_sequence
        OPTIONAL producer stream contract. Ordering and supersession are
        distinct and both opt-in:

        - ``stream_id`` + monotonic ``sequence`` declare an ORDERED stream:
          events deliver strictly low→high (enforced at the claim
          chokepoint, so racing immediate wakes cannot deliver a higher
          sequence past an undelivered lower one). Nothing is ever dropped
          by sequencing alone.
        - ``supersedes_before_sequence`` additionally declares that THIS
          event makes every pending stream event with
          ``sequence < supersedes_before_sequence`` obsolete. Once this
          event is delivered, those           older events are retired as
          ``superseded`` and can never surface afterwards. A terminal event
          may pass its own sequence (supersede everything before it); a
          latest-wins stream may do so on every event. The watermark may not
          exceed the event's own sequence (no obsoleting future events).

        A sequence is a stable stream position: publishing a second
        notification_id onto an occupied ``(stream_id, sequence)`` slot is a
        ``conflict``; publishing a lower sequence after a higher one was
        already DELIVERED is ``rejected`` as out-of-order (or persisted
        born-``superseded`` when a delivered watermark covers it). Lower
        sequences published while higher siblings are still pending are
        fine — claim ordering delivers low→high.

    Returns ``{"status": "published"|"republished"|"duplicate"|"conflict"|
    "rejected"|"superseded", "notification_id": ..., ["error": ...]}``.
    """
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("summary must be a non-empty string")
    if (stream_id and sequence is None) or (sequence is not None and not stream_id):
        raise ValueError("stream_id and sequence must be provided together")
    if supersedes_before_sequence is not None and sequence is None:
        raise ValueError(
            "supersedes_before_sequence requires stream_id and sequence"
        )
    if sequence is not None:
        sequence = int(sequence)
    if supersedes_before_sequence is not None:
        supersedes_before_sequence = int(supersedes_before_sequence)
        if supersedes_before_sequence > sequence:
            # A watermark above the event's own sequence would obsolete FUTURE
            # events the producer hasn't emitted yet.
            raise ValueError(
                "supersedes_before_sequence must be <= sequence "
                f"({supersedes_before_sequence} > {sequence})"
            )
    notification_id = notification_id or f"notif_{uuid.uuid4().hex[:12]}"
    now = time.time()
    occurred_at = float(occurred_at) if occurred_at is not None else now

    evt: Dict[str, Any] = {
        "type": "async_delegation",
        "kind": "notification",
        "delegation_id": notification_id,
        "session_key": session_key or "",
        "origin_ui_session_id": origin_ui_session_id or "",
        "origin_session_id": origin_session_id or "",
        "parent_session_id": parent_session_id,
        "goal": title or "",
        "context": context,
        "status": status,
        "summary": summary,
        "error": None,
        "dispatched_at": occurred_at,
        "completed_at": occurred_at,
        **_capture_routing_origin(),
    }
    if stream_id:
        evt["stream_id"] = stream_id
        evt["sequence"] = sequence
        if supersedes_before_sequence is not None:
            evt["supersedes_before_sequence"] = supersedes_before_sequence
    result = {"status": status, "summary": summary}

    try:
        from gateway.status import get_process_start_time
        owner_started_at = get_process_start_time(__import__("os").getpid())
    except Exception:
        owner_started_at = None
    task_payload = {
        key: evt[key]
        for key in ("goal", "context", "scope_id", "user_id", "user_name")
        if evt.get(key)
    }

    outcome = "published"
    wake_evt = evt
    with _DB_LOCK, _transaction() as conn:
        existing = conn.execute(
            """SELECT delivery_state, event_json FROM async_delegations
               WHERE delegation_id=?""",
            (notification_id,),
        ).fetchone()
        if existing is not None:
            if existing[0] != "pending":
                # Terminal (delivered/dropped/superseded): idempotent no-op —
                # a producer retry must never resurrect a finished delivery.
                return {"status": "duplicate", "notification_id": notification_id}
            # An idempotency key binds IMMUTABLE content. Re-publishing a
            # still-pending id may only refresh its wake — and the wake must
            # carry the PERSISTED payload, never the retry's: enqueueing new
            # content against old durable truth splits the brain (immediate
            # delivery shows the new summary/routing, a sweep re-wake of the
            # same row delivers the old one). Divergent immutable fields are
            # an explicit conflict, not a silent overwrite.
            try:
                persisted_evt = json.loads(existing[1] or "{}")
            except Exception:
                persisted_evt = {}
            conflicts = _notification_payload_conflicts(persisted_evt, evt)
            if conflicts:
                logger.warning(
                    "Background notification %s republished with divergent "
                    "immutable content (%s); refusing the conflicting payload.",
                    notification_id, ", ".join(conflicts),
                )
                return {
                    "status": "conflict",
                    "notification_id": notification_id,
                    "error": (
                        "notification_id already bound to different content: "
                        + ", ".join(conflicts)
                    ),
                }
            wake_evt = persisted_evt
            outcome = "republished"
        else:
            initial_delivery_state = "pending"
            if stream_id:
                verdict, detail = _stream_position_verdict_locked(
                    conn, notification_id, stream_id, sequence
                )
                if verdict == "conflict" or verdict == "rejected":
                    logger.warning(
                        "Background notification %s refused (%s): %s",
                        notification_id, verdict, detail,
                    )
                    return {
                        "status": verdict,
                        "notification_id": notification_id,
                        "error": detail,
                    }
                if verdict == "superseded":
                    # Late publication of an event an already-DELIVERED
                    # watermark covers: persist the durable record for
                    # queryability, but born-terminal — no wake, no delivery.
                    initial_delivery_state = "superseded"
            conn.execute(
                """INSERT INTO async_delegations
                   (delegation_id, origin_session, origin_ui_session_id,
                    parent_session_id, state, dispatched_at, completed_at,
                    updated_at, event_json, result_json, delivery_state,
                    delivery_attempts, owner_pid, owner_started_at, task_json,
                    origin_session_id, stream_id, stream_seq, supersedes_before)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)""",
                (notification_id, session_key or "", origin_ui_session_id or "",
                 parent_session_id, status, occurred_at, occurred_at, now,
                 json.dumps(evt), json.dumps(result), initial_delivery_state,
                 __import__("os").getpid(), owner_started_at,
                 json.dumps(task_payload), origin_session_id or "",
                 stream_id or None, sequence, supersedes_before_sequence),
            )
            if initial_delivery_state == "superseded":
                logger.info(
                    "Background notification %s (stream %s seq %s) published "
                    "after a delivered watermark already covers it; persisted "
                    "as superseded without delivery.",
                    notification_id, stream_id, sequence,
                )
                return {
                    "status": "superseded",
                    "notification_id": notification_id,
                }
    _prune_durable_records()

    try:
        from tools.process_registry import process_registry
        process_registry.completion_queue.put(dict(wake_evt))
        _note_wake_activity(notification_id, now)
    except Exception as exc:  # noqa: BLE001 — durable row survives a lost wake
        logger.warning(
            "Background notification %s persisted but its immediate wake "
            "failed (%s); the durable sweep will deliver it.",
            notification_id, exc,
        )
    logger.info(
        "Published background notification %s (session_key=%s, stream=%s seq=%s)",
        notification_id, session_key or "<cli>", stream_id or "-",
        sequence if sequence is not None else "-",
    )
    return {"status": outcome, "notification_id": notification_id}


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
    _persist_completion(evt, result)
    try:
        process_registry.completion_queue.put(evt)
        _note_wake_activity(str(record.get("delegation_id") or ""))
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation %s: failed to enqueue completion event; "
            "the durable sweep will re-wake it: %s",
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
    _persist_completion(evt, combined)
    try:
        process_registry.completion_queue.put(evt)
        _note_wake_activity(str(event_record.get("delegation_id") or ""))
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation batch %s: failed to enqueue completion event; "
            "the durable sweep will re-wake it: %s",
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
    global _last_abandoned_recovery_at
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
    with _wake_tracker_lock:
        _recent_wake_activity.clear()
    _last_abandoned_recovery_at = 0.0
    global _last_sweep_scan_monotonic
    with _sweep_scan_lock:
        _last_sweep_scan_monotonic = float("-inf")
