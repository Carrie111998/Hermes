#!/usr/bin/env python3
"""Orca → Hermes completion bridge.

Orca is the execution plane: it owns runs, worktrees and terminals, and it
finishes work while Hermes is idle.  Without this bridge Hermes only learns
that a run ended on its next poll, so a task that finished in 40 seconds sits
undelivered for minutes.  The bridge gives a LOCAL Orca a loopback door to
knock on.

Three rules make it safe to open that door:

1. **A payload never declares itself complete.**  Every inbound event is a
   *candidate*.  Hermes re-queries Orca by ``run_id`` (``run-show`` +
   ``task-list`` + ``worker-list``) and only Orca's own ledger can move a run
   to completed.  A forged body, a truncated hook, a worker that lies about
   its outcome — none of them can fabricate a completion, because the claim
   in the body is never read as truth.  Only identifiers are read out of the
   payload, and only to look authoritative state up.

2. **Quiet is not done.**  A Claude ``Stop`` hook or an idle TUI means the
   model stopped emitting — mid-task, waiting on approval, or genuinely
   finished; the signal cannot tell you which.  Those kinds are recorded and
   otherwise ignored.  Only ``hermes-ready`` / ``worker_done`` / terminal
   ``exit`` are candidates worth re-querying about.

   ``StopFailure`` deserves its own paragraph, because it is the trap — and
   the trap is subtler than it looks.  ``StopFailure`` is a hook *eventName*:
   in Orca's agent-hook vocabulary it is a turn boundary sitting right next
   to ``Stop``, mapping to the *same* "done" lamp in the UI, and it means the
   agent's stop hook failed.  It is therefore treated exactly like ``Stop``
   — observed, never a completion candidate — because a turn boundary says
   the model stopped emitting and nothing about whether the work is done.

   It is NOT a worker state, and reading it as one was the actual hole.  The
   authoritative ledger is ``worker_dispatches.state``
   (``workerState`` in ``worker-list --json``), whose domain Orca fixes with
   a CHECK constraint: ``starting``, ``ready``, ``start_unknown``, ``failed``,
   ``succeeded``, ``stopping``, ``stop_unknown``, ``stopped``, ``abandoned``
   — plus ``unsupervised``, the ``COALESCE`` sentinel ``worker-list`` reports
   for a dispatch that has no worker ledger row at all.
   Guarding only ``StopFailure`` guarded a string Orca never emits while
   ``failed`` walked straight through and published a success.  So the
   classification is fail-closed on the real domain: only ``succeeded`` (or
   an empty ledger, which just defers to the Task ledger) can complete; every
   settled-but-unsuccessful and every ``*_unknown`` state publishes NOTHING.
   Reporting such a run as complete would tell the owner their work landed
   when the worker fell over on the way out.

3. **Exactly one winner.**  Two distinct events for one run (a
   ``worker_done`` and a terminal ``exit`` racing, or a webhook and the
   recovery sweep) both used to read ``state='open'``, both transition, and
   both publish.  Completion is therefore a single conditional UPDATE whose
   ``rowcount`` elects one caller; every loser returns ``duplicate`` and
   publishes nothing.  Belt and braces: the delegation id is derived from the
   RUN, not the event, so even a lost race cannot mint a second delegation.

The wake itself rides the EXISTING supervisor rail rather than a new one:
:func:`tools.async_delegation.publish_external_completion` writes a durable
terminal row and pushes onto ``process_registry.completion_queue``, which the
gateway's ``_async_delegation_watcher`` already drains, routes by
``session_key``, retries, and acknowledges.  That is what preserves the
originating conversation — a Mattermost thread key captured at registration
is replayed verbatim, so the follow-up lands in the thread that asked for the
work rather than on some inferred default surface.

Downtime is covered by :func:`sweep`, not by webhook retry: if Hermes is down
when Orca finishes, the POST is lost for good.  The sweep re-queries every
still-open run at startup, so recovery does not depend on the sender.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Signal taxonomy
# ---------------------------------------------------------------------------
# Observational only.  These fire whenever the model stops emitting —
# including mid-task, on an approval prompt, and between tool calls.  Treating
# one as a completion is how a half-finished run gets reported as done.
# ``stopfailure``/``stop_failure`` belong HERE, not in the candidate set:
# as an eventName it is the turn boundary whose stop hook failed, which says
# the model stopped emitting and nothing about whether the work is done.
OBSERVE_KINDS = frozenset({
    "stop", "subagentstop", "subagent_stop", "tui-idle", "tui_idle", "idle",
    "notification", "pretooluse", "posttooluse", "permissionrequest",
    "stopfailure", "stop_failure", "stop-failure",
})
# Completion CANDIDATES: worth spending an Orca re-query on.  Still not proof.
CANDIDATE_KINDS = frozenset({
    "hermes-ready", "hermes_ready", "hermesready",
    "worker_done", "worker-done", "workerdone",
    "exit", "terminal_exit", "terminal-exit",
})

# Orca's worker ledger vocabulary.  ``workerState`` in
# ``orca orchestration worker-list --json`` is ``worker_dispatches.state``,
# whose domain is fixed by a CHECK constraint in the installed runtime:
#
#   starting, ready, start_unknown, failed, succeeded,
#   stopping, stop_unknown, stopped, abandoned
#
# ``StopFailure`` is NOT in that domain — it is a hook *eventName* (the
# Claude/Kimi turn boundary that sits next to ``Stop``; Grok spells it
# ``stop_failure``) and it never reaches ``workerState``.  Guarding only that
# spelling is how a worker that really did settle in ``failed`` was reported
# to the owner as a completion.  See classify_worker_state.

# Two values sit OUTSIDE that domain because `worker-list --json` reports
# ``COALESCE(worker_dispatches.state, 'unsupervised')``: a dispatch with no
# worker ledger row at all — a context-only dispatch injected into an
# existing terminal — comes back as ``unsupervised``, and the single-worker
# path spells the same absence ``unknown``.  Orca excludes ``unsupervised``
# from both WORKER_SETTLED_STATES and WORKER_RELEASABLE_STATES for exactly
# that reason.  Neither is an outcome, so both mean "no worker verdict" and
# defer to the Task ledger, precisely like an empty worker list.  (Note this
# is unrelated to ``start_unknown``/``stop_unknown``, which ARE real settled
# states meaning Orca could not confirm a start or a stop — those fail
# closed.)
WORKER_NO_LEDGER_STATES = frozenset({"unsupervised", "unknown"})

# The one and only value that means "this worker finished cleanly".
WORKER_SUCCESS_STATE = "succeeded"
# Not settled either way yet: the run is not finished, and sweep() re-asks.
WORKER_IN_FLIGHT_STATES = frozenset({"starting", "ready", "stopping"})
# Settled but not successful, plus the two ambiguous states Orca writes when
# it could not confirm a start or a stop.  ``StopFailure`` is kept as a
# defensive alias in case a hook ever forwards the eventName spelling
# straight through — it must never be the only guarded value.
TERMINAL_FAILURE_STATES = frozenset({
    "failed", "stopped", "abandoned", "start_unknown", "stop_unknown",
    "StopFailure",
})

# Verdicts returned by classify_worker_state.
WORKER_VERDICT_NONE = "none"
WORKER_VERDICT_SUCCESS = "success"
WORKER_VERDICT_IN_FLIGHT = "in_flight"
WORKER_VERDICT_FAILURE = "failure"


def classify_worker_state(state: str) -> str:
    """Map one Orca ``workerState`` onto the bridge's verdict vocabulary.

    Fail-closed by construction: only the exact string ``succeeded`` earns
    :data:`WORKER_VERDICT_SUCCESS`, and anything Orca reports that is neither
    a known in-flight state nor empty falls to
    :data:`WORKER_VERDICT_FAILURE`.  A value the bridge has never heard of
    (a future Orca state, a hook eventName leaking into the field) therefore
    withholds the completion rather than publishing one.

    ``""`` is deliberately NOT a failure, and neither are Orca's own
    no-ledger sentinels :data:`WORKER_NO_LEDGER_STATES`: a run can have no
    workers at all, or only context-only dispatches, and "Orca kept no worker
    ledger for this run" has to fall through to the Task ledger rather than
    block every completion the bridge exists to deliver.
    """
    normalized = (state or "").strip()
    if not normalized or normalized in WORKER_NO_LEDGER_STATES:
        return WORKER_VERDICT_NONE
    if normalized == WORKER_SUCCESS_STATE:
        return WORKER_VERDICT_SUCCESS
    if normalized in WORKER_IN_FLIGHT_STATES:
        return WORKER_VERDICT_IN_FLIGHT
    return WORKER_VERDICT_FAILURE


# Orca task states that mean "still in flight".  ``blocked`` is deliberately
# in here: a blocked task waits on a decision gate, it is not finished.
_OPEN_TASK_STATES = frozenset({"pending", "ready", "dispatched", "blocked"})
_FAILED_TASK_STATES = frozenset({"failed"})

# Classification vocabulary returned by _classify_transition.
TRANSITION_COMPLETED = "completed"
TRANSITION_TERMINAL_FAILURE = "terminal_failure"
TRANSITION_IN_FLIGHT = "in_flight"
TRANSITION_NONCOMPLETION = "noncompletion"
TRANSITION_UNKNOWN = "unknown"

# Identifier shapes.  Real Orca ids look like ``run_6e33f11c3f86``,
# ``task_9c5dd9be32ef``, ``ctx_79f9dcc59504`` and
# ``term_97ca040f-868b-4d29-af75-10bafd0d3245``.  These values become argv
# elements and primary keys, so the charset stays to something that cannot be
# mistaken for a flag, a path, or shell syntax even if a future caller stops
# using an argv list.
_RUN_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_TERMINAL_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_EVENT_ID_MAX = 128
# A worktree path is only ever echoed back into a report, never opened or
# executed.  Cap it and refuse control characters so it cannot smuggle ANSI
# escapes or newlines into a chat message.
_WORKTREE_MAX = 512
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")

_ORCA_TIMEOUT_SECONDS = 20.0

# Bounded retention.  Both tables are caches of recent activity, not an audit
# log; the durable delegation ledger in tools.async_delegation is what keeps
# the actual result.
_MAX_EVENTS_PER_RUN = 200
_MAX_TERMINAL_RUNS = 200
# Open runs are bounded too.  A registration whose run never reports (the
# operator killed Orca, the run id was a typo) would otherwise be swept
# forever, costing two subprocess round-trips per gateway start for eternity.
_MAX_OPEN_RUNS = 500
_OPEN_RUN_TTL_SECONDS = 14 * 24 * 60 * 60

_DB_LOCK = threading.Lock()
# Set by start()/stop().  A bridge that has been stopped refuses events rather
# than half-processing one while the gateway tears its listener down.
_STARTED = threading.Event()


class BridgeNotRunning(RuntimeError):
    """Raised when an event arrives before start() or after stop()."""


@dataclass(frozen=True)
class ReconcileResult:
    """What Orca itself says about a run.  The only source of completion."""

    known: bool
    terminal: bool
    status: str
    summary: str
    # Verbatim authoritative Orca workerState, e.g. "succeeded", "failed",
    # "ready", or "" when Orca reported no worker at all (which is the normal
    # shape for a dispatch-driven run).  Interpreted by classify_worker_state.
    terminal_state: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Durable state
# ---------------------------------------------------------------------------

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

    apply_wal_with_fallback(conn, db_label="state.db (orca_bridge)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS orca_runs (
            run_id TEXT PRIMARY KEY,
            goal TEXT NOT NULL DEFAULT '',
            session_key TEXT NOT NULL DEFAULT '',
            origin_ui_session_id TEXT NOT NULL DEFAULT '',
            origin_session_id TEXT NOT NULL DEFAULT '',
            parent_session_id TEXT,
            scope_id TEXT NOT NULL DEFAULT '',
            user_id TEXT NOT NULL DEFAULT '',
            user_name TEXT NOT NULL DEFAULT '',
            worktree TEXT NOT NULL DEFAULT '',
            terminal TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'open',
            outcome TEXT NOT NULL DEFAULT '',
            last_sequence INTEGER NOT NULL DEFAULT -1,
            last_observed_kind TEXT NOT NULL DEFAULT '',
            last_observed_at REAL NOT NULL DEFAULT 0,
            published_at REAL,
            registered_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS orca_bridge_events (
            run_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            seq INTEGER NOT NULL DEFAULT -1,
            kind TEXT NOT NULL DEFAULT '',
            received_at REAL NOT NULL,
            PRIMARY KEY (run_id, event_id)
        )"""
    )


@contextmanager
def _transaction(immediate: bool = False) -> Iterator[sqlite3.Connection]:
    """Commit/rollback AND close — ``with _connect()`` alone leaks the fd.

    ``immediate=True`` takes SQLite's write lock up front.  Read-then-write
    sequences that must elect a single winner (see :func:`_claim_completion`)
    need it: with the default deferred transaction two writers can both take
    a read lock, and the loser fails with SQLITE_BUSY at COMMIT rather than
    losing cleanly at the UPDATE.
    """
    conn = _connect()
    try:
        conn.row_factory = sqlite3.Row
        if immediate:
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
        else:
            with conn:
                yield conn
    finally:
        conn.close()


def start() -> None:
    """Open (and migrate) the durable state before the listener goes live.

    Called from the gateway's startup path so a broken/unwritable ``state.db``
    fails while the operator is still watching, instead of on the first real
    completion event.  Idempotent.
    """
    with _DB_LOCK, _transaction() as conn:
        conn.execute("SELECT 1 FROM orca_runs LIMIT 1").fetchone()
    _STARTED.set()


def stop() -> None:
    """Refuse further events.  Connections are per-transaction, so there is
    nothing to close; this only flips the gate."""
    _STARTED.clear()


def is_running() -> bool:
    return _STARTED.is_set()


def _reset_for_tests() -> None:
    with _DB_LOCK, _transaction() as conn:
        conn.execute("DELETE FROM orca_runs")
        conn.execute("DELETE FROM orca_bridge_events")


# ---------------------------------------------------------------------------
# Identifier + shape validation
# ---------------------------------------------------------------------------

def is_valid_run_id(run_id: Any) -> bool:
    return isinstance(run_id, str) and bool(_RUN_ID_RE.match(run_id))


def is_valid_terminal_id(terminal_id: Any) -> bool:
    return isinstance(terminal_id, str) and bool(_TERMINAL_ID_RE.match(terminal_id))


def sanitize_worktree(worktree: Any) -> str:
    """Clamp a worktree path to something safe to echo into a chat message."""
    if not isinstance(worktree, str):
        return ""
    cleaned = _CONTROL_CHARS_RE.sub("", worktree).strip()
    return cleaned[:_WORKTREE_MAX]


def _coerce_sequence(payload: Dict[str, Any]) -> int:
    for key in ("sequence", "seq"):
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value.strip())
    return -1


def _event_identity(payload: Dict[str, Any], run_id: str, kind: str) -> str:
    """Stable identity for an inbound event.

    Prefer an id the sender supplied.  When there is none, hash the payload so
    a byte-identical retry still dedupes — a sender without ids must not be
    able to run the reconcile path twice for the same notification.
    """
    for key in ("event_id", "eventId", "id", "delivery_id"):
        value = payload.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            text = str(value).strip()
            if text:
                return _CONTROL_CHARS_RE.sub("", text)[:_EVENT_ID_MAX]
    blob = json.dumps(payload, sort_keys=True, default=str)
    return "sha:" + hashlib.sha256(
        f"{run_id}\x00{kind}\x00{blob}".encode()
    ).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Registration (the launch side)
# ---------------------------------------------------------------------------

def _session_env(name: str) -> str:
    """Read a session var through the ContextVar layer, env as fallback."""
    try:
        from gateway.session_context import get_session_env

        return get_session_env(name, "") or ""
    except Exception:  # noqa: BLE001 — origin capture is additive, never fatal
        return os.environ.get(name, "") or ""


def register_run(
    run_id: str,
    *,
    goal: str = "",
    session_key: Optional[str] = None,
    origin_ui_session_id: Optional[str] = None,
    origin_session_id: Optional[str] = None,
    parent_session_id: Optional[str] = None,
    worktree: str = "",
    terminal: str = "",
) -> Dict[str, Any]:
    """Record an Orca run and the conversation that launched it.

    The routing snapshot is taken HERE, at launch, because by completion time
    the ContextVars are long gone.  ``session_key`` is the whole answer to
    "where does the follow-up go": the gateway parses it back into
    platform/chat_type/chat_id/thread_id, so a Mattermost thread key routes to
    that thread and nothing infers a default surface.  When there is no
    session (CLI, cron) the key stays empty and the completion simply has no
    chat to land in — the honest outcome, and much better than guessing a
    platform.
    """
    if not is_valid_run_id(run_id):
        raise ValueError(f"invalid Orca run id: {run_id!r}")
    if terminal and not is_valid_terminal_id(terminal):
        raise ValueError(f"invalid Orca terminal handle: {terminal!r}")

    now = time.time()
    row = {
        "run_id": run_id,
        "goal": goal or "",
        "session_key": (
            session_key if session_key is not None
            else _session_env("HERMES_SESSION_KEY")
        ),
        "origin_ui_session_id": (
            origin_ui_session_id if origin_ui_session_id is not None
            else _session_env("HERMES_SESSION_UI_SESSION_ID")
        ),
        "origin_session_id": (
            origin_session_id if origin_session_id is not None
            else (
                _session_env("HERMES_SESSION_CHAT_ID")
                if _session_env("HERMES_SESSION_PLATFORM") == "api_server"
                else ""
            )
        ),
        "parent_session_id": parent_session_id,
        "scope_id": _session_env("HERMES_SESSION_SCOPE_ID"),
        "user_id": _session_env("HERMES_SESSION_USER_ID"),
        "user_name": _session_env("HERMES_SESSION_USER_NAME"),
        "worktree": sanitize_worktree(worktree),
        "terminal": terminal or "",
    }
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO orca_runs
               (run_id, goal, session_key, origin_ui_session_id,
                origin_session_id, parent_session_id, scope_id, user_id,
                user_name, worktree, terminal, state, outcome, last_sequence,
                registered_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?, 'open', '', -1, ?, ?)""",
            (row["run_id"], row["goal"], row["session_key"],
             row["origin_ui_session_id"], row["origin_session_id"],
             row["parent_session_id"], row["scope_id"], row["user_id"],
             row["user_name"], row["worktree"], row["terminal"], now, now),
        )
        _prune_runs(conn, now=now)
    return get_run(run_id) or row


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    if not is_valid_run_id(run_id):
        return None
    with _DB_LOCK, _transaction() as conn:
        row = conn.execute(
            "SELECT * FROM orca_runs WHERE run_id=?", (run_id,)
        ).fetchone()
    return dict(row) if row is not None else None


def list_runs(state: Optional[str] = None) -> List[Dict[str, Any]]:
    with _DB_LOCK, _transaction() as conn:
        if state:
            rows = conn.execute(
                "SELECT * FROM orca_runs WHERE state=? ORDER BY registered_at",
                (state,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM orca_runs ORDER BY registered_at"
            ).fetchall()
    return [dict(r) for r in rows]


def count_events(run_id: str) -> int:
    with _DB_LOCK, _transaction() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM orca_bridge_events WHERE run_id=?", (run_id,)
        ).fetchone()[0]


def list_event_ids(run_id: str) -> List[str]:
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            "SELECT event_id FROM orca_bridge_events WHERE run_id=? "
            "ORDER BY received_at, event_id",
            (run_id,),
        ).fetchall()
    return [r["event_id"] for r in rows]


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------

def _prune_state(
    conn: sqlite3.Connection,
    run_id: str,
    max_entries: int = _MAX_EVENTS_PER_RUN,
) -> int:
    """Evict the oldest dedupe records for *run_id* beyond *max_entries*.

    Ordering is by the RECORDED TIMESTAMP, not by insertion order.  Events
    arrive out of order — that is the entire reason ``seq`` exists — so
    trusting rowid order would evict a genuinely recent record that merely
    arrived late while keeping an ancient one that happened to land first.
    The evicted record is exactly the one that answers "have I already seen
    this?", so getting the order wrong silently re-opens the replay window
    this table exists to close.
    """
    rows = conn.execute(
        "SELECT event_id, received_at FROM orca_bridge_events WHERE run_id=?",
        (run_id,),
    ).fetchall()
    excess = len(rows) - max_entries
    if excess <= 0:
        return 0
    ordered = sorted(rows, key=lambda r: (r["received_at"], r["event_id"]))
    doomed = [r["event_id"] for r in ordered[:excess]]
    conn.executemany(
        "DELETE FROM orca_bridge_events WHERE run_id=? AND event_id=?",
        [(run_id, event_id) for event_id in doomed],
    )
    return len(doomed)


def _prune_runs(conn: sqlite3.Connection, *, now: float) -> None:
    """Bound BOTH terminal and open-run retention.

    Terminal rows are trimmed to the newest ``_MAX_TERMINAL_RUNS``.  Open rows
    need bounding too: a run that never reports back (Orca killed, run id
    typo'd) otherwise costs two subprocess round-trips on every gateway start
    forever.  Expire them by age first, then by count, and take their dedupe
    records with them so the events table cannot outlive its run.
    """
    cutoff = now - _OPEN_RUN_TTL_SECONDS
    conn.execute(
        "DELETE FROM orca_runs WHERE state != 'completed' AND registered_at < ?",
        (cutoff,),
    )
    for state_clause, keep in (
        ("state = 'completed'", _MAX_TERMINAL_RUNS),
        ("state != 'completed'", _MAX_OPEN_RUNS),
    ):
        conn.execute(
            f"""DELETE FROM orca_runs WHERE run_id IN (
                  SELECT run_id FROM orca_runs WHERE {state_clause}
                  ORDER BY updated_at DESC, run_id DESC LIMIT -1 OFFSET ?
                )""",
            (keep,),
        )
    conn.execute(
        "DELETE FROM orca_bridge_events WHERE run_id NOT IN "
        "(SELECT run_id FROM orca_runs)"
    )


# ---------------------------------------------------------------------------
# Reconciliation — Orca is the authority
# ---------------------------------------------------------------------------

def _orca_bin() -> str:
    return os.environ.get("HERMES_ORCA_BIN") or shutil.which("orca") or "orca"


def _orca_json(args: List[str], timeout: float) -> Dict[str, Any]:
    """Run ``orca <args> --json`` and parse the envelope.

    argv list, never a shell string: the run id reaches Orca as one opaque
    element and cannot be re-parsed as flags or shell syntax.
    """
    proc = subprocess.run(
        [_orca_bin(), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"orca {' '.join(args[:2])} exited {proc.returncode}"
        )
    parsed = json.loads(proc.stdout or "{}")
    if not isinstance(parsed, dict):
        raise RuntimeError(f"orca {' '.join(args[:2])} returned a non-object")
    return parsed


def _authoritative_terminal_state(workers: List[Dict[str, Any]]) -> str:
    """Pick the worker state that decides this run's fate.

    Precedence is fail-closed: any failed worker outranks every healthy
    sibling (one worker in ``failed`` means the run did not finish cleanly,
    however tidy the others look), a worker still in flight outranks a
    successful one (a run is not over while somebody is still working), and a
    real verdict of any kind outranks a no-ledger sentinel.  Returned
    verbatim so the caller sees Orca's own spelling rather than a normalised
    guess; :func:`classify_worker_state` does the interpreting.
    """
    states = [
        str(w.get("workerState") or "").strip()
        for w in workers
        if isinstance(w, dict)
    ]
    states = [state for state in states if state]
    for wanted in (WORKER_VERDICT_FAILURE, WORKER_VERDICT_IN_FLIGHT,
                   WORKER_VERDICT_SUCCESS):
        for state in states:
            if classify_worker_state(state) == wanted:
                return state
    return states[-1] if states else ""


def reconcile_run(
    run_id: str, timeout: float = _ORCA_TIMEOUT_SECONDS
) -> ReconcileResult:
    """Ask Orca what actually happened to *run_id*.

    A run is terminal only when it HAS tasks and none of them are still open.
    Zero tasks is deliberately non-terminal: a run whose ledger is empty has
    no evidence that any work was done, and "no evidence" must never read as
    "finished".
    """
    if not is_valid_run_id(run_id):
        raise ValueError(f"invalid Orca run id: {run_id!r}")

    show = _orca_json(
        ["orchestration", "run-show", "--id", run_id, "--json"], timeout
    )
    if not show.get("ok"):
        return ReconcileResult(
            known=False, terminal=False, status="unknown",
            summary="Orca does not know this run.",
        )

    listing = _orca_json(
        ["orchestration", "task-list", "--run", run_id, "--brief", "--json"],
        timeout,
    )
    tasks = ((listing.get("result") or {}).get("tasks")) or []
    tasks = [t for t in tasks if isinstance(t, dict)]
    open_tasks = [
        t for t in tasks
        if str(t.get("status", "")).lower() in _OPEN_TASK_STATES
    ]
    failed = [
        t for t in tasks
        if str(t.get("status", "")).lower() in _FAILED_TASK_STATES
    ]

    # Worker accounting is a separate ledger from Task status — Orca says so
    # itself ("a completed Task can still own a live terminal") — and it is
    # the only place a settled-in-failure worker surfaces.  A failure of the
    # CALL here is not fatal to reconciliation: no workers simply means no
    # terminal-level verdict, and dispatch-driven runs report none at all.
    try:
        worker_listing = _orca_json(
            ["orchestration", "worker-list", "--run", run_id, "--json"], timeout
        )
        workers = ((worker_listing.get("result") or {}).get("workers")) or []
    except Exception as exc:  # noqa: BLE001
        logger.debug("[orca-bridge] worker-list unavailable for %s: %s", run_id, exc)
        workers = []
    terminal_state = _authoritative_terminal_state(
        [w for w in workers if isinstance(w, dict)]
    )

    detail: Dict[str, Any] = {
        "tasks": [
            {"id": t.get("id"), "title": t.get("task_title"),
             "status": t.get("status")}
            for t in tasks
        ],
        "open": len(open_tasks),
        "failed": len(failed),
        "terminal_state": terminal_state,
    }

    if not tasks or open_tasks:
        return ReconcileResult(
            known=True, terminal=False, status="running",
            summary=(
                f"{len(open_tasks)} of {len(tasks)} Orca task(s) still open."
                if tasks else "Orca run has no tasks yet."
            ),
            terminal_state=terminal_state,
            detail=detail,
        )

    return ReconcileResult(
        known=True,
        terminal=True,
        status="failed" if failed else "completed",
        summary=(
            f"Orca run {run_id}: {len(tasks) - len(failed)} of {len(tasks)} "
            f"task(s) completed, {len(failed)} failed."
        ),
        terminal_state=terminal_state,
        detail=detail,
    )


def _classify_transition(kind: str, verdict: ReconcileResult) -> str:
    """Decide what an authoritative verdict means for a candidate signal.

    Split out from :func:`process_event` because this is the one judgement in
    the bridge that is pure policy, and the one most easily got wrong: three
    of the five outcomes here must NOT wake anybody.
    """
    if kind.strip().lower() not in CANDIDATE_KINDS:
        return TRANSITION_NONCOMPLETION
    if not verdict.known:
        return TRANSITION_UNKNOWN

    # The worker ledger is a SEPARATE ledger from Task status — Orca says so
    # itself — and it can veto a run whose tasks all read `completed`.  A
    # worker that settled in `failed`/`stopped`/`abandoned`, or one Orca
    # could not confirm (`start_unknown`/`stop_unknown`), publishes NOTHING:
    # a "your task is done" ping for a worker that fell over is worse than
    # silence, because the owner stops watching.
    worker_verdict = classify_worker_state(verdict.terminal_state)
    if worker_verdict == WORKER_VERDICT_FAILURE:
        return TRANSITION_TERMINAL_FAILURE
    # Still starting, idle-but-alive, or on its way down: not finished yet.
    # The run stays open and sweep() re-asks once the worker settles.
    if worker_verdict == WORKER_VERDICT_IN_FLIGHT:
        return TRANSITION_IN_FLIGHT

    if not verdict.terminal:
        return TRANSITION_IN_FLIGHT
    return TRANSITION_COMPLETED


# ---------------------------------------------------------------------------
# Wake
# ---------------------------------------------------------------------------

def _delegation_id(run_id: str) -> str:
    """Delegation id for a run.

    Derived from the RUN and nothing else.  An event-derived id was how two
    racing notifications for one run produced two distinct delegation ids and
    two "completed" messages: with a run-scoped id the durable ledger's own
    INSERT OR IGNORE catches a second publish even if the state machine above
    it somehow lets two callers through.
    """
    digest = hashlib.sha256(run_id.encode()).hexdigest()[:12]
    return f"orca_{digest}"


def _build_wake_event(
    run: Dict[str, Any], *, delegation_id: str, status: str, summary: str,
    error: Optional[str], kind: str, detail: Dict[str, Any],
) -> Dict[str, Any]:
    """Build the completion event.

    Everything here is either Hermes' own registration data or Orca's
    reconciled verdict.  Nothing from the inbound payload body is copied in —
    no summary, no command, no prompt — so a hostile POST cannot inject text
    into the agent's next turn.  ``kind`` is the one payload-derived value and
    it is constrained to the fixed vocabulary above before it reaches here.
    """
    now = time.time()
    worktree = sanitize_worktree(run.get("worktree", ""))
    evt: Dict[str, Any] = {
        "type": "async_delegation",
        "delegation_id": delegation_id,
        "session_key": run.get("session_key", "") or "",
        "origin_ui_session_id": run.get("origin_ui_session_id", "") or "",
        "origin_session_id": run.get("origin_session_id", "") or "",
        "parent_session_id": run.get("parent_session_id"),
        "goal": run.get("goal", "") or f"Orca run {run['run_id']}",
        "context": (
            f"Executed by Orca (run {run['run_id']}"
            + (f", worktree {worktree}" if worktree else "")
            + f"). Signal: {kind}. Outcome confirmed by re-querying Orca, "
            "not taken from the notification body."
        ),
        "toolsets": None,
        "role": "orca-run",
        "model": None,
        "status": status,
        "summary": summary,
        "error": error,
        "api_calls": 0,
        "duration_seconds": round(now - (run.get("registered_at") or now), 2),
        "dispatched_at": run.get("registered_at") or now,
        "completed_at": now,
        "orca_run_id": run["run_id"],
        "orca_detail": detail,
    }
    for key in ("scope_id", "user_id", "user_name"):
        if run.get(key):
            evt[key] = run[key]
    return evt


def _publish_completion(evt: Dict[str, Any]) -> str:
    """Hand the wake to the existing supervisor rail."""
    from tools.async_delegation import publish_external_completion

    return publish_external_completion(evt)


# ---------------------------------------------------------------------------
# Event handling
# ---------------------------------------------------------------------------

def _record_event(
    run_id: str, event_id: str, seq: int, kind: str, now: float
) -> bool:
    """Insert the event; False when it was already seen (duplicate/replay)."""
    with _DB_LOCK, _transaction() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO orca_bridge_events
               (run_id, event_id, seq, kind, received_at) VALUES (?,?,?,?,?)""",
            (run_id, event_id, seq, kind, now),
        )
        if cur.rowcount != 1:
            return False
        _prune_state(conn, run_id)
    return True


def _observe(run_id: str, *, seq: int, kind: str, observed_at: float) -> None:
    """Record that we saw something, without changing the run's fate.

    Deliberately does NOT write ``state``.  An observation is assembled from a
    row that was read before the reconcile, so writing that state back would
    undo a completion a concurrent event committed in the meantime — a lost
    update that hands the next caller an ``open`` run and lets it publish a
    second completion.  The conditional UPDATE in :func:`_claim_completion` is
    only single-winner if nothing else resurrects the state behind it.
    """
    now = time.time()
    sets = ["updated_at=?", "last_observed_kind=?", "last_observed_at=?"]
    args: List[Any] = [now, kind, observed_at or now]
    if seq >= 0:
        sets.append("last_sequence=MAX(last_sequence, ?)")
        args.append(seq)
    args.append(run_id)
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            f"UPDATE orca_runs SET {', '.join(sets)} WHERE run_id=?", args
        )


def _mark_terminal_failure(run_id: str, *, kind: str,
                           observed_at: float) -> None:
    """Park a failed worker's run without overwriting a real completion."""
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            """UPDATE orca_runs SET state='terminal_failure', updated_at=?,
               last_observed_kind=?, last_observed_at=?
               WHERE run_id=? AND state != 'completed'""",
            (now, kind, observed_at or now, run_id),
        )


def _claim_completion(run_id: str, outcome: str) -> bool:
    """Elect exactly ONE caller to publish this run's completion.

    The whole transition is a single conditional UPDATE inside a BEGIN
    IMMEDIATE transaction, so the "is it still open?" read and the "mark it
    completed" write cannot be split by another caller.  ``rowcount == 1``
    means this caller flipped the row and owns publication; ``0`` means
    somebody else already did and this caller must publish nothing.

    That is the fix for the race where a ``worker_done`` webhook and a
    terminal ``exit`` (or the recovery sweep) both read ``state='open'``,
    both transitioned, and both published a completion.
    """
    now = time.time()
    with _DB_LOCK, _transaction(immediate=True) as conn:
        cur = conn.execute(
            """UPDATE orca_runs SET state='completed', outcome=?, updated_at=?
               WHERE run_id=? AND state != 'completed'""",
            (outcome, now, run_id),
        )
        return cur.rowcount == 1


def _mark_published(run_id: str) -> None:
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            "UPDATE orca_runs SET published_at=?, updated_at=? WHERE run_id=?",
            (now, now, run_id),
        )
        _prune_runs(conn, now=now)


def _complete(run: Dict[str, Any], verdict: ReconcileResult, *,
              kind: str) -> Dict[str, Any]:
    """Claim, then publish.  Losers publish nothing."""
    run_id = run["run_id"]
    if not _claim_completion(run_id, verdict.status):
        logger.info(
            "[orca-bridge] run %s was already completed by another event; "
            "not publishing again", run_id,
        )
        return {"status": "duplicate", "completed": True, "published": False,
                "run_id": run_id, "outcome": verdict.status}

    evt = _build_wake_event(
        run,
        delegation_id=_delegation_id(run_id),
        status=verdict.status,
        summary=verdict.summary,
        error=None if verdict.status == "completed" else verdict.summary,
        kind=kind,
        detail=verdict.detail,
    )
    # Publish AFTER the claim commits but BEFORE published_at is stamped: a
    # crash in between leaves state='completed' with published_at NULL, which
    # sweep() re-publishes.  publish_external_completion is idempotent by
    # delegation_id, so the retry cannot double-deliver.
    _publish_completion(evt)
    _mark_published(run_id)
    return {"status": "completed", "completed": True, "published": True,
            "run_id": run_id, "outcome": verdict.status}


def process_event(payload: Any) -> Dict[str, Any]:
    """Process one inbound Orca notification.  Never raises on bad input."""
    if not _STARTED.is_set():
        raise BridgeNotRunning("Orca bridge is not running")
    if not isinstance(payload, dict):
        return {"status": "invalid_run_id", "completed": False,
                "published": False}

    raw_run_id = payload.get("run_id") or payload.get("runId") or ""
    run_id = raw_run_id.strip() if isinstance(raw_run_id, str) else ""
    if not is_valid_run_id(run_id):
        return {"status": "invalid_run_id", "completed": False,
                "published": False}

    raw_terminal = payload.get("terminal") or payload.get("terminal_handle") or ""
    if raw_terminal and not is_valid_terminal_id(raw_terminal):
        return {"status": "invalid_terminal", "completed": False,
                "published": False, "run_id": run_id}

    run = get_run(run_id)
    if run is None:
        # Registration is what carries the conversation to report back to.
        # An unregistered run has nowhere to land, and accepting it would let
        # any local caller populate the ledger with ids of its choosing.
        logger.info("[orca-bridge] event for unregistered run %s", run_id)
        return {"status": "unknown_run", "completed": False,
                "published": False, "run_id": run_id}

    raw_kind = (
        payload.get("kind") or payload.get("event")
        or payload.get("event_type") or payload.get("type") or ""
    )
    kind = _CONTROL_CHARS_RE.sub("", str(raw_kind)).strip()[:64]
    normalized = kind.lower()
    now = time.time()
    event_id = _event_identity(payload, run_id, kind)
    seq = _coerce_sequence(payload)

    if not _record_event(run_id, event_id, seq, kind, now):
        return {"status": "duplicate", "completed": False, "published": False,
                "run_id": run_id}

    last_seq = int(run.get("last_sequence", -1) or -1)
    if 0 <= seq < last_seq:
        logger.info(
            "[orca-bridge] dropping replayed event seq=%s (run %s is at %s)",
            seq, run_id, last_seq,
        )
        return {"status": "stale", "completed": False, "published": False,
                "run_id": run_id}

    if run.get("state") == "completed":
        return {"status": "already_completed", "completed": True,
                "published": False, "run_id": run_id}

    if normalized in OBSERVE_KINDS:
        # Inspect-only.  Deliberately does NOT re-query Orca: a Stop hook
        # fires constantly and cannot mean "done", so spending a reconcile on
        # one would be pure load with no decision attached to it.
        _observe(run_id, seq=seq, kind=kind, observed_at=now)
        return {"status": "observed", "completed": False, "published": False,
                "run_id": run_id}

    if normalized not in CANDIDATE_KINDS:
        return {"status": "ignored", "completed": False, "published": False,
                "run_id": run_id}

    _observe(run_id, seq=seq, kind=kind, observed_at=now)
    try:
        verdict = reconcile_run(run_id)
    except Exception as exc:  # noqa: BLE001 — Orca down must never mean done
        logger.warning(
            "[orca-bridge] could not reconcile run %s: %s", run_id, exc
        )
        return {"status": "reconcile_unavailable", "completed": False,
                "published": False, "run_id": run_id}

    transition = _classify_transition(kind, verdict)
    if transition == TRANSITION_UNKNOWN:
        return {"status": "reconcile_unavailable", "completed": False,
                "published": False, "run_id": run_id}
    if transition == TRANSITION_TERMINAL_FAILURE:
        _mark_terminal_failure(run_id, kind=kind, observed_at=now)
        logger.warning(
            "[orca-bridge] run %s ended in worker state %r — not a completion",
            run_id, verdict.terminal_state,
        )
        return {"status": TRANSITION_TERMINAL_FAILURE, "completed": False,
                "published": False, "run_id": run_id,
                "terminal_state": verdict.terminal_state}
    if transition == TRANSITION_IN_FLIGHT:
        return {"status": "not_terminal", "completed": False,
                "published": False, "run_id": run_id}
    if transition != TRANSITION_COMPLETED:
        return {"status": "ignored", "completed": False, "published": False,
                "run_id": run_id}

    return _complete(dict(run), verdict, kind=kind)


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

def sweep() -> int:
    """Reconcile every unfinished run; wake for the ones Orca says are done.

    This — not webhook retry — is what covers Hermes being down when Orca
    finished: the POST is gone for good, so recovery has to re-ask Orca.  Also
    finishes the job for a run that was claimed but crashed before its
    completion reached the durable ledger (``state='completed'`` with
    ``published_at`` still NULL).
    """
    published = 0
    for run in list_runs():
        run_id = run.get("run_id") or ""
        if not is_valid_run_id(run_id):
            continue

        if run.get("state") == "completed":
            if run.get("published_at") is not None:
                continue
            # Claimed but never published — replay it.  Idempotent by
            # delegation_id, so a double publish cannot double-deliver.
            evt = _build_wake_event(
                run,
                delegation_id=_delegation_id(run_id),
                status=run.get("outcome") or "completed",
                summary=f"Orca run {run_id} completed (recovered at startup).",
                error=None,
                kind="sweep",
                detail={"recovered": True},
            )
            try:
                _publish_completion(evt)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[orca-bridge] sweep could not republish %s: %s", run_id, exc
                )
                continue
            _mark_published(run_id)
            published += 1
            continue

        try:
            verdict = reconcile_run(run_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[orca-bridge] sweep skipped %s: %s", run_id, exc)
            continue
        if _classify_transition("worker_done", verdict) != TRANSITION_COMPLETED:
            continue
        if _complete(run, verdict, kind="sweep").get("published"):
            published += 1
    return published
