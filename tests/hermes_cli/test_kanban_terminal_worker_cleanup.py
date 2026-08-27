"""Regression coverage for run-scoped Kanban worker cleanup.

A worker can make its task/run terminal before its one-shot Python process has
actually exited.  The claim and PID must remain attached to that ended run
until the dispatcher has proved the exact process is gone and reaped it; a
ready/dependency task must never respawn beside the old writer.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import ANY

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _claimed_with_process(conn, monkeypatch, *, goal_mode: bool = False):
    task_id = kb.create_task(
        conn,
        title="terminal cleanup",
        assignee="default",
        goal_mode=goal_mode,
    )
    claimed = kb.claim_task(conn, task_id, claimer=kb._claimer_id())
    assert claimed is not None
    monkeypatch.setattr(kb, "_worker_start_time", lambda _pid: 777_123)
    kb._set_worker_pid(conn, task_id, 424_242)
    return task_id, claimed.current_run_id


def test_additive_worker_fields_preserve_legacy_positional_constructors():
    task = kb.Task(
        "t_legacy",
        "legacy task",
        None,
        "default",
        "running",
        1,
        "creator",
        10,
        None,
        None,
        "scratch",
        None,
        "host:pid",
        20,
        None,
        "branch",
        "project",
        "result",
        "idempotency",
        3,
        424_242,
        "legacy error",
    )
    assert task.worker_pid == 424_242
    assert task.last_failure_error == "legacy error"
    assert task.worker_start_time is None

    run = kb.Run(
        7,
        "t_legacy",
        "default",
        "step",
        "running",
        "host:pid",
        20,
        424_242,
        300,
        11,
        10,
        None,
        None,
        None,
        {"legacy": True},
        "legacy run error",
    )
    assert run.worker_pid == 424_242
    assert run.max_runtime_seconds == 300
    assert run.error == "legacy run error"
    assert run.worker_start_time is None

    dispatch = kb.DispatchResult(
        1,
        2,
        ["t_orphan"],
        [("t_spawn", "default", "/workspace")],
    )
    assert dispatch.reconciled_orphans == ["t_orphan"]
    assert dispatch.spawned == [("t_spawn", "default", "/workspace")]
    assert dispatch.cleaned_terminal == []


@pytest.mark.parametrize("goal_mode", [False, True])
@pytest.mark.parametrize(
    "transition",
    ["complete", "block", "dependency", "review", "archive", "schedule"],
)
def test_terminal_transition_retains_process_claim_until_cleanup(
    kanban_home, monkeypatch, goal_mode, transition,
):
    """Classic and goal workers keep run/claim/process identity after handoff."""
    with kb.connect() as conn:
        task_id, run_id = _claimed_with_process(
            conn, monkeypatch, goal_mode=goal_mode,
        )
        if transition == "complete":
            assert kb.complete_task(
                conn, task_id, summary="done", expected_run_id=run_id,
            )
            expected_status = "done"
        elif transition == "review":
            assert kb.request_review(
                conn,
                task_id,
                summary="ready for review",
                expected_run_id=run_id,
            )
            expected_status = "review"
        elif transition == "archive":
            assert kb.archive_task(conn, task_id)
            expected_status = "archived"
        elif transition == "schedule":
            assert kb.schedule_task(
                conn,
                task_id,
                reason="retry later",
                expected_run_id=run_id,
            )
            expected_status = "scheduled"
        else:
            kind = "dependency" if transition == "dependency" else "needs_input"
            assert kb.block_task(
                conn,
                task_id,
                reason="waiting",
                kind=kind,
                expected_run_id=run_id,
            )
            expected_status = "todo" if kind == "dependency" else "blocked"

        task_row = conn.execute(
            "SELECT status, claim_lock, worker_pid, worker_start_time, "
            "current_run_id FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        run_row = conn.execute(
            "SELECT ended_at, claim_lock, worker_pid, worker_start_time "
            "FROM task_runs WHERE id = ?",
            (run_id,),
        ).fetchone()

        assert task_row["status"] == expected_status
        assert task_row["current_run_id"] is None
        assert task_row["claim_lock"] is not None
        assert task_row["worker_pid"] == 424_242
        assert task_row["worker_start_time"] == 777_123
        assert run_row["ended_at"] is not None
        assert run_row["claim_lock"] == task_row["claim_lock"]
        assert run_row["worker_pid"] == 424_242
        assert run_row["worker_start_time"] == 777_123


def test_goal_budget_exhaustion_retains_worker_until_dispatch_cleanup(
    kanban_home, monkeypatch,
):
    from hermes_cli import goals

    with kb.connect() as conn:
        task_id, run_id = _claimed_with_process(
            conn, monkeypatch, goal_mode=True,
        )
        monkeypatch.setattr(
            goals,
            "judge_goal",
            lambda *_args, **_kwargs: (
                "continue", "not done", False, None, False
            ),
        )

        result = goals.run_kanban_goal_loop(
            task_id=task_id,
            goal_text="finish it",
            run_turn=lambda _prompt: pytest.fail("budget must stop another turn"),
            task_status_fn=lambda: kb.get_task(conn, task_id).status,
            block_fn=lambda reason: kb.block_task(
                conn,
                task_id,
                reason=reason,
                kind="needs_input",
                expected_run_id=run_id,
            ),
            max_turns=1,
            first_response="still working",
        )

        assert result["outcome"] == "blocked_budget"
        task = kb.get_task(conn, task_id)
        run = kb.get_run(conn, run_id)
        assert task is not None and task.status == "blocked"
        assert task.worker_pid == 424_242
        assert task.worker_start_time == 777_123
        assert run is not None and run.ended_at is not None
        assert run.worker_pid == 424_242
        assert run.worker_start_time == 777_123


def test_review_changes_handoff_retains_reviewer_process_identity(
    kanban_home, monkeypatch,
):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="review changes", assignee="implementer"
        )
        implementation = kb.claim_task(conn, task_id)
        assert implementation is not None
        assert kb.request_review(
            conn,
            task_id,
            summary="ready",
            expected_run_id=implementation.current_run_id,
        )
        review = kb.claim_review_task(conn, task_id, claimer="reviewer")
        assert review is not None
        monkeypatch.setattr(kb, "_worker_start_time", lambda _pid: 777_123)
        kb._set_worker_pid(conn, task_id, 424_242)

        assert kb.request_changes(
            conn,
            task_id,
            reason="fix it",
            expected_run_id=review.current_run_id,
        )
        task = kb.get_task(conn, task_id)
        run = kb.get_run(conn, review.current_run_id)
        assert task is not None and task.status == "ready"
        assert task.worker_pid == 424_242
        assert task.worker_start_time == 777_123
        assert run is not None and run.outcome == "changes_requested"
        assert run.worker_pid == 424_242
        assert run.worker_start_time == 777_123


def test_review_reopen_cannot_drop_pending_worker_identity(
    kanban_home, monkeypatch,
):
    """A fast operator reopen cannot spawn beside the implementation handoff."""
    with kb.connect() as conn:
        task_id, run_id = _claimed_with_process(conn, monkeypatch)
        assert kb.request_review(
            conn,
            task_id,
            summary="ready",
            expected_run_id=run_id,
        )
        assert kb.reopen_review_task(conn, task_id)
        reopened = kb.get_task(conn, task_id)
        assert reopened is not None and reopened.status == "ready"
        assert reopened.claim_lock is not None
        assert reopened.worker_pid == 424_242
        assert reopened.worker_start_time == 777_123
        assert kb.claim_task(conn, task_id) is None

        monkeypatch.setattr(
            kb,
            "_terminate_reclaimed_worker",
            lambda *_args, **_kwargs: {
                "prev_pid": 424_242,
                "host_local": True,
                "identity_verified": True,
                "termination_attempted": True,
                "terminated": True,
                "still_alive": False,
                "sigkill": False,
                "reaped": True,
            },
        )
        assert kb.cleanup_terminal_workers(conn) == [task_id]
        assert kb.claim_task(conn, task_id) is not None


def test_late_spawn_registration_after_reclaim_cannot_create_duplicate_writer(
    kanban_home, monkeypatch,
):
    """A terminal transition between Popen and PID persistence stays guarded."""
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="spawn race", assignee="default")
        claimed = kb.claim_task(conn, task_id, claimer=kb._claimer_id())
        assert claimed is not None
        run_id = claimed.current_run_id
        claim_lock = claimed.claim_lock
        assert run_id is not None and claim_lock is not None

        # Simulate an operator reclaim racing the dispatcher's post-Popen PID
        # persistence. No PID existed yet, so the transition legitimately ends
        # the run and clears its claim.
        assert kb.reclaim_task(conn, task_id)
        assert kb.get_task(conn, task_id).status == "ready"

        monkeypatch.setattr(kb, "_worker_start_time", lambda _pid: 777_123)
        monkeypatch.setattr(
            kb,
            "_terminate_reclaimed_worker",
            lambda *_args, **_kwargs: {
                "host_local": True,
                "identity_verified": True,
                "termination_attempted": True,
                "terminated": False,
                "still_alive": True,
                "reaped": False,
            },
        )
        kb._set_worker_pid(
            conn,
            task_id,
            424_242,
            expected_run_id=run_id,
            expected_claim_lock=claim_lock,
        )

        guarded = kb.get_task(conn, task_id)
        assert guarded is not None and guarded.status == "ready"
        assert guarded.claim_lock == claim_lock
        assert guarded.worker_pid == 424_242
        assert guarded.worker_start_time == 777_123
        assert kb.claim_task(conn, task_id) is None
        ended = kb.get_run(conn, run_id)
        assert ended is not None and ended.ended_at is not None
        assert ended.claim_lock == claim_lock
        assert ended.worker_pid == 424_242

        with kb.write_txn(conn):
            due = int(time.time()) - 1
            conn.execute(
                "UPDATE tasks SET claim_expires = ? WHERE id = ?",
                (due, task_id),
            )
            conn.execute(
                "UPDATE task_runs SET claim_expires = ? WHERE id = ?",
                (due, run_id),
            )
        monkeypatch.setattr(
            kb,
            "_terminate_reclaimed_worker",
            lambda *_args, **_kwargs: {
                "host_local": True,
                "identity_verified": True,
                "termination_attempted": True,
                "terminated": True,
                "still_alive": False,
                "reaped": True,
            },
        )
        assert kb.cleanup_terminal_workers(conn) == [task_id]
        assert kb.claim_task(conn, task_id) is not None


def test_terminal_cleanup_clears_claim_only_after_exact_worker_is_gone(
    kanban_home, monkeypatch,
):
    with kb.connect() as conn:
        task_id, run_id = _claimed_with_process(conn, monkeypatch)
        assert kb.complete_task(
            conn, task_id, summary="done", expected_run_id=run_id,
        )

        calls = []

        def _terminated(pid, claim_lock, worker_start_time=None, **_kwargs):
            calls.append((pid, claim_lock, worker_start_time))
            return {
                "prev_pid": pid,
                "host_local": True,
                "identity_verified": True,
                "termination_attempted": True,
                "terminated": True,
                "still_alive": False,
                "sigkill": False,
                "reaped": True,
            }

        monkeypatch.setattr(kb, "_terminate_reclaimed_worker", _terminated)
        assert kb.cleanup_terminal_workers(conn) == [task_id]
        assert calls == [(424_242, ANY, 777_123)]

        task_row = conn.execute(
            "SELECT status, claim_lock, claim_expires, worker_pid, "
            "worker_start_time FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        run_row = conn.execute(
            "SELECT worker_pid, worker_start_time FROM task_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        assert task_row["status"] == "done"
        assert task_row["claim_lock"] is None
        assert task_row["claim_expires"] is None
        assert task_row["worker_pid"] is None
        assert task_row["worker_start_time"] is None
        assert run_row["worker_pid"] is None
        assert run_row["worker_start_time"] is None

        event = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'worker_cleanup' "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert event is not None
        assert json.loads(event["payload"])["run_id"] == run_id


def test_terminal_cleanup_is_scoped_to_the_open_board(tmp_path, monkeypatch):
    db_a = tmp_path / "board-a.db"
    db_b = tmp_path / "board-b.db"
    with kb.connect(db_a) as conn_a, kb.connect(db_b) as conn_b:
        task_a, run_a = _claimed_with_process(conn_a, monkeypatch)
        task_b, run_b = _claimed_with_process(conn_b, monkeypatch)
        assert kb.complete_task(
            conn_a, task_a, summary="a", expected_run_id=run_a
        )
        assert kb.complete_task(
            conn_b, task_b, summary="b", expected_run_id=run_b
        )
        monkeypatch.setattr(
            kb,
            "_terminate_reclaimed_worker",
            lambda pid, claim_lock, **_kw: {
                "prev_pid": pid,
                "host_local": True,
                "identity_verified": True,
                "termination_attempted": True,
                "terminated": True,
                "still_alive": False,
                "sigkill": False,
                "reaped": True,
            },
        )

        assert kb.cleanup_terminal_workers(conn_a) == [task_a]
        assert kb.get_task(conn_a, task_a).claim_lock is None
        untouched = kb.get_task(conn_b, task_b)
        assert untouched is not None
        assert untouched.claim_lock is not None
        assert untouched.worker_pid == 424_242


def test_dependency_handoff_cannot_respawn_while_old_worker_survives(
    kanban_home, monkeypatch,
):
    """A dependency wait promoted to ready still carries the old live claim."""
    with kb.connect() as conn:
        task_id, run_id = _claimed_with_process(conn, monkeypatch, goal_mode=True)
        assert kb.block_task(
            conn,
            task_id,
            reason="waiting on parent",
            kind="dependency",
            expected_run_id=run_id,
        )
        kb.recompute_ready(conn)
        promoted = kb.get_task(conn, task_id)
        assert promoted is not None
        assert promoted.status == "ready"

        monkeypatch.setattr(
            kb,
            "_terminate_reclaimed_worker",
            lambda *args, **kwargs: {
                "prev_pid": 424_242,
                "host_local": True,
                "identity_verified": True,
                "termination_attempted": True,
                "terminated": False,
                "still_alive": True,
                "sigkill": True,
                "reaped": False,
            },
        )
        assert kb.cleanup_terminal_workers(conn) == []
        assert kb.claim_task(conn, task_id) is None
        held = kb.get_task(conn, task_id)
        assert held is not None
        assert held.status == "ready"
        assert held.claim_lock is not None
        assert held.worker_pid == 424_242

        monkeypatch.setattr(
            kb,
            "_terminate_reclaimed_worker",
            lambda *_args, **_kwargs: pytest.fail(
                "cleanup retry must honor the deferred claim expiry"
            ),
        )
        assert kb.cleanup_terminal_workers(conn) == []

        # A failed kill is deferred instead of retried on every dispatcher
        # tick. Make the retry due before modelling the next cleanup pass.
        retry_due = int(time.time()) - 1
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET claim_expires = ? WHERE id = ?",
                (retry_due, task_id),
            )
            conn.execute(
                "UPDATE task_runs SET claim_expires = ? WHERE id = ?",
                (retry_due, run_id),
            )

        monkeypatch.setattr(
            kb,
            "_terminate_reclaimed_worker",
            lambda *args, **kwargs: {
                "prev_pid": 424_242,
                "host_local": True,
                "identity_verified": True,
                "termination_attempted": True,
                "terminated": True,
                "still_alive": False,
                "sigkill": True,
                "reaped": True,
            },
        )
        assert kb.cleanup_terminal_workers(conn) == [task_id]
        successor = kb.claim_task(conn, task_id)
        assert successor is not None
        assert successor.current_run_id != run_id


def test_pid_reuse_mismatch_is_never_signalled(monkeypatch):
    """A recycled PID means the original worker is gone, not a kill target."""
    claim_lock = kb._claimer_id()
    monkeypatch.setattr(kb, "_worker_start_time", lambda _pid: 999_999)

    def _must_not_kill(*_args, **_kwargs):
        pytest.fail("recycled PID must not be signalled")

    monkeypatch.setattr("agent.deadline.terminate_process_tree", _must_not_kill)
    result = kb._terminate_reclaimed_worker(
        123_456,
        claim_lock,
        worker_start_time=111_111,
    )
    assert result["identity_verified"] is False
    assert result["identity_mismatch"] is True
    assert result["terminated"] is True
    assert result["still_alive"] is False


def test_unavailable_identity_probe_holds_claim_and_never_signals(monkeypatch):
    """A transient fingerprint failure must not turn a live PID into proof of exit."""
    claim_lock = kb._claimer_id()
    monkeypatch.setattr(kb, "_worker_start_time", lambda _pid: None)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)

    def _must_not_kill(*_args, **_kwargs):
        pytest.fail("an unverifiable live PID must not be signalled")

    monkeypatch.setattr("agent.deadline.terminate_process_tree", _must_not_kill)
    result = kb._terminate_reclaimed_worker(
        123_456,
        claim_lock,
        worker_start_time=111_111,
    )
    assert result["identity_unavailable"] is True
    assert result["termination_attempted"] is False
    assert result["terminated"] is False
    assert result["still_alive"] is True
    assert kb._worker_survived_termination(result) is True


def test_legacy_claim_without_boot_fingerprint_fails_closed(monkeypatch):
    host = kb._claimer_id().split(":", 1)[0]
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(kb, "_worker_start_time", lambda _pid: 111_111)

    def _must_not_kill(*_args, **_kwargs):
        pytest.fail("a pre-boot-fingerprint claim must not signal a live PID")

    monkeypatch.setattr("agent.deadline.terminate_process_tree", _must_not_kill)
    result = kb._terminate_reclaimed_worker(
        123_456,
        f"{host}:legacy-worker",
        worker_start_time=111_111,
    )
    assert result["identity_unavailable"] is True
    assert result["termination_attempted"] is False
    assert result["terminated"] is False
    assert result["still_alive"] is True


def test_reboot_incarnation_mismatch_never_signals_reused_pid(monkeypatch):
    host = kb._claimer_id().split(":", 1)[0]
    claim_lock = f"{host}:boot=previous-boot:worker"
    monkeypatch.setattr(kb, "_worker_boot_id", lambda: "current-boot")
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(kb, "_worker_start_time", lambda _pid: 111_111)

    def _must_not_kill(*_args, **_kwargs):
        pytest.fail("a previous-boot PID identity must never be signalled")

    monkeypatch.setattr("agent.deadline.terminate_process_tree", _must_not_kill)
    assert kb._worker_identity_alive(123_456, 111_111, claim_lock) is False
    result = kb._terminate_reclaimed_worker(
        123_456,
        claim_lock,
        worker_start_time=111_111,
    )
    assert result["identity_mismatch"] is True
    assert result["boot_mismatch"] is True
    assert result["termination_attempted"] is False
    assert result["terminated"] is True
    assert result["still_alive"] is False


def test_pid_reuse_does_not_extend_expired_claim(
    kanban_home, monkeypatch,
):
    """TTL recovery compares the full worker identity, not a recycled PID."""
    with kb.connect() as conn:
        task_id, _run_id = _claimed_with_process(conn, monkeypatch)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = ? "
                "WHERE id = ?",
                (int(time.time()) - 10, int(time.time()), task_id),
            )
        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)
        monkeypatch.setattr(kb, "_worker_start_time", lambda _pid: 999_999)

        assert kb.release_stale_claims(conn) == 1
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "ready"
        assert task.claim_lock is None
        assert task.worker_pid is None
        kinds = [event.kind for event in kb.list_events(conn, task_id)]
        assert "claim_extended" not in kinds
        assert "reclaimed" in kinds


def test_terminal_one_shot_finalizer_skips_background_linger(monkeypatch):
    import cli as cli_mod
    from tools.process_registry import process_registry

    calls = []

    class _CLI:
        def _release_active_session(self):
            calls.append("release")

    monkeypatch.setattr(cli_mod, "_kanban_worker_run_is_terminal", lambda: True)
    monkeypatch.setattr(
        cli_mod,
        "_wait_for_oneshot_background_completions",
        lambda _cli: pytest.fail("terminal worker must not enter the 600s linger"),
    )
    monkeypatch.setattr(process_registry, "kill_all", lambda **_kw: calls.append("kill_all"))
    monkeypatch.setattr(cli_mod, "_flush_one_shot_session_store", lambda _cli: None)
    monkeypatch.setattr(cli_mod, "_notify_single_query_session_finalize", lambda _cli: None)
    monkeypatch.setattr(cli_mod, "_run_cleanup", lambda **_kw: None)
    monkeypatch.setattr(
        cli_mod,
        "_shutdown_terminal_kanban_worker",
        lambda: calls.append("hard_exit"),
    )

    cli_mod._finalize_single_query(_CLI())
    assert calls == ["kill_all", "release", "hard_exit"]


@pytest.mark.parametrize("failure_site", ["notify", "cleanup", "release"])
def test_terminal_one_shot_hard_exit_runs_when_finalization_raises(
    monkeypatch, failure_site,
):
    import cli as cli_mod
    from tools.process_registry import process_registry

    calls = []

    def _fail(site):
        def _raise(*_args, **_kwargs):
            calls.append(site)
            raise RuntimeError(site)

        return _raise

    class _CLI:
        def _release_active_session(self):
            if failure_site == "release":
                _fail("release")()
            calls.append("release")

    monkeypatch.setattr(cli_mod, "_kanban_worker_run_is_terminal", lambda: True)
    monkeypatch.setattr(process_registry, "kill_all", lambda **_kw: None)
    monkeypatch.setattr(cli_mod, "_flush_one_shot_session_store", lambda _cli: None)
    monkeypatch.setattr(
        cli_mod,
        "_notify_single_query_session_finalize",
        _fail("notify") if failure_site == "notify" else lambda _cli: None,
    )
    monkeypatch.setattr(
        cli_mod,
        "_run_cleanup",
        _fail("cleanup") if failure_site == "cleanup" else lambda **_kw: None,
    )
    monkeypatch.setattr(
        cli_mod,
        "_shutdown_terminal_kanban_worker",
        lambda: calls.append("hard_exit"),
    )

    with pytest.raises(RuntimeError, match=failure_site):
        cli_mod._finalize_single_query(_CLI())
    assert calls[-1] == "hard_exit"
    assert "release" in calls or failure_site == "release"


def _process_gone_or_zombie(pid: int) -> bool:
    try:
        import psutil

        proc = psutil.Process(pid)
        return not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


@pytest.mark.linux_only
def test_process_level_terminal_cleanup_escalates_kills_tree_and_reaps(
    kanban_home, tmp_path,
):
    """Real SIGTERM-ignoring worker + detached child are gone before release."""
    child_pid_file = tmp_path / "child.pid"
    child_code = (
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(120)"
    )
    worker_code = (
        "import pathlib,signal,subprocess,sys,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "p=subprocess.Popen([sys.executable,'-c',sys.argv[2]], start_new_session=True); "
        "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); "
        "time.sleep(120)"
    )
    worker = subprocess.Popen(
        [sys.executable, "-c", worker_code, str(child_pid_file), child_code],
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert child_pid_file.exists()
        child_pid = int(child_pid_file.read_text())

        with kb.connect() as conn:
            task_id = kb.create_task(
                conn, title="real terminal cleanup", assignee="default"
            )
            task = kb.claim_task(conn, task_id, claimer=kb._claimer_id())
            assert task is not None
            run_id = task.current_run_id
            kb._set_worker_pid(conn, task_id, worker.pid)
            assert kb.complete_task(
                conn, task_id, summary="done", expected_run_id=run_id
            )

            assert kb.cleanup_terminal_workers(
                conn, grace_seconds=0.15
            ) == [task_id]
            payload = conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? "
                "AND kind = 'worker_cleanup' ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            assert payload is not None
            cleanup = json.loads(payload["payload"])
            assert cleanup["identity_verified"] is True
            assert cleanup["termination_attempted"] is True
            assert cleanup["sigkill"] is True
            assert cleanup["reaped"] is True

        worker.wait(timeout=3)
        deadline = time.monotonic() + 3
        while not _process_gone_or_zombie(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _process_gone_or_zombie(child_pid)
        assert worker.poll() is not None
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=3)


@pytest.mark.linux_only
@pytest.mark.parametrize("max_retries,expected_status", [(None, "ready"), (1, "blocked")])
def test_process_level_timeout_and_gave_up_reap_worker_tree(
    kanban_home, tmp_path, max_retries, expected_status,
):
    child_pid_file = tmp_path / f"timeout-child-{max_retries}.pid"
    child_code = (
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(120)"
    )
    worker_code = (
        "import pathlib,signal,subprocess,sys,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "p=subprocess.Popen([sys.executable,'-c',sys.argv[2]], start_new_session=True); "
        "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); "
        "time.sleep(120)"
    )
    worker = subprocess.Popen(
        [sys.executable, "-c", worker_code, str(child_pid_file), child_code],
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert child_pid_file.exists()
        child_pid = int(child_pid_file.read_text())

        with kb.connect() as conn:
            task_id = kb.create_task(
                conn,
                title="timeout cleanup",
                assignee="default",
                max_runtime_seconds=1,
                max_retries=max_retries,
            )
            task = kb.claim_task(conn, task_id, claimer=kb._claimer_id())
            assert task is not None
            kb._set_worker_pid(conn, task_id, worker.pid)
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE task_runs SET started_at = ? WHERE id = ?",
                    (int(time.time()) - 20, task.current_run_id),
                )

            assert kb.enforce_max_runtime(
                conn, grace_seconds=0.15
            ) == [task_id]
            updated = kb.get_task(conn, task_id)
            assert updated is not None
            assert updated.status == expected_status
            assert updated.claim_lock is None
            assert updated.worker_pid is None
            events = [event.kind for event in kb.list_events(conn, task_id)]
            assert "timed_out" in events
            if max_retries == 1:
                assert "gave_up" in events

        worker.wait(timeout=3)
        deadline = time.monotonic() + 3
        while not _process_gone_or_zombie(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _process_gone_or_zombie(child_pid)
        assert worker.poll() is not None
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=3)


@pytest.mark.linux_only
def test_terminal_worker_hard_exit_does_not_wait_for_threads_or_children(tmp_path):
    """The worker-side finalizer drains an untracked child then bypasses joins."""
    child_pid_file = tmp_path / "shutdown-child.pid"
    script = (
        "import pathlib,signal,subprocess,sys,threading,time; "
        "from cli import _shutdown_terminal_kanban_worker; "
        "threading.Thread(target=lambda: time.sleep(120), daemon=False).start(); "
        "p=subprocess.Popen([sys.executable,'-c',"
        "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)'],"
        "start_new_session=True); "
        "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); "
        "_shutdown_terminal_kanban_worker()"
    )
    worker = subprocess.Popen([sys.executable, "-c", script, str(child_pid_file)])
    worker.wait(timeout=10)
    assert worker.returncode == 0
    child_pid = int(child_pid_file.read_text())
    deadline = time.monotonic() + 3
    while not _process_gone_or_zombie(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _process_gone_or_zombie(child_pid)


@pytest.mark.windows_only
def test_windows_terminal_cleanup_uses_identity_bound_tree_kill(kanban_home, tmp_path):
    child_pid_file = tmp_path / "windows-child.pid"
    worker_code = (
        "import pathlib,subprocess,sys,time; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(120)"
    )
    worker = subprocess.Popen(
        [sys.executable, "-c", worker_code, str(child_pid_file)]
    )
    try:
        deadline = time.monotonic() + 5
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert child_pid_file.exists()
        child_pid = int(child_pid_file.read_text())
        with kb.connect() as conn:
            task_id = kb.create_task(
                conn, title="windows terminal cleanup", assignee="default"
            )
            task = kb.claim_task(conn, task_id, claimer=kb._claimer_id())
            assert task is not None
            kb._set_worker_pid(conn, task_id, worker.pid)
            assert kb.complete_task(
                conn,
                task_id,
                summary="done",
                expected_run_id=task.current_run_id,
            )
            assert kb.cleanup_terminal_workers(
                conn, grace_seconds=3.0
            ) == [task_id]
        worker.wait(timeout=5)
        assert _process_gone_or_zombie(child_pid)
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=3)
