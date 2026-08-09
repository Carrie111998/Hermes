"""Reconcile matrix tests (mock systemctl)."""

from __future__ import annotations

import pytest

from hermes_cli import dev_executor as ex


@pytest.mark.parametrize(
    "unit_active,pid_match,candidate,phase,attempts,max_attempts,expected_action,expected_phase,expected_reason",
    [
        (True, True, "abc", "RUNNING", 1, 2, "adopt", "RUNNING", None),
        (True, False, None, "RUNNING", 1, 2, "unit_gone", None, "pid_mismatch"),
        (False, False, "abc123", "RUNNING", 1, 2, "resume", "VERIFYING", None),
        (False, False, None, "RUNNING", 1, 2, "retry", "RUNNING", None),
        (False, False, None, "RUNNING", 2, 2, "block", None, "executor_restarted"),
    ],
)
def test_reconcile_task_state_matrix(
    unit_active,
    pid_match,
    candidate,
    phase,
    attempts,
    max_attempts,
    expected_action,
    expected_phase,
    expected_reason,
):
    decision = ex.reconcile_task_state(
        {"phase": phase, "candidate_commit": candidate},
        unit_active=unit_active,
        pid_match=pid_match,
        candidate_commit=candidate,
        attempts_used=attempts,
        max_attempts=max_attempts,
    )
    assert decision.action == expected_action
    if expected_phase:
        assert decision.phase == expected_phase
    if expected_reason:
        assert decision.reason == expected_reason


def test_reconcile_board_adopts_active_unit(kanban_home_fixture):
    from hermes_cli import kanban_db as kb

    conn = kanban_home_fixture
    task_id = kb.create_task(
        conn,
        title="t",
        body="{}",
        workspace_kind="scratch",
        board="dev",
    )
    conn.execute(
        "UPDATE tasks SET status='running' WHERE id=?",
        (task_id,),
    )
    conn.execute(
        "INSERT INTO task_runs (task_id, status, started_at, metadata) VALUES (?, 'running', ?, ?)",
        (
            task_id,
            1,
            '{"dev_pipeline": {"phase": "RUNNING", "unit_name": "hermes-dev-t-1", "unit_pid": 99, "host_start_time": 42, "candidate_commit": "deadbeef"}}',
        ),
    )
    conn.commit()

    def fake_active(unit):
        return unit == "hermes-dev-t-1", "active"

    def fake_pid(pid, start):
        return pid == 99 and start == 42

    cfg = {"board": "dev", "max_attempts": 2}
    decisions = ex.reconcile_board(
        conn,
        cfg,
        is_active_fn=fake_active,
        pid_match_fn=fake_pid,
    )
    assert any(d.adopt for d in decisions)


@pytest.fixture
def kanban_home_fixture(tmp_path, monkeypatch):
    from pathlib import Path

    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.create_board("dev")
    return kb.connect(board="dev")
