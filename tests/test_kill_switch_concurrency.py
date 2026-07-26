"""CS-10a concurrency acceptance tests for serialized task fences."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.cost import gate_integration, ledger, task_cap_schema
from hermes_cli.cost.kill_switch import (
    KillSwitchTripped,
    PerTaskCapExceeded,
    is_task_killed,
    kill_task,
)
from hermes_cli.programme import gate as programme_gate
from hermes_cli.programme import init as programme_init
from hermes_cli.side_effects import schema as side_effects_schema


@pytest.fixture
def concurrency_env(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(ledger, "DB_PATH", db_path)
    monkeypatch.setattr(task_cap_schema, "DB_PATH", db_path)
    monkeypatch.setattr(programme_init, "DB_PATH", db_path)
    monkeypatch.setattr(programme_gate, "HALT_SIGNAL_PATH", tmp_path / "halt")
    monkeypatch.setattr(side_effects_schema, "DB_PATH", db_path)
    monkeypatch.setattr(
        gate_integration.telegram_alert,
        "send_bridge_alert",
        lambda _message: None,
    )
    ledger._MIGRATED_PATHS.clear()
    task_cap_schema._MIGRATED_PATHS.clear()
    side_effects_schema._MIGRATED_PATHS.clear()
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    conn = kb.connect(db_path)
    conn.close()
    programme_init.migrate(db_path)
    ledger.migrate(db_path)
    task_cap_schema.migrate(db_path)
    side_effects_schema.migrate(db_path)
    yield db_path
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))


def _claimed_task(db_path, cap: float) -> str:
    conn = kb.connect(db_path)
    try:
        task_id = kb.create_task(
            conn,
            title="concurrent task cap",
            assignee="platform",
            initial_status="running",
        )
        claimed = kb.claim_task(
            conn,
            task_id,
            lane="platform",
            task_cap_aud=cap,
            claimer="concurrency-test",
        )
        assert claimed is not None
        return task_id
    finally:
        conn.close()


def _write(db_path, task_id: str, amount: float):
    return ledger.record_call(
        task_id=task_id,
        lane="platform",
        vendor="anthropic",
        model="anthropic/test",
        amount_aud=amount,
        profile="test",
        route="test",
        enforce_task_cap=True,
        db_path=db_path,
    )


def _cost_count(db_path, task_id: str) -> int:
    with sqlite3.connect(db_path) as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM cost_ledger WHERE task_id=?",
                (task_id,),
            ).fetchone()[0]
        )


def test_two_writers_racing_below_cap_both_succeed_when_sum_below(
    concurrency_env,
):
    task_id = _claimed_task(concurrency_env, 0.20)
    barrier = threading.Barrier(2)

    def writer(_index):
        barrier.wait(timeout=5)
        return _write(concurrency_env, task_id, 0.05)

    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(writer, range(2)))
    assert len(rows) == 2
    assert _cost_count(concurrency_env, task_id) == 2
    assert is_task_killed(task_id, db_path=concurrency_env) is None


def test_two_writers_racing_at_cap_only_one_succeeds_and_kills(
    concurrency_env,
):
    task_id = _claimed_task(concurrency_env, 0.10)
    _write(concurrency_env, task_id, 0.04)
    barrier = threading.Barrier(2)

    def writer(_index):
        barrier.wait(timeout=5)
        try:
            return _write(concurrency_env, task_id, 0.06)
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(writer, range(2)))
    assert sum(not isinstance(item, Exception) for item in results) == 1
    assert sum(isinstance(item, PerTaskCapExceeded) for item in results) == 1
    assert _cost_count(concurrency_env, task_id) == 2
    assert is_task_killed(task_id, db_path=concurrency_env) is not None


def test_kill_from_cli_during_active_write_fences_immediately(
    concurrency_env,
):
    task_id = _claimed_task(concurrency_env, 1.0)
    kill_conn = task_cap_schema.connect(concurrency_env)
    kill_conn.execute("BEGIN IMMEDIATE")
    kill_task(
        task_id=task_id,
        killed_by="operator",
        reason="operator",
        conn=kill_conn,
    )
    started = threading.Event()

    def waiting_writer():
        started.set()
        try:
            return _write(concurrency_env, task_id, 0.01)
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(waiting_writer)
        assert started.wait(timeout=5)
        kill_conn.execute("COMMIT")
        result = future.result(timeout=10)
    kill_conn.close()
    assert isinstance(result, KillSwitchTripped)
    assert _cost_count(concurrency_env, task_id) == 0


def test_reclaim_after_kill_admits_fresh_task_with_new_kill_row_deleted(
    concurrency_env,
):
    task_id = _claimed_task(concurrency_env, 0.10)
    kill_task(
        task_id=task_id,
        killed_by="test",
        reason="test",
        db_path=concurrency_env,
    )
    with sqlite3.connect(concurrency_env) as conn:
        conn.execute(
            """
            UPDATE tasks
               SET status='ready', claim_lock=NULL, claim_expires=NULL
             WHERE id=?
            """,
            (task_id,),
        )
    conn = kb.connect(concurrency_env)
    try:
        fresh = kb.claim_task(
            conn,
            task_id,
            lane="platform",
            task_cap_aud=0.20,
            claimer="fresh-claim",
        )
    finally:
        conn.close()
    assert fresh is not None
    assert is_task_killed(task_id, db_path=concurrency_env) is None
    with sqlite3.connect(concurrency_env) as conn:
        cap = conn.execute(
            "SELECT task_cap_aud FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()[0]
    assert cap == pytest.approx(0.20)
