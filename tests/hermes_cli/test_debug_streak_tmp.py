"""Temp debug for streak reset failure — run via pytest, deleted after."""
import pytest
from pathlib import Path
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    kb.init_db()
    return home


def _exited_status(code: int) -> int:
    return code << 8


def _drive_worker_exit(conn, tid, fake_pid, raw_status):
    import hermes_cli.kanban_db as _kb
    host_prefix = _kb._claimer_id().split(":", 1)[0]
    claimed = _kb.claim_task(conn, tid, claimer=f"{host_prefix}:mock")
    assert claimed is not None, "task was not claimable for the next attempt"
    _kb._set_worker_pid(conn, tid, fake_pid)
    _kb._record_worker_exit(fake_pid, raw_status)
    original_alive = _kb._pid_alive
    _kb._pid_alive = lambda p: False
    try:
        return _kb.detect_crashed_workers(conn)
    finally:
        _kb._pid_alive = original_alive


def _drive_protocol_violation(conn, tid, fake_pid):
    return _drive_worker_exit(conn, tid, fake_pid, 0)


def _drive_nonzero_crash(conn, tid, fake_pid):
    return _drive_worker_exit(conn, tid, fake_pid, 256)


def _dump_runs(conn, tid):
    rows = conn.execute(
        "SELECT id, outcome, status, error, metadata FROM task_runs "
        "WHERE task_id=? ORDER BY id", (tid,),
    ).fetchall()
    print(f"  --- runs for {tid} ---")
    for r in rows:
        print(
            f"  run id={r['id']} outcome={r['outcome']!r} status={r['status']!r} "
            f"error={(r['error'] or '')[:70]!r} meta={(r['metadata'] or '')[:100]!r}"
        )


def test_debug_streak_reset(kanban_home):
    import hermes_cli.kanban_db as _kb
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="reset", assignee="worker")

        _drive_protocol_violation(conn, tid, 993000)
        t = kb.get_task(conn, tid)
        print("after v1:", t.status, "cf=", t.consecutive_failures)
        _dump_runs(conn, tid)
        _drive_protocol_violation(conn, tid, 993001)
        print("after 2 violations:", kb.get_task(conn, tid).status,
              "cf=", kb.get_task(conn, tid).consecutive_failures)
        _dump_runs(conn, tid)

        _drive_nonzero_crash(conn, tid, 993002)
        task = kb.get_task(conn, tid)
        print("after crash:", task.status, "cf=", task.consecutive_failures)
        _dump_runs(conn, tid)

        _drive_protocol_violation(conn, tid, 993003)
        task = kb.get_task(conn, tid)
        print("after violation3:", task.status, "cf=", task.consecutive_failures)
        _dump_runs(conn, tid)
    finally:
        conn.close()
