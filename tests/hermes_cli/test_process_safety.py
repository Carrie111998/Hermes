"""Unit tests for hermes_cli/process_safety.py (PID-birth safety and safe termination)."""

import os
import signal
import time
from unittest.mock import MagicMock

import pytest

from hermes_cli.process_safety import (
    ProcessIdentity,
    classify_process_identity,
    get_process_start_time,
    is_pid_alive,
    safe_terminate_process,
)


def test_process_identity_dataclass():
    ident = ProcessIdentity(pid=1234, process_start_time=5678)
    assert ident.pid == 1234
    assert ident.process_start_time == 5678


def test_get_process_start_time_current_process():
    current_pid = os.getpid()
    st = get_process_start_time(current_pid)
    assert st is not None
    assert isinstance(st, int)
    assert st > 0


def test_get_process_start_time_invalid_pid():
    assert get_process_start_time(0) is None
    assert get_process_start_time(-1) is None
    assert get_process_start_time(99999999) is None


def test_is_pid_alive():
    assert is_pid_alive(os.getpid()) is True
    assert is_pid_alive(0) is False
    assert is_pid_alive(-1) is False
    assert is_pid_alive(99999999) is False


def test_classify_process_identity_matched(monkeypatch):
    monkeypatch.setattr("hermes_cli.process_safety.is_pid_alive", lambda p: True)
    monkeypatch.setattr("hermes_cli.process_safety.get_process_start_time", lambda p: 100000)

    res = classify_process_identity(1234, 100000)
    assert res == "matched"


def test_classify_process_identity_mismatch(monkeypatch):
    monkeypatch.setattr("hermes_cli.process_safety.is_pid_alive", lambda p: True)
    monkeypatch.setattr("hermes_cli.process_safety.get_process_start_time", lambda p: 100000)

    res = classify_process_identity(1234, 999999)
    assert res == "mismatch"


def test_classify_process_identity_unavailable_none_expected(monkeypatch):
    monkeypatch.setattr("hermes_cli.process_safety.is_pid_alive", lambda p: True)
    monkeypatch.setattr("hermes_cli.process_safety.get_process_start_time", lambda p: 100000)

    res = classify_process_identity(1234, None)
    assert res == "unavailable"


def test_classify_process_identity_unavailable_none_observed(monkeypatch):
    monkeypatch.setattr("hermes_cli.process_safety.is_pid_alive", lambda p: True)
    monkeypatch.setattr("hermes_cli.process_safety.get_process_start_time", lambda p: None)

    res = classify_process_identity(1234, 100000)
    assert res == "unavailable"


def test_classify_process_identity_dead_pid(monkeypatch):
    monkeypatch.setattr("hermes_cli.process_safety.is_pid_alive", lambda p: False)
    res = classify_process_identity(1234, 100000)
    assert res == "not_applicable"


def test_safe_terminate_process_refuses_on_mismatch():
    signal_mock = MagicMock()
    current_pid = os.getpid()
    real_st = get_process_start_time(current_pid)
    bogus_st = (real_st or 1000) + 5555

    res = safe_terminate_process(current_pid, bogus_st, signal_fn=signal_mock)
    assert signal_mock.call_count == 0
    assert res["termination_attempted"] is False
    assert res["process_identity"] == "mismatch"
    assert res["terminated"] is False


def test_safe_terminate_process_refuses_on_unavailable():
    signal_mock = MagicMock()
    current_pid = os.getpid()

    res = safe_terminate_process(current_pid, None, signal_fn=signal_mock)
    assert signal_mock.call_count == 0
    assert res["termination_attempted"] is False
    assert res["process_identity"] == "unavailable"
    assert res["terminated"] is False


def test_safe_terminate_process_matched_successful(monkeypatch):
    current_pid = 42424
    expected_st = 100000

    alive_states = [True, True, False]
    def mock_alive(p):
        return alive_states.pop(0) if alive_states else False

    monkeypatch.setattr("hermes_cli.process_safety.is_pid_alive", mock_alive)
    monkeypatch.setattr("hermes_cli.process_safety.get_process_start_time", lambda p: expected_st)

    signal_mock = MagicMock()
    res = safe_terminate_process(
        current_pid,
        expected_st,
        signal_fn=signal_mock,
        timeout_seconds=0.5,
        poll_interval=0.01,
    )

    assert signal_mock.call_count == 1
    assert signal_mock.call_args[0] == (current_pid, signal.SIGTERM)
    assert res["termination_attempted"] is True
    assert res["terminated"] is True
    assert res["sigkill"] is False
    assert res["process_identity"] == "matched"


def test_safe_terminate_process_handles_process_lookup_error(monkeypatch):
    current_pid = 42424
    expected_st = 100000

    monkeypatch.setattr("hermes_cli.process_safety.is_pid_alive", lambda p: True)
    monkeypatch.setattr("hermes_cli.process_safety.get_process_start_time", lambda p: expected_st)

    def mock_signal(p, s):
        raise ProcessLookupError("Process already gone")

    res = safe_terminate_process(
        current_pid,
        expected_st,
        signal_fn=mock_signal,
        timeout_seconds=0.5,
        poll_interval=0.01,
    )

    assert res["termination_attempted"] is True
    assert res["terminated"] is True
    assert res["sigkill"] is False


def test_safe_terminate_process_escalates_to_sigkill(monkeypatch):
    current_pid = 42424
    expected_st = 100000

    is_alive_flag = [True]
    def mock_alive(p):
        return is_alive_flag[0]

    monkeypatch.setattr("hermes_cli.process_safety.is_pid_alive", mock_alive)
    monkeypatch.setattr("hermes_cli.process_safety.get_process_start_time", lambda p: expected_st)

    signals_sent = []
    def mock_signal(p, s):
        signals_sent.append(s)
        if s == getattr(signal, "SIGKILL", signal.SIGTERM):
            is_alive_flag[0] = False

    res = safe_terminate_process(
        current_pid,
        expected_st,
        signal_fn=mock_signal,
        timeout_seconds=0.05,
        poll_interval=0.01,
    )

    assert res["termination_attempted"] is True
    assert res["sigkill"] is True
    assert res["terminated"] is True
    assert signal.SIGTERM in signals_sent
    sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
    assert sigkill in signals_sent


def test_safe_terminate_process_aborts_sigkill_on_recycled_pid_after_timeout(monkeypatch):
    current_pid = 42424
    expected_st = 100000

    start_times = [expected_st, 999999]
    def mock_start_time(p):
        return start_times.pop(0) if start_times else 999999

    monkeypatch.setattr("hermes_cli.process_safety.is_pid_alive", lambda p: True)
    monkeypatch.setattr("hermes_cli.process_safety.get_process_start_time", mock_start_time)

    signals_sent = []
    def mock_signal(p, s):
        signals_sent.append(s)

    res = safe_terminate_process(
        current_pid,
        expected_st,
        signal_fn=mock_signal,
        timeout_seconds=0.05,
        poll_interval=0.01,
    )

    assert res["termination_attempted"] is True
    assert res["sigkill"] is False
    assert len(signals_sent) == 1
    assert signals_sent[0] == signal.SIGTERM


def test_kanban_reclaim_refuses_kill_on_recycled_pid_e2e(monkeypatch, tmp_path):
    """End-to-end test proving kanban reclaim refuses to kill a recycled PID."""
    import sqlite3
    from unittest.mock import MagicMock
    from hermes_cli.kanban_db import connect, release_stale_claims, _claimer_id

    db_file = tmp_path / "board.db"
    conn = connect(db_file)

    # 1. Insert a running task with PID 42424 and birth time 100000
    host_prefix = f"{_claimer_id().split(':', 1)[0]}:"
    claim_lock = f"{host_prefix}test_worker_lock"
    now = int(time.time())

    conn.execute(
        """
        INSERT INTO tasks (
            id, title, status, claim_lock, claim_expires,
            worker_pid, worker_process_start_time, consecutive_failures, created_at
        ) VALUES (?, ?, 'running', ?, ?, ?, ?, 0, ?)
        """,
        ("task-recycled-1", "Test Task", claim_lock, now - 100, 42424, 100000, now),
    )
    conn.commit()

    # 2. Mock process safety: PID 42424 is alive, but its observed start time is 999999 (recycled PID)
    monkeypatch.setattr("hermes_cli.process_safety.is_pid_alive", lambda p: True)
    monkeypatch.setattr("hermes_cli.process_safety.get_process_start_time", lambda p: 999999)

    signal_mock = MagicMock()

    # 3. Trigger release_stale_claims with signal_mock
    reclaimed = release_stale_claims(conn, signal_fn=signal_mock)

    # 4. Assert signal_mock was NEVER called (kill refused due to recycled PID)
    assert signal_mock.call_count == 0

    # 5. Verify the task was reclaimed cleanly without harming the unrelated process
    row = conn.execute("SELECT status, claim_lock, worker_pid, worker_process_start_time FROM tasks WHERE id = ?", ("task-recycled-1",)).fetchone()
    assert row[0] != "running"
    assert row[1] is None
    assert row[2] is None
    assert row[3] is None
