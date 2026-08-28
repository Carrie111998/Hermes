"""Clean worker exits without a lifecycle handoff fail closed, not as crashes."""

from __future__ import annotations

import os
import signal
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _claim_with_dead_worker(conn, task_id: str, pid: int):
    host = kb._claimer_id().split(":", 1)[0]
    task = kb.claim_task(conn, task_id, claimer=f"{host}:test")
    assert task is not None
    kb._set_worker_pid(conn, task_id, pid)
    return task


def _reap_exit(monkeypatch, conn, task_id: str, pid: int, raw_status: int):
    claimed = _claim_with_dead_worker(conn, task_id, pid)
    kb._record_worker_exit(pid, raw_status)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    kb.detect_crashed_workers(conn)
    return claimed


def test_worker_spawn_precreates_and_resumes_exact_run_session(
    kanban_home, monkeypatch, tmp_path
):
    from hermes_state import SessionDB

    captured = {}

    class _Proc:
        pid = 4321

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        return _Proc()

    monkeypatch.setattr("subprocess.Popen", _fake_popen)
    monkeypatch.setattr(kb, "_retag_legacy_worker_sessions", lambda _root: None)
    monkeypatch.setattr(kb, "worker_logs_dir", lambda board=None: tmp_path / "logs")
    monkeypatch.setattr(kb, "kanban_db_path", lambda board=None: tmp_path / "kanban.db")
    monkeypatch.setattr(kb, "workspaces_root", lambda board=None: tmp_path / "workspaces")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = kb.Task(
        id="t_b21733fb",
        title="ship it",
        body=None,
        assignee="default",
        status="running",
        priority=0,
        created_by=None,
        created_at=0,
        started_at=0,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=str(workspace),
        claim_lock="host:test",
        claim_expires=999,
        tenant=None,
        current_run_id=42,
    )

    kb._default_spawn(task, str(workspace))

    session_id = kb._worker_session_id(task.id, 42)
    assert session_id is not None
    resume_at = captured["cmd"].index("--resume")
    assert captured["cmd"][resume_at + 1] == session_id
    assert "--create-if-missing" not in captured["cmd"]

    session_db = SessionDB(
        db_path=Path(captured["env"]["HERMES_HOME"]) / "state.db"
    )
    try:
        session = session_db.get_session(session_id)
        assert session is not None
        assert session["source"] == "kanban"
    finally:
        session_db.close()


def test_worker_session_prepare_rejects_conflicting_source_created_during_race(
    monkeypatch, tmp_path
):
    import hermes_state

    class _RacingSessionDB:
        def __init__(self, db_path):
            self.session = None

        def get_session(self, session_id):
            return self.session

        def create_session(self, session_id, source):
            # Another writer won the same id with a user-visible source.
            self.session = {"id": session_id, "source": "cli"}

        def close(self):
            pass

    monkeypatch.setattr(hermes_state, "SessionDB", _RacingSessionDB)

    with pytest.raises(RuntimeError, match="source 'cli'"):
        kb._prepare_worker_session(str(tmp_path), "kanban-t_race-run-1")


def test_clean_exit_without_handoff_blocks_with_typed_outcome_and_session(
    kanban_home, monkeypatch
):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="clean no handoff", assignee="worker")
        claimed = _reap_exit(monkeypatch, conn, task_id, 991001, 0)

        task = kb.get_task(conn, task_id)
        run = kb.latest_run(conn, task_id)
        events = kb.list_events(conn, task_id)
        assert task is not None
        assert run is not None

        assert task.status == "blocked"
        assert task.block_kind == "needs_input"
        assert task.current_run_id is None
        assert task.consecutive_failures == 0
        assert run.status == "blocked"
        assert run.outcome == "handoff_missing"
        assert run.metadata["worker_session_id"] == kb._worker_session_id(
            task_id, claimed.current_run_id
        )
        assert run.metadata["exit_code"] == 0
        assert not any(event.kind == "crashed" for event in events)
        assert not any(event.kind == "gave_up" for event in events)
        blocked = [event for event in events if event.kind == "blocked"][-1]
        assert blocked.payload is not None
        assert blocked.run_id == run.id
        assert blocked.payload["outcome"] == "handoff_missing"
        assert blocked.payload["worker_session_id"] == run.metadata["worker_session_id"]

        spawned = []
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda *args, **kwargs: spawned.append((args, kwargs)),
            max_spawn=1,
        )
        assert spawned == []
        assert result.spawned == []
    finally:
        conn.close()


def test_explicit_terminal_transitions_keep_their_outcomes(kanban_home):
    conn = kb.connect()
    try:
        completed = kb.create_task(conn, title="complete", assignee="worker")
        complete_run = kb.claim_task(conn, completed)
        assert complete_run is not None
        assert kb.complete_task(
            conn,
            completed,
            summary="done",
            expected_run_id=complete_run.current_run_id,
        )

        blocked = kb.create_task(conn, title="block", assignee="worker")
        block_run = kb.claim_task(conn, blocked)
        assert block_run is not None
        assert kb.block_task(
            conn,
            blocked,
            reason="need a decision",
            kind="needs_input",
            expected_run_id=block_run.current_run_id,
        )

        review = kb.create_task(conn, title="review", assignee="worker")
        review_run = kb.claim_task(conn, review)
        assert review_run is not None
        assert kb.request_review(
            conn,
            review,
            summary="ready",
            expected_run_id=review_run.current_run_id,
        )

        assert (kb.get_task(conn, completed).status, kb.latest_run(conn, completed).outcome) == (
            "done",
            "completed",
        )
        assert (kb.get_task(conn, blocked).status, kb.latest_run(conn, blocked).outcome) == (
            "blocked",
            "blocked",
        )
        assert (kb.get_task(conn, review).status, kb.latest_run(conn, review).outcome) == (
            "review",
            "review_requested",
        )
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("raw_status", "exit_kind", "exit_code"),
    [
        (7 << 8, "nonzero_exit", 7),
        (signal.SIGKILL, "signaled", signal.SIGKILL),
    ],
)
@pytest.mark.skipif(os.name == "nt", reason="raw wait statuses are POSIX-only")
def test_genuine_worker_failures_remain_crashes(
    kanban_home, monkeypatch, raw_status, exit_kind, exit_code
):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title=exit_kind, assignee="worker")
        _reap_exit(monkeypatch, conn, task_id, 991002, raw_status)

        task = kb.get_task(conn, task_id)
        run = kb.latest_run(conn, task_id)
        event = [event for event in kb.list_events(conn, task_id) if event.kind == "crashed"][-1]
        assert task is not None
        assert run is not None
        assert event.payload is not None

        assert task.status == "ready"
        assert task.consecutive_failures == 1
        assert run.outcome == "crashed"
        assert event.payload["exit_kind"] == exit_kind
        assert event.payload["exit_code"] == exit_code
    finally:
        conn.close()


def test_timeout_remains_timed_out(kanban_home, monkeypatch):
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="timeout",
            assignee="worker",
            max_runtime_seconds=1,
        )
        claimed = _claim_with_dead_worker(conn, task_id, 991003)
        conn.execute(
            "UPDATE task_runs SET started_at = started_at - 60 WHERE id = ?",
            (claimed.current_run_id,),
        )
        conn.commit()
        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)

        assert task_id in kb.enforce_max_runtime(conn, signal_fn=lambda _pid, _sig: None)
        task = kb.get_task(conn, task_id)
        run = kb.latest_run(conn, task_id)
        assert task is not None
        assert run is not None
        assert task.status == "ready"
        assert run.outcome == "timed_out"
    finally:
        conn.close()
