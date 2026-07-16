"""Tests for retriage-on-timeout (``kanban.retriage_on_timeout``).

When enabled, a task whose circuit breaker trips on consecutive TIMEOUTS
is sent back to ``triage`` for decomposition (failure-context block
appended to its body, counter reset, ``retriaged`` event) instead of
being parked in ``blocked`` with a ``gave_up`` event. Rationale: a
timeout is deterministic — a task that needs more than
``max_runtime_seconds`` fails identically on every blind retry — so
subdivision via the existing auto-decompose pipeline is the productive
recovery, not repetition.

Covers:
  * default-off keeps the historical gave_up behavior byte-for-byte
  * the retriage transition itself (status, body block, counter reset,
    claim cleanup, event payload)
  * the once-only guard (second trip blocks normally)
  * non-timeout outcomes (crash / spawn_failed) never retriage
  * ``force_trip`` callers keep plain block semantics
  * per-task ``max_retries`` override interaction
  * compatibility with ``decompose_triage_task`` (the retriaged task is
    a valid decomposition root)
  * ``dispatch_once`` plumbing end-to-end + ``DispatchResult.retriaged``
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _age_active_run(conn, tid, *, seconds=30):
    """Backdate the active run so elapsed > max_runtime_seconds."""
    old_started = int(time.time()) - seconds
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET started_at = ? WHERE id = ?",
            (old_started, tid),
        )
        conn.execute(
            "UPDATE task_runs SET started_at = ? "
            "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
            (old_started, tid),
        )


def _run_one_timeout_cycle(conn, tid, *, retriage_on_timeout):
    """Claim the task, backdate its run, and enforce max runtime once."""
    kb.claim_task(conn, tid)
    kb._set_worker_pid(conn, tid, os.getpid())
    _age_active_run(conn, tid)
    return kb.enforce_max_runtime(
        conn,
        signal_fn=lambda pid, sig: None,
        retriage_on_timeout=retriage_on_timeout,
    )


@pytest.fixture
def fast_pid_dead(monkeypatch):
    """Pretend SIGTERM works instantly so the grace poll exits fast."""
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)


# ---------------------------------------------------------------------------
# Default off — historical behavior unchanged
# ---------------------------------------------------------------------------

def test_default_off_keeps_gave_up_behavior(kanban_home, fast_pid_dead):
    """Without the flag, repeated timeouts still block with gave_up."""
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="long job", assignee="worker",
            max_runtime_seconds=1,
        )
        for _ in range(2):
            _run_one_timeout_cycle(conn, tid, retriage_on_timeout=False)

        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        events = kb.list_events(conn, tid)
        assert any(e.kind == "gave_up" for e in events)
        assert not any(e.kind == "retriaged" for e in events)
    finally:
        conn.close()


def test_config_default_is_off():
    """The config ships with retriage_on_timeout disabled (opt-in)."""
    from hermes_cli.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["kanban"]["retriage_on_timeout"] is False


# ---------------------------------------------------------------------------
# The retriage transition
# ---------------------------------------------------------------------------

def test_retriage_on_breaker_trip(kanban_home, fast_pid_dead):
    """Second consecutive timeout retriages instead of blocking."""
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="long job", assignee="worker",
            body="Original spec: do the big thing.",
            max_runtime_seconds=1,
        )
        # First timeout: below the default limit (2) — plain retry.
        _run_one_timeout_cycle(conn, tid, retriage_on_timeout=True)
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 1

        # Second timeout: breaker would trip — retriage instead.
        timed_out = _run_one_timeout_cycle(conn, tid, retriage_on_timeout=True)
        assert tid in timed_out, "retriaged task still counts as timed out"
        assert tid in kb.enforce_max_runtime._last_retriaged

        task = kb.get_task(conn, tid)
        assert task.status == "triage"
        assert task.consecutive_failures == 0, "fresh breaker budget"
        assert task.claim_lock is None
        assert task.worker_pid is None

        # Body: original content preserved + failure-context block.
        assert task.body.startswith("Original spec: do the big thing.")
        assert "Automated retriage after timeout" in task.body
        assert "within 1 seconds of work" in task.body

        events = kb.list_events(conn, tid)
        assert not any(e.kind == "gave_up" for e in events)
        retriaged = [e for e in events if e.kind == "retriaged"]
        assert len(retriaged) == 1
        payload = retriaged[0].payload
        assert payload["failures"] == 2
        assert payload["effective_limit"] == 2
        assert payload["trigger_outcome"] == "timed_out"
        assert "error" in payload
    finally:
        conn.close()


def test_retriage_only_once_then_blocks(kanban_home, fast_pid_dead):
    """A task is retriaged at most once; the second trip blocks normally.

    This is the guard against unbounded subdivision: if the decomposed
    task somehow lands back in the run cycle and keeps timing out, the
    breaker wins.
    """
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="long job", assignee="worker",
            max_runtime_seconds=1,
        )
        for _ in range(2):
            _run_one_timeout_cycle(conn, tid, retriage_on_timeout=True)
        assert kb.get_task(conn, tid).status == "triage"

        # Simulate specify/promote putting it back into rotation.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,),
            )

        # Counter was reset — two more timeouts to reach the limit again.
        for _ in range(2):
            _run_one_timeout_cycle(conn, tid, retriage_on_timeout=True)

        task = kb.get_task(conn, tid)
        assert task.status == "blocked", "second trip must give up"
        events = kb.list_events(conn, tid)
        assert sum(1 for e in events if e.kind == "retriaged") == 1
        assert any(e.kind == "gave_up" for e in events)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Only deterministic timeouts retriage
# ---------------------------------------------------------------------------

def test_crash_outcome_never_retriages(kanban_home):
    """Crashes are plausibly transient — they keep the block semantics."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="crashy", assignee="worker")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'ready', consecutive_failures = 1 "
                "WHERE id = ?", (tid,),
            )
        tripped = kb._record_task_failure(
            conn, tid, "worker died",
            outcome="crashed",
            retriage_on_timeout=True,
        )
        assert tripped is True
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert not any(
            e.kind == "retriaged" for e in kb.list_events(conn, tid)
        )
    finally:
        conn.close()


def test_force_trip_never_retriages(kanban_home):
    """force_trip callers applied their own retry policy — plain block."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="violator", assignee="worker")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,),
            )
        tripped = kb._record_task_failure(
            conn, tid, "elapsed 99s > limit 1s",
            outcome="timed_out",
            force_trip=True,
            retriage_on_timeout=True,
        )
        assert tripped is True
        assert kb.get_task(conn, tid).status == "blocked"
    finally:
        conn.close()


def test_per_task_max_retries_retriages_on_first_timeout(
    kanban_home, fast_pid_dead,
):
    """max_retries=1 means the very first timeout is the trip → retriage."""
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="one shot", assignee="worker",
            max_runtime_seconds=1, max_retries=1,
        )
        _run_one_timeout_cycle(conn, tid, retriage_on_timeout=True)
        task = kb.get_task(conn, tid)
        assert task.status == "triage"
        retriaged = [
            e for e in kb.list_events(conn, tid) if e.kind == "retriaged"
        ]
        assert retriaged and retriaged[0].payload["effective_limit"] == 1
        assert retriaged[0].payload["limit_source"] == "task"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Decompose pipeline compatibility
# ---------------------------------------------------------------------------

def test_retriaged_task_is_a_valid_decompose_root(kanban_home, fast_pid_dead):
    """After retriage the task decomposes exactly like a created-in-triage
    one: children get created, the root flips to todo, and the failure
    block stays in the root body for the judge run."""
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="big job", assignee="worker",
            body="Do A then B then C.",
            max_runtime_seconds=1,
        )
        for _ in range(2):
            _run_one_timeout_cycle(conn, tid, retriage_on_timeout=True)
        assert kb.get_task(conn, tid).status == "triage"

        child_ids = kb.decompose_triage_task(
            conn, tid,
            root_assignee="orchestrator",
            children=[
                {"title": "Do A", "assignee": "worker", "parents": []},
                {"title": "Do B", "assignee": "worker", "parents": [0]},
                {"title": "Do C", "assignee": "worker", "parents": [1]},
            ],
            author="auto-decomposer",
        )
        assert child_ids is not None and len(child_ids) == 3

        root = kb.get_task(conn, tid)
        assert root.status == "todo"
        assert "Automated retriage after timeout" in root.body
        for cid in child_ids:
            assert kb.get_task(conn, cid) is not None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# dispatch_once plumbing
# ---------------------------------------------------------------------------

def test_dispatch_once_surfaces_retriaged(kanban_home, monkeypatch):
    """The full chain dispatch_once → _dispatch_once_locked →
    enforce_max_runtime → _record_task_failure works, and the retriaged
    task id lands in DispatchResult.retriaged."""
    # Keep our own process alive in the eyes of detect_crashed_workers,
    # neutralize the real signals + the grace poll sleep.
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(kb.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(kb.time, "sleep", lambda s: None)

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="long job", assignee="worker",
            max_runtime_seconds=1,
        )
        kb.claim_task(conn, tid)
        kb._set_worker_pid(conn, tid, os.getpid())
        _age_active_run(conn, tid)
        # One failure already on the counter → this tick's timeout trips.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET consecutive_failures = 1 WHERE id = ?",
                (tid,),
            )

        res = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, ws, board=None: None,
            retriage_on_timeout=True,
        )
        assert tid in res.timed_out
        assert tid in res.retriaged
        assert kb.get_task(conn, tid).status == "triage"

        # Default-off dispatch keeps the field empty (and, with the task
        # now in triage, nothing new times out).
        res2 = kb.dispatch_once(
            conn, spawn_fn=lambda task, ws, board=None: None,
        )
        assert res2.retriaged == []
    finally:
        conn.close()
