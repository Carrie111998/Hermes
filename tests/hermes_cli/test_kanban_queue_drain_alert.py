"""Tests for the kanban queue-drain alert (hermes_cli.kanban_health).

Covers the acceptance criteria for the queue-drain monitor added after the
2026-08-03 "silent fleet drain" incident:

* Todo/Blocked work exists AND no runnable worker remains because every
  owner profile is gated (quarantined/unhealthy store or tripped
  two-failure circuit breaker) -> alert fires.
* At least one runnable worker remains -> no alert.
* Quarantine state is read from the board's event stream by default (the
  task-1 seam) and through registered providers.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_health as kh


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _open_conn():
    return kb.connect()


def _make_task(
    conn,
    *,
    title="task",
    assignee=None,
    status="blocked",
    consecutive_failures=0,
    max_retries=None,
):
    """Create a task and force its status/failure counter directly.

    ``create_task`` only accepts a small initial-status set, so tests that
    need ``todo`` / ``blocked`` + a specific failure count UPDATE after
    creation — same technique as other kanban DB tests.
    """
    task_id = kb.create_task(
        conn,
        title=title,
        assignee=assignee,
        initial_status="blocked",  # create_task accepts this; we override below
    )
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = ?, consecutive_failures = ?, "
            "max_retries = ? WHERE id = ?",
            (status, consecutive_failures, max_retries, task_id),
        )
    return task_id


# ---------------------------------------------------------------------------
# Alert fires: all workers gated
# ---------------------------------------------------------------------------


def test_alert_fires_when_todo_blocked_all_workers_quarantined(kanban_home):
    """Todo + Blocked work, every owner profile quarantined -> alert."""
    conn = _open_conn()
    try:
        _make_task(conn, title="todo work", assignee="alice", status="todo")
        _make_task(conn, title="blocked work", assignee="alice", status="blocked")
        _make_task(conn, title="blocked bob", assignee="bob", status="blocked")

        def quarantine_provider(c, profile):
            if profile in ("alice", "bob"):
                return kh.QuarantineState(
                    profile=profile,
                    reason="store_unhealthy",
                    detail="session storage could not be written",
                    db_path="/tmp/fake/state.db",
                    error="database disk image is malformed",
                )
            return None

        report = kh.check_queue_drain(
            conn,
            board="default",
            profile_exists_fn=lambda name: True,
            quarantine_provider=quarantine_provider,
        )
    finally:
        conn.close()

    assert report.should_alert is True
    assert report.todo_blocked_count == 3
    assert sorted(report.quarantined_profiles) == ["alice", "bob"]
    assert report.runnable_profiles == []
    assert "quarantine" in report.reasons


def test_alert_fires_when_circuit_breaker_tripped(kanban_home):
    """Todo/Blocked work, breaker tripped on all pending tasks -> alert."""
    conn = _open_conn()
    try:
        _make_task(
            conn, title="tripped A", assignee="alice",
            status="blocked", consecutive_failures=2,
        )
        _make_task(
            conn, title="tripped B", assignee="alice",
            status="blocked", consecutive_failures=3,
        )
        report = kh.check_queue_drain(
            conn,
            board="default",
            failure_limit=2,
            profile_exists_fn=lambda name: True,
        )
    finally:
        conn.close()

    assert report.should_alert is True
    assert report.todo_blocked_count == 2
    assert report.breaker_tripped_profiles == ["alice"]
    assert report.breaker_tripped_tasks == 2
    assert "circuit_breaker" in report.reasons
    assert report.runnable_profiles == []


def test_alert_fires_mixed_gates(kanban_home):
    """Quarantine on one profile + breaker on another -> both reasons."""
    conn = _open_conn()
    try:
        _make_task(
            conn, title="quarantined", assignee="alice",
            status="blocked",
        )
        _make_task(
            conn, title="tripped", assignee="bob",
            status="blocked", consecutive_failures=2,
        )

        def quarantine_provider(c, profile):
            if profile == "alice":
                return kh.QuarantineState(profile=profile, reason="fts_malformed")
            return None

        report = kh.check_queue_drain(
            conn,
            board="default",
            failure_limit=2,
            profile_exists_fn=lambda name: True,
            quarantine_provider=quarantine_provider,
        )
    finally:
        conn.close()

    assert report.should_alert is True
    assert set(report.reasons) == {"quarantine", "circuit_breaker"}
    assert report.quarantined_profiles == ["alice"]
    assert report.breaker_tripped_profiles == ["bob"]


def test_alert_fires_with_event_stream_quarantine(kanban_home):
    """Default provider reads quarantine events from the board event stream.

    This is the "reuse quarantine state/event streams from task 1 if
    available" seam: the pre-dispatch health probe emits
    ``profile_quarantined`` events and the alert picks them up with zero
    provider registration.
    """
    conn = _open_conn()
    try:
        task_id = _make_task(
            conn, title="pending", assignee="alice", status="todo"
        )
        with kb.write_txn(conn):
            kb._append_event(
                conn, task_id, "profile_quarantined",
                {
                    "profile": "alice",
                    "reason": "store_unhealthy",
                    "db_path": "/tmp/fake/state.db",
                    "error": "database disk image is malformed",
                    "fts_index": "messages_fts",
                },
            )
        report = kh.check_queue_drain(
            conn,
            board="default",
            profile_exists_fn=lambda name: True,
        )
    finally:
        conn.close()

    assert report.should_alert is True
    assert report.quarantined_profiles == ["alice"]
    assert "quarantine" in report.reasons


def test_event_stream_unquarantine_clears(kanban_home):
    """A later un-quarantine event clears the profile from quarantine."""
    conn = _open_conn()
    try:
        task_id = _make_task(
            conn, title="pending", assignee="alice", status="todo"
        )
        with kb.write_txn(conn):
            kb._append_event(
                conn, task_id, "profile_quarantined",
                {"profile": "alice", "reason": "store_unhealthy"},
            )
            kb._append_event(
                conn, task_id, "profile_unquarantined",
                {"profile": "alice", "reason": "store_repaired"},
            )
        report = kh.check_queue_drain(
            conn,
            board="default",
            profile_exists_fn=lambda name: True,
        )
    finally:
        conn.close()

    assert report.quarantined_profiles == []
    # Healthy profile exists and is not gated -> runnable, no alert.
    assert report.should_alert is False
    assert report.runnable_profiles == ["alice"]


# ---------------------------------------------------------------------------
# No alert: runnable worker remains
# ---------------------------------------------------------------------------


def test_no_alert_when_runnable_worker_exists(kanban_home):
    """Todo/Blocked work exists, but a healthy ready worker can run."""
    conn = _open_conn()
    try:
        _make_task(conn, title="blocked", assignee="alice", status="blocked")
        _make_task(conn, title="runnable", assignee="bob", status="ready")

        def quarantine_provider(c, profile):
            if profile == "alice":
                return kh.QuarantineState(profile=profile, reason="store_unhealthy")
            return None

        report = kh.check_queue_drain(
            conn,
            board="default",
            profile_exists_fn=lambda name: True,
            quarantine_provider=quarantine_provider,
        )
    finally:
        conn.close()

    assert report.should_alert is False
    assert report.todo_blocked_count == 1
    assert report.runnable_profiles == ["bob"]
    assert report.quarantined_profiles == ["alice"]


def test_no_alert_when_breaker_not_tripped(kanban_home):
    """Blocked work with a sub-threshold failure count is not a breaker
    trip, and the profile can still run -> no alert."""
    conn = _open_conn()
    try:
        _make_task(
            conn, title="below threshold", assignee="alice",
            status="blocked", consecutive_failures=1,
        )
        report = kh.check_queue_drain(
            conn,
            board="default",
            failure_limit=2,
            profile_exists_fn=lambda name: True,
        )
    finally:
        conn.close()

    assert report.should_alert is False
    assert report.breaker_tripped_profiles == []
    assert report.runnable_profiles == ["alice"]


def test_no_alert_without_backlog(kanban_home):
    """Only ready work (no Todo/Blocked) never fires the drain alert."""
    conn = _open_conn()
    try:
        _make_task(
            conn, title="ready only", assignee="alice",
            status="ready", consecutive_failures=5,
        )
        report = kh.check_queue_drain(
            conn,
            board="default",
            failure_limit=2,
            profile_exists_fn=lambda name: True,
        )
    finally:
        conn.close()

    assert report.todo_blocked_count == 0
    assert report.should_alert is False


def test_no_alert_when_control_plane_lane_only(kanban_home):
    """Assignees that are not real Hermes profiles are control-plane lanes
    (never auto-spawned) and do not trigger the drain alert by themselves."""
    conn = _open_conn()
    try:
        _make_task(conn, title="terminal lane", assignee="orion-cc", status="blocked")
        report = kh.check_queue_drain(
            conn,
            board="default",
            profile_exists_fn=lambda name: False,  # no real profile
        )
    finally:
        conn.close()

    assert report.should_alert is False
    assert report.runnable_profiles == []


# ---------------------------------------------------------------------------
# Alert payload / formatting
# ---------------------------------------------------------------------------


def test_format_alert_includes_required_figures():
    report = kh.QueueDrainReport(
        board="default",
        todo_blocked_count=3,
        pending_profiles=["alice", "bob"],
        runnable_profiles=[],
        quarantined_profiles=["alice"],
        breaker_tripped_profiles=["bob"],
        breaker_tripped_tasks=2,
        reasons=["quarantine", "circuit_breaker"],
    )
    line = kh.format_queue_drain_alert(report)
    assert "KANBAN QUEUE DRAIN" in line
    assert "3 todo/blocked task(s) pending" in line
    assert "1 quarantined profile(s)" in line
    assert "circuit breaker tripped on 1 profile(s)/2 task(s)" in line


def test_report_to_dict_round_trip():
    report = kh.QueueDrainReport(
        board="default",
        todo_blocked_count=2,
        runnable_profiles=[],
        quarantined_profiles=["alice"],
        reasons=["quarantine"],
    )
    data = json.loads(json.dumps(report.to_dict()))
    assert data["should_alert"] is True
    assert data["todo_blocked_count"] == 2


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _run_cmd_health(monkeypatch, *, profile_exists=True):
    """Invoke ``hermes kanban health`` handler with patched profile lookup."""
    args = types.SimpleNamespace(json=False, failure_limit=None)
    if profile_exists:
        monkeypatch.setattr(
            "hermes_cli.profiles.profile_exists", lambda name: True
        )
    from hermes_cli import kanban as kanban_cli
    return kanban_cli._cmd_health(args)


def test_cli_health_returns_1_when_drained(kanban_home, monkeypatch, capsys):
    conn = _open_conn()
    try:
        _make_task(conn, title="pending", assignee="alice", status="todo")
        with kb.write_txn(conn):
            task_id = conn.execute(
                "SELECT id FROM tasks WHERE assignee='alice' LIMIT 1"
            ).fetchone()[0]
            kb._append_event(
                conn, task_id, "profile_quarantined",
                {"profile": "alice", "reason": "store_unhealthy"},
            )
    finally:
        conn.close()

    exit_code = _run_cmd_health(monkeypatch)
    out = capsys.readouterr().out
    assert exit_code == 1
    assert "KANBAN QUEUE DRAIN" in out


def test_cli_health_returns_0_when_healthy(kanban_home, monkeypatch, capsys):
    conn = _open_conn()
    try:
        _make_task(conn, title="pending", assignee="alice", status="todo")
    finally:
        conn.close()

    exit_code = _run_cmd_health(monkeypatch)
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "runnable worker" in out


def test_cli_health_returns_0_when_no_backlog(kanban_home, monkeypatch, capsys):
    exit_code = _run_cmd_health(monkeypatch)
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "No Todo/Blocked work pending" in out
