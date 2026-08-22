"""Resume-on-retry for re-dispatched kanban workers (card t_e1ba67d5).

A task that bounces back from review via ``request_changes`` re-enters
``ready`` and gets reclaimed by the SAME implementer profile. Before this
fix, every round spawned a fully cold ``hermes chat -q`` session. This
suite covers ``_resolve_worker_resume_session_id`` directly and the
``--resume``/``--no-restore-cwd`` wiring in ``_default_spawn``.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

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


def _make_task(kb, *, assignee: str, task_id: str = "t_resume", run_id: int = 1):
    return kb.Task(
        id=task_id,
        title="resume test",
        body=None,
        assignee=assignee,
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        current_run_id=run_id,
    )


def _make_state_db_with_session(path: Path, session_id: str) -> None:
    """Minimal state.db with one row in a `sessions` table shaped like
    the real SessionDB.get_session() expects (id + a couple of columns)."""
    from hermes_state import SessionDB

    sdb = SessionDB(db_path=path)
    try:
        sdb.create_session(session_id, "cli")
    finally:
        sdb.close()


class TestResolveWorkerResumeSessionId:
    def test_no_prior_run_returns_none(self, kanban_home):
        conn = kb.connect()
        try:
            tid = kb.create_task(conn, title="t", assignee="worker")
            assert kb._resolve_worker_resume_session_id(
                conn, tid, "worker", str(kanban_home)
            ) is None
        finally:
            conn.close()

    def test_prior_ended_run_with_session_metadata_resolves(
        self, kanban_home, tmp_path
    ):
        state_db_path = kanban_home / "state.db"
        session_id = "20260821_120000_deadbeef"
        _make_state_db_with_session(state_db_path, session_id)

        conn = kb.connect()
        try:
            tid = kb.create_task(conn, title="t", assignee="worker")
            kb.claim_task(conn, tid)
            kb._end_run(
                conn, tid,
                outcome="review_requested",
                status="review",
                metadata={"worker_session_id": session_id},
            )
            resolved = kb._resolve_worker_resume_session_id(
                conn, tid, "worker", str(kanban_home)
            )
            assert resolved == session_id
        finally:
            conn.close()

    def test_session_missing_from_state_db_returns_none(self, kanban_home):
        # state.db exists but has no matching session row.
        state_db_path = kanban_home / "state.db"
        _make_state_db_with_session(state_db_path, "some-other-session")

        conn = kb.connect()
        try:
            tid = kb.create_task(conn, title="t", assignee="worker")
            kb.claim_task(conn, tid)
            kb._end_run(
                conn, tid,
                outcome="review_requested",
                status="review",
                metadata={"worker_session_id": "vanished-session"},
            )
            resolved = kb._resolve_worker_resume_session_id(
                conn, tid, "worker", str(kanban_home)
            )
            assert resolved is None
        finally:
            conn.close()

    def test_different_profile_run_is_not_a_candidate(self, kanban_home):
        """A reviewer's run must never be handed back as the implementer's
        resume target — profile filter in the SQL is load-bearing."""
        state_db_path = kanban_home / "state.db"
        session_id = "20260821_120000_reviewer"
        _make_state_db_with_session(state_db_path, session_id)

        conn = kb.connect()
        try:
            tid = kb.create_task(conn, title="t", assignee="worker")
            kb.claim_task(conn, tid)
            kb._end_run(
                conn, tid,
                outcome="review_requested",
                status="review",
                metadata={"worker_session_id": session_id},
            )
            # Ask for a DIFFERENT profile's resume candidate.
            resolved = kb._resolve_worker_resume_session_id(
                conn, tid, "reviewer", str(kanban_home)
            )
            assert resolved is None
        finally:
            conn.close()

    def test_no_profile_home_returns_none(self, kanban_home):
        conn = kb.connect()
        try:
            tid = kb.create_task(conn, title="t", assignee="worker")
            assert kb._resolve_worker_resume_session_id(
                conn, tid, "worker", None
            ) is None
        finally:
            conn.close()

    def test_missing_state_db_returns_none(self, kanban_home, tmp_path):
        conn = kb.connect()
        try:
            tid = kb.create_task(conn, title="t", assignee="worker")
            kb.claim_task(conn, tid)
            kb._end_run(
                conn, tid,
                outcome="review_requested",
                status="review",
                metadata={"worker_session_id": "whatever"},
            )
            other_home = tmp_path / "no-state-db-here"
            other_home.mkdir()
            resolved = kb._resolve_worker_resume_session_id(
                conn, tid, "worker", str(other_home)
            )
            assert resolved is None
        finally:
            conn.close()


class TestDefaultSpawnResumeWiring:
    def test_spawn_adds_resume_flag_when_conn_resolves_a_session(
        self, kanban_home, monkeypatch, tmp_path
    ):
        profile_dir = kanban_home / "profiles" / "worker"
        profile_dir.mkdir(parents=True)
        state_db_path = profile_dir / "state.db"
        session_id = "20260821_120000_warmsession"
        _make_state_db_with_session(state_db_path, session_id)

        monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
        captured = {}

        class FakeProc:
            pid = 5150

        def fake_popen(cmd, *args, **kwargs):
            captured["cmd"] = list(cmd)
            return FakeProc()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        conn = kb.connect()
        try:
            tid = kb.create_task(conn, title="t", assignee="worker")
            kb.claim_task(conn, tid)
            kb._end_run(
                conn, tid,
                outcome="review_requested",
                status="review",
                metadata={"worker_session_id": session_id},
            )
            task = _make_task(kb, assignee="worker", task_id=tid)
            workspace = tmp_path / "workspace"
            workspace.mkdir()
            pid = kb._default_spawn(task, str(workspace), conn=conn)
        finally:
            conn.close()

        assert pid == 5150
        assert "--resume" in captured["cmd"]
        idx = captured["cmd"].index("--resume")
        assert captured["cmd"][idx + 1] == session_id
        assert "--no-restore-cwd" in captured["cmd"]

    def test_spawn_stays_cold_without_conn(self, kanban_home, monkeypatch, tmp_path):
        """spawn_fn stubs / callers that omit `conn` keep today's cold start —
        resume resolution must never be attempted implicitly."""
        profile_dir = kanban_home / "profiles" / "worker"
        profile_dir.mkdir(parents=True)
        monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
        captured = {}

        class FakeProc:
            pid = 5151

        def fake_popen(cmd, *args, **kwargs):
            captured["cmd"] = list(cmd)
            return FakeProc()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        task = _make_task(kb, assignee="worker")
        workspace = tmp_path / "workspace2"
        workspace.mkdir()
        pid = kb._default_spawn(task, str(workspace))

        assert pid == 5151
        assert "--resume" not in captured["cmd"]

    def test_spawn_stays_cold_when_no_resumable_session(
        self, kanban_home, monkeypatch, tmp_path
    ):
        profile_dir = kanban_home / "profiles" / "worker"
        profile_dir.mkdir(parents=True)
        monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
        captured = {}

        class FakeProc:
            pid = 5152

        def fake_popen(cmd, *args, **kwargs):
            captured["cmd"] = list(cmd)
            return FakeProc()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)

        conn = kb.connect()
        try:
            tid = kb.create_task(conn, title="t", assignee="worker")
            task = _make_task(kb, assignee="worker", task_id=tid)
            workspace = tmp_path / "workspace3"
            workspace.mkdir()
            pid = kb._default_spawn(task, str(workspace), conn=conn)
        finally:
            conn.close()

        assert pid == 5152
        assert "--resume" not in captured["cmd"]


class TestDispatchOnceResumeAcrossReviewRounds:
    """End-to-end: dispatch_once through two ready->review->changes_requested
    rounds for the same task/profile. Verifies the SAME worker_session_id is
    reused and handed to round 2's spawn as --resume, per the card's
    acceptance criterion ('re-review a real task through 2 rounds, confirm
    session_id stays the SAME across rounds')."""

    def test_second_round_resumes_first_rounds_session(
        self, kanban_home, monkeypatch, tmp_path
    ):
        profile_dir = kanban_home / "profiles" / "worker"
        profile_dir.mkdir(parents=True)
        (kanban_home / "profiles" / "reviewer").mkdir(parents=True)

        round1_session = "20260821_100000_round1session"
        _make_state_db_with_session(profile_dir / "state.db", round1_session)

        from hermes_cli import profiles as profiles_mod
        monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: True)
        monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])

        spawns = []

        def fake_spawn(task, workspace, *, board=None, conn=None):
            resume_id = None
            if conn is not None:
                resume_id = kb._resolve_worker_resume_session_id(
                    conn, task.id, kb._canonical_assignee(task.assignee),
                    str(profile_dir) if task.assignee == "worker" else None,
                )
            spawns.append({"assignee": task.assignee, "resume": resume_id})
            return 9000 + len(spawns)

        conn = kb.connect()
        try:
            tid = kb.create_task(conn, title="resume-e2e", assignee="worker")

            # --- Round 1: implementer picks up the ready task cold ---
            result = kb.dispatch_once(conn, spawn_fn=fake_spawn)
            assert len(spawns) == 1
            assert spawns[0]["assignee"] == "worker"
            assert spawns[0]["resume"] is None  # no prior run yet

            # Worker finishes round 1 and hands off to review, stamping its
            # own HERMES_SESSION_ID onto the run metadata exactly like
            # kanban_request_review's _stamp_worker_session_metadata does.
            ok = kb.request_review(
                conn, tid,
                summary="implemented X",
                metadata={"worker_session_id": round1_session},
                reviewer="reviewer",
                expected_run_id=kb.get_task(conn, tid).current_run_id,
            )
            assert ok

            # --- Reviewer round: claim from review, then request changes ---
            result = kb.dispatch_once(conn, spawn_fn=fake_spawn)
            assert len(spawns) == 2
            assert spawns[1]["assignee"] == "reviewer"

            reviewer_run_id = kb.get_task(conn, tid).current_run_id
            ok, implementer = kb.request_changes(
                conn, tid,
                reason="needs another pass",
                expected_run_id=reviewer_run_id,
            )
            assert ok
            assert implementer == "worker"

            # --- Round 2: implementer re-claims 'ready' — must resume ---
            result = kb.dispatch_once(conn, spawn_fn=fake_spawn)
            assert len(spawns) == 3
            assert spawns[2]["assignee"] == "worker"
            assert spawns[2]["resume"] == round1_session, (
                "round 2 must resume round 1's stamped session, not cold-start"
            )
        finally:
            conn.close()

