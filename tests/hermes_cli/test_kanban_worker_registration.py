from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db(board="default")
    with kb.connect_closing(board="default") as connection:
        yield connection


def _claim(conn) -> tuple[str, kb.Task]:
    task_id = kb.create_task(conn, title="worker", assignee="coder")
    claimed = kb.claim_task(conn, task_id, claimer="host:claim")
    assert claimed is not None
    assert claimed.current_run_id is not None
    assert claimed.claim_lock == "host:claim"
    return task_id, claimed


def _run_id(claimed: kb.Task) -> int:
    assert claimed.current_run_id is not None
    return claimed.current_run_id


def _pid_state(conn, task_id: str, run_id: int):
    task_row = conn.execute(
        "SELECT worker_pid FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    run_row = conn.execute(
        "SELECT worker_pid FROM task_runs WHERE id = ?", (run_id,)
    ).fetchone()
    spawned = [event for event in kb.list_events(conn, task_id) if event.kind == "spawned"]
    return task_row["worker_pid"], run_row["worker_pid"], spawned


def test_register_worker_pid_updates_task_and_current_run_once(conn):
    task_id, claimed = _claim(conn)

    result = kb.register_worker_pid(
        conn,
        task_id,
        43210,
        expected_run_id=_run_id(claimed),
        expected_claim_lock=claimed.claim_lock,
        source="dispatcher",
    )
    again = kb.register_worker_pid(
        conn,
        task_id,
        43210,
        expected_run_id=_run_id(claimed),
        expected_claim_lock=claimed.claim_lock,
        source="worker_start",
    )

    task_pid, run_pid, spawned = _pid_state(
        conn, task_id, _run_id(claimed)
    )
    assert result == "registered"
    assert again == "already_registered"
    assert task_pid == run_pid == 43210
    assert len(spawned) == 1
    assert spawned[0].payload == {"pid": 43210, "source": "dispatcher"}


@pytest.mark.parametrize(
    ("pin_change", "expected_pid"),
    [
        ("run", None),
        ("claim", None),
    ],
)
def test_register_worker_pid_rejects_stale_identity(conn, pin_change, expected_pid):
    task_id, claimed = _claim(conn)
    run_id = _run_id(claimed)
    expected_run_id = run_id + 1 if pin_change == "run" else run_id
    expected_claim_lock = (
        "host:stale" if pin_change == "claim" else claimed.claim_lock
    )

    result = kb.register_worker_pid(
        conn,
        task_id,
        43210,
        expected_run_id=expected_run_id,
        expected_claim_lock=expected_claim_lock,
        source="dispatcher",
    )

    task_pid, run_pid, spawned = _pid_state(conn, task_id, run_id)
    assert result == "rejected"
    assert task_pid == run_pid == expected_pid
    assert spawned == []


def test_register_worker_pid_rejects_wrong_claim_lock(conn):
    task_id, claimed = _claim(conn)

    result = kb.register_worker_pid(
        conn,
        task_id,
        43210,
        expected_run_id=_run_id(claimed),
        expected_claim_lock="host:stale",
        source="dispatcher",
    )

    task_pid, run_pid, spawned = _pid_state(
        conn, task_id, _run_id(claimed)
    )
    assert result == "rejected"
    assert task_pid is None
    assert run_pid is None
    assert spawned == []


def test_register_worker_pid_rejects_different_existing_pid(conn):
    task_id, claimed = _claim(conn)
    conn.execute(
        "UPDATE tasks SET worker_pid = ? WHERE id = ?", (11111, task_id)
    )
    conn.execute(
        "UPDATE task_runs SET worker_pid = ? WHERE id = ?",
        (11111, _run_id(claimed)),
    )
    conn.commit()

    result = kb.register_worker_pid(
        conn,
        task_id,
        43210,
        expected_run_id=_run_id(claimed),
        expected_claim_lock=claimed.claim_lock,
        source="worker_start",
    )

    task_pid, run_pid, spawned = _pid_state(
        conn, task_id, _run_id(claimed)
    )
    assert result == "rejected"
    assert task_pid == run_pid == 11111
    assert spawned == []


def test_register_worker_pid_rejects_ended_run(conn):
    task_id, claimed = _claim(conn)
    conn.execute(
        "UPDATE task_runs SET status = 'failed', ended_at = 123 "
        "WHERE id = ?",
        (_run_id(claimed),),
    )
    conn.commit()

    result = kb.register_worker_pid(
        conn,
        task_id,
        43210,
        expected_run_id=_run_id(claimed),
        expected_claim_lock=claimed.claim_lock,
        source="dispatcher",
    )

    task_pid, run_pid, spawned = _pid_state(
        conn, task_id, _run_id(claimed)
    )
    assert result == "rejected"
    assert task_pid is None
    assert run_pid is None
    assert spawned == []
