"""Regression tests: the goal loop must actually reach its judge.

Field evidence (2026-07): a board with 97 ``goal_mode=1`` cards logged 8
judge calls and ZERO goal-loop iterations over seven weeks. The loop is
invoked from cli.py AFTER the worker's first turn has already finished, and
its first action is a status check that returns on ``done``/``blocked``.
Workers are instructed to call ``kanban_complete`` / ``kanban_block`` during
that first turn, so the status is already terminal and ``judge_goal`` is
never consulted: 92 of 97 cards had a run ending completed/blocked.

The judge itself was verified healthy (client resolves, 5.1s round trip,
correct verdict), so this is purely a reachability defect.

These tests encode the intended contract:

1. A worker that self-completes in turn 1 is still judged once.
2. A ``continue`` verdict reopens the task rather than accepting the
   worker's own completion.
3. A ``done`` verdict leaves the worker's completion alone.
4. Human-gate blocks (``needs_input``) are NEVER second-guessed - the
   worker is waiting on a person, and re-prompting it would burn turns
   arguing with a wall.
"""

from __future__ import annotations

import pytest

from hermes_cli import goals


def _patch_judge(monkeypatch, verdicts):
    seq = list(verdicts)
    calls = []

    def _fake_judge(goal, response, subgoals=None, background_processes=None, **_kw):
        v = seq.pop(0) if seq else "done"
        calls.append((goal, response, v))
        return v, f"scripted:{v}", False, None, False

    monkeypatch.setattr(goals, "judge_goal", _fake_judge)
    return calls


def test_self_completed_worker_is_still_judged(monkeypatch):
    """Turn-1 kanban_complete must not bypass the judge."""
    calls = _patch_judge(monkeypatch, ["done"])

    goals.run_kanban_goal_loop(
        task_id="t1",
        goal_text="confirm root cause with two independent sources",
        run_turn=lambda p: "x",
        task_status_fn=lambda: "done",
        block_fn=lambda r: pytest.fail("should not block on an accepted completion"),
        first_response="Root cause found. Completing.",
    )

    assert len(calls) == 1, (
        "worker self-completed in turn 1 and the judge was never consulted "
        "- this is the reachability bug"
    )


def test_judge_continue_reopens_a_self_completed_task(monkeypatch):
    """A premature self-completion is reopened and the worker keeps working."""
    _patch_judge(monkeypatch, ["continue", "done"])
    statuses = iter(["done", "running", "done"])
    reopened = []
    turns = []

    res = goals.run_kanban_goal_loop(
        task_id="t2",
        goal_text="ship it",
        run_turn=lambda p: turns.append(p) or "kept going",
        task_status_fn=lambda: next(statuses),
        block_fn=lambda r: pytest.fail("should not block"),
        reopen_fn=lambda reason: reopened.append(reason),
        max_turns=10,
        first_response="Probably fine. Completing.",
    )

    assert reopened, "judge said continue - the task must be reopened"
    assert turns, "worker must be re-prompted after a continue verdict"
    assert "not done yet" in turns[0]
    assert res["outcome"] in ("completed_by_worker", "stopped")


def test_judge_done_accepts_self_completion(monkeypatch):
    """A legitimate completion is judged once and left alone."""
    calls = _patch_judge(monkeypatch, ["done"])
    reopened = []

    res = goals.run_kanban_goal_loop(
        task_id="t3",
        goal_text="task",
        run_turn=lambda p: pytest.fail("no extra turn for an accepted completion"),
        task_status_fn=lambda: "done",
        block_fn=lambda r: pytest.fail("should not block"),
        reopen_fn=lambda reason: reopened.append(reason),
        first_response="Full evidence attached. Completing.",
    )

    assert len(calls) == 1
    assert reopened == []
    assert res["outcome"] == "completed_by_worker"


def test_human_gate_block_is_never_judged(monkeypatch):
    """needs_input blocks wait on a person - never re-prompt or reopen them."""
    calls = _patch_judge(monkeypatch, ["continue"])
    reopened = []

    res = goals.run_kanban_goal_loop(
        task_id="t4",
        goal_text="task",
        run_turn=lambda p: pytest.fail("must not re-prompt a human-gated worker"),
        task_status_fn=lambda: "blocked",
        block_kind_fn=lambda: "needs_input",
        block_fn=lambda r: None,
        reopen_fn=lambda reason: reopened.append(reason),
        first_response="Drafted the message. Blocking for approval.",
    )

    assert calls == [], "a human-gate block must not be sent to the judge"
    assert reopened == []
    assert res["outcome"] == "blocked_by_worker"


def test_non_human_block_is_judged(monkeypatch):
    """A self-declared block with no human gate still gets judged."""
    calls = _patch_judge(monkeypatch, ["continue", "done"])
    statuses = iter(["blocked", "running", "done"])
    turns = []
    reopened = []

    goals.run_kanban_goal_loop(
        task_id="t5",
        goal_text="task",
        run_turn=lambda p: turns.append(p) or "resumed",
        task_status_fn=lambda: next(statuses),
        block_kind_fn=lambda: None,
        block_fn=lambda r: None,
        reopen_fn=lambda reason: reopened.append(reason),
        max_turns=10,
        first_response="Giving up, blocking.",
    )

    assert len(calls) >= 1, "a non-human block must still be judged"
    assert turns, "judge said continue - worker must be re-prompted"


def test_dependency_block_is_never_judged(monkeypatch):
    """dependency blocks wait on another task - same exemption as needs_input."""
    calls = _patch_judge(monkeypatch, ["continue"])

    res = goals.run_kanban_goal_loop(
        task_id="t7",
        goal_text="task",
        run_turn=lambda p: pytest.fail("must not re-prompt a dependency-gated worker"),
        task_status_fn=lambda: "blocked",
        block_kind_fn=lambda: "dependency",
        block_fn=lambda r: None,
        reopen_fn=lambda reason: pytest.fail("must not reopen a dependency gate"),
        first_response="Waiting on parent task.",
    )

    assert calls == []
    assert res["outcome"] == "blocked_by_worker"


def test_unknown_block_kind_fails_safe(monkeypatch):
    """A block_kind lookup that throws is treated as a human gate."""
    calls = _patch_judge(monkeypatch, ["continue"])

    def _boom():
        raise RuntimeError("db unavailable")

    res = goals.run_kanban_goal_loop(
        task_id="t6",
        goal_text="task",
        run_turn=lambda p: pytest.fail("must not re-prompt when block kind is unknown"),
        task_status_fn=lambda: "blocked",
        block_kind_fn=_boom,
        block_fn=lambda r: None,
        reopen_fn=lambda reason: pytest.fail("must not reopen when block kind is unknown"),
        first_response="Blocked.",
    )

    assert calls == []
    assert res["outcome"] == "blocked_by_worker"


class TestReopenKeepsTaskWatchdogVisible:
    """reopen_task must leave the card recoverable if the worker then dies.

    ``complete_task`` / ``block_task`` NULL out claim_lock, worker_pid and
    claim_expires. Both watchdogs are scoped to those columns
    (``reap_crashed_workers``: running AND worker_pid IS NOT NULL;
    ``release_stale_claims``: running AND claim_expires IS NOT NULL), so a
    reopen that left them NULL would strand the task in ``running`` with no
    recovery path.
    """

    def test_reopen_restores_claim_identity(self, tmp_path, monkeypatch):
        from pathlib import Path
        from hermes_cli import kanban_db as kb

        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        kb.init_db()

        with kb.connect() as conn:
            tid = kb.create_task(conn, title="goal card", assignee="w", goal_mode=True)
            kb.complete_task(conn, tid, result="premature", summary="s")
            assert kb.get_task(conn, tid).status == "done"

            run_id = kb.reopen_task(
                conn, tid, reason="judge rejected",
                claim_lock="host:123", worker_pid=4242, claim_ttl_seconds=900,
            )
            assert run_id is not None

            row = conn.execute(
                "SELECT status, worker_pid, claim_lock, claim_expires, completed_at, "
                "current_run_id FROM tasks WHERE id = ?", (tid,),
            ).fetchone()

        assert row["status"] == "running"
        assert row["completed_at"] is None
        assert row["worker_pid"] == 4242, "reap_crashed_workers would skip a NULL pid"
        assert row["claim_lock"] == "host:123"
        assert row["claim_expires"], "release_stale_claims would skip a NULL claim_expires"
        assert row["current_run_id"] == run_id, (
            "a reopened task with no live run cannot terminally complete: "
            "complete_task matches the worker's pinned run id against current_run_id"
        )

    def test_full_complete_reopen_complete_lifecycle(self, tmp_path, monkeypatch):
        """The guarded worker path must still work after a reopen.

        kanban_complete passes the worker's pinned HERMES_KANBAN_RUN_ID and
        complete_task requires it to equal tasks.current_run_id. If a reopen
        leaves that pointer stale or NULL, the continuation can never
        terminally complete or block through the guarded paths - it just
        fails silently and the card hangs in running.
        """
        from pathlib import Path
        from hermes_cli import kanban_db as kb

        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        kb.init_db()

        with kb.connect() as conn:
            tid = kb.create_task(conn, title="goal card", assignee="w", goal_mode=True)

            # Claim it like the dispatcher does, so a real run row exists.
            claimed = kb.claim_task(conn, tid, claimer="host:123")
            assert claimed is not None
            first_run = kb.get_task(conn, tid).current_run_id
            assert first_run is not None, "claim must open a run"

            # First attempt: worker completes with its own run id pinned.
            assert kb.complete_task(
                conn, tid, result="premature", summary="s",
                expected_run_id=first_run,
            ) is True

            # Judge rejects it -> reopen onto a NEW run.
            second_run = kb.reopen_task(
                conn, tid, reason="judge rejected",
                claim_lock="host:123", worker_pid=4242,
            )
            assert second_run is not None
            assert second_run != first_run, "reopen must not revive the closed run"

            # The stale run id must NOT be able to complete the task.
            assert kb.complete_task(
                conn, tid, result="stale", summary="s",
                expected_run_id=first_run,
            ) is False
            assert kb.get_task(conn, tid).status == "running"

            # The rebound run id must succeed.
            assert kb.complete_task(
                conn, tid, result="real work", summary="done properly",
                expected_run_id=second_run,
            ) is True
            assert kb.get_task(conn, tid).status == "done"

            runs = conn.execute(
                "SELECT id, status, outcome FROM task_runs WHERE task_id = ? ORDER BY id",
                (tid,),
            ).fetchall()

        assert len(runs) == 2, "each attempt keeps its own run row"
        assert all(r["status"] != "running" for r in runs), "both runs closed"

    def test_reopen_allows_block_after_reopen(self, tmp_path, monkeypatch):
        """A reopened continuation must also be able to block for a human."""
        from pathlib import Path
        from hermes_cli import kanban_db as kb

        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        kb.init_db()

        with kb.connect() as conn:
            tid = kb.create_task(conn, title="goal card", assignee="w", goal_mode=True)
            kb.complete_task(conn, tid, result="premature", summary="s")
            new_run = kb.reopen_task(conn, tid, reason="judge rejected", worker_pid=4242)
            assert new_run is not None

            kb.block_task(conn, tid, reason="needs a human", kind="needs_input")
            task = kb.get_task(conn, tid)

        assert task.status == "blocked"
        assert task.block_kind == "needs_input"

    def test_reopen_heartbeat_uses_the_restored_lock(self, tmp_path, monkeypatch):
        """heartbeat_claim matches on claim_lock - the drain must pass it."""
        from pathlib import Path
        from hermes_cli import kanban_db as kb

        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        kb.init_db()

        with kb.connect() as conn:
            tid = kb.create_task(conn, title="goal card", assignee="w", goal_mode=True)
            kb.complete_task(conn, tid, result="x", summary="s")
            kb.reopen_task(conn, tid, reason="r", claim_lock="host:999", worker_pid=1)

            assert kb.heartbeat_claim(conn, tid, claimer="host:999") is True
            assert kb.heartbeat_claim(conn, tid, claimer="wrong:lock") is False

    def test_reopen_rejects_non_terminal_task(self, tmp_path, monkeypatch):
        from pathlib import Path
        from hermes_cli import kanban_db as kb

        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        kb.init_db()

        with kb.connect() as conn:
            tid = kb.create_task(conn, title="open card", assignee="w")
            assert kb.reopen_task(conn, tid, reason="x") is None
