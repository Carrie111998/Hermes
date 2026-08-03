"""Tests for the Kanban DB layer (hermes_cli.kanban_db)."""

from __future__ import annotations

import concurrent.futures
import os
import sqlite3
import subprocess
import sys
import time
import types
import unittest.mock
from pathlib import Path

import pytest

import hermes_state
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _init_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "kanban@example.com"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Kanban Test"], check=True, capture_output=True, text=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True, text=True)


# ---------------------------------------------------------------------------
# Schema / init
# ---------------------------------------------------------------------------







@pytest.mark.windows_only
def test_cross_process_init_lock_uses_windows_byte_range_lock(tmp_path, monkeypatch):
    """Windows must use a real (non-blocking) process lock, not a no-op open.

    The init lock acquires with LK_NBLCK in a bounded retry loop (#36644) so a
    wedged holder can never block connect() forever; a clean acquire takes the
    lock once and releases it once.

    ``windows_only``: ``msvcrt`` does not exist off Windows, so faking
    ``_IS_WINDOWS`` on Linux meant injecting a fake ``msvcrt`` module too —
    the test then asserted against its own stub rather than the byte-range
    locking API. Here the platform is real; only ``msvcrt.locking`` is
    instrumented so the call sequence is observable.
    """
    calls: list[tuple[int, int, int]] = []
    import msvcrt as _msvcrt

    fake_msvcrt = types.SimpleNamespace(
        LK_NBLCK=_msvcrt.LK_NBLCK,
        LK_UNLCK=_msvcrt.LK_UNLCK,
        locking=lambda fd, mode, nbytes: calls.append((fd, mode, nbytes)),
    )
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)

    db_path = tmp_path / "kanban.db"
    with kb._cross_process_init_lock(db_path):
        # Acquired exactly once via the non-blocking byte-range lock.
        assert [call[1:] for call in calls] == [(fake_msvcrt.LK_NBLCK, 1)]

    # Released once on exit.
    assert [call[1:] for call in calls] == [
        (fake_msvcrt.LK_NBLCK, 1),
        (fake_msvcrt.LK_UNLCK, 1),
    ]


def test_connect_migrates_legacy_db_before_optional_column_indexes(tmp_path):
    """Legacy DBs missing additive indexed columns must migrate cleanly.

    SCHEMA_SQL runs in ``connect()`` before ``_migrate_add_optional_columns``.
    Indexes over additive columns therefore must be created after the
    migration adds those columns, or boards predating the column fail to
    open before migration can run.

    Covers all four indexes that sit on additive columns:
    - ``tasks.session_id``       -> ``idx_tasks_session_id``    (#28447)
    - ``tasks.tenant``           -> ``idx_tasks_tenant``        (#16081)
    - ``tasks.idempotency_key``  -> ``idx_tasks_idempotency``   (#17805)
    - ``task_events.run_id``     -> ``idx_events_run``          (#17805)
    """
    db_path = tmp_path / "legacy-kanban.db"
    conn = sqlite3.connect(str(db_path))
    # Pre-#16081 ``tasks`` shape: missing tenant, idempotency_key, session_id.
    conn.execute("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT,
            assignee TEXT,
            status TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0,
            created_by TEXT,
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            workspace_kind TEXT NOT NULL DEFAULT 'scratch',
            workspace_path TEXT,
            claim_lock TEXT,
            claim_expires INTEGER
        )
    """)
    # Pre-#17805 ``task_events`` shape: missing run_id. Required because
    # ``_migrate_add_optional_columns`` unconditionally runs PRAGMA on
    # ``task_events`` for run_id back-fill.
    conn.execute("""
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        )
    """)
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) "
        "VALUES ('legacy', 'old board task', 'ready', 1)"
    )
    conn.commit()
    conn.close()

    with kb.connect(db_path) as migrated:
        task_columns = {
            row["name"] for row in migrated.execute("PRAGMA table_info(tasks)")
        }
        event_columns = {
            row["name"]
            for row in migrated.execute("PRAGMA table_info(task_events)")
        }
        indexes = {
            row["name"]
            for row in migrated.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }

    # Additive columns added by migration:
    assert "session_id" in task_columns
    assert "tenant" in task_columns
    assert "idempotency_key" in task_columns
    assert "run_id" in event_columns
    # And their indexes — the regression scope of this test:
    assert "idx_tasks_session_id" in indexes
    assert "idx_tasks_tenant" in indexes
    assert "idx_tasks_idempotency" in indexes
    assert "idx_events_run" in indexes


# ---------------------------------------------------------------------------
# Task creation + status inference
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Links + dependency resolution
# ---------------------------------------------------------------------------







# ---------------------------------------------------------------------------
# Atomic claim (CAS)
# ---------------------------------------------------------------------------



def test_schedule_task_parks_time_delay_without_dispatching(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="delayed recheck", assignee="ops")
        assert kb.schedule_task(conn, t, reason="run next week") is True
        task = kb.get_task(conn, t)
        assert task.status == "scheduled"
        assert kb.claim_task(conn, t) is None

        events = kb.list_events(conn, t)
        assert any(e.kind == "scheduled" and e.payload == {"reason": "run next week"} for e in events)








def test_stale_claim_reclaim_event_records_diagnostic_payload(
    kanban_home, monkeypatch,
):
    """``reclaimed`` events should carry claim_expires, last_heartbeat_at,
    and worker_pid so operators can diagnose why a claim went stale
    (#23025: previous payload only had ``stale_lock`` which gives no
    timing context)."""
    import json
    import hermes_cli.kanban_db as _kb

    with kb.connect() as conn:
        t = kb.create_task(conn, title="x", assignee="a")
        host = _kb._claimer_id().split(":", 1)[0]
        kb.claim_task(conn, t, claimer=f"{host}:worker")
        kb._set_worker_pid(conn, t, 12345)
        old_expires = int(time.time()) - 3600
        hb_at = int(time.time()) - 1800
        conn.execute(
            "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = ? "
            "WHERE id = ?",
            (old_expires, hb_at, t),
        )

        monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
        kb.release_stale_claims(conn, signal_fn=lambda _p, _s: None)
        row = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'reclaimed'",
            (t,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload"])
        assert payload["claim_expires"] == old_expires
        assert payload["last_heartbeat_at"] == hb_at
        assert payload["worker_pid"] == 12345
        assert payload["host_local"] is True






# ---------------------------------------------------------------------------
# Rate-limit requeue: a worker that bails on a provider quota wall must be
# released back to ``ready`` WITHOUT counting a failure, so a long (e.g.
# 5-hour) quota window can't trip the circuit breaker and permanently block
# the card. The respawn guard then defers it on a cooldown until quota
# returns. Regression coverage for the kanban-rate-limit-failure report.
# ---------------------------------------------------------------------------


def _exited_status(code: int) -> int:
    """Raw wait-status for a WIFEXITED child with the given exit code."""
    return code << 8




def test_rate_limit_exit_requeues_without_counting_failure(
    kanban_home, monkeypatch,
):
    """A rate-limit sentinel exit releases the task to ``ready`` and leaves
    ``consecutive_failures`` untouched — the breaker must never trip on a
    transient throttle, even across many quota-wall hits."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kb.connect() as conn:
        host = _kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="rl", assignee="a")

        # Simulate FAR more quota-wall hits than DEFAULT_FAILURE_LIMIT (2).
        # If any of these counted as a failure the task would be blocked.
        for i in range(6):
            pid = 70000 + i
            # Claim to open a real run (so detect_crashed_workers can close
            # it with a rate_limited outcome), then point the claim at this
            # host + a dead pid so the crash path acts on it.
            kb.claim_task(conn, tid, claimer=f"{host}:w{i}")
            conn.execute(
                "UPDATE tasks SET worker_pid=?, consecutive_failures=? "
                "WHERE id=?",
                (pid, 0, tid),
            )
            conn.commit()
            _kb._record_worker_exit(
                pid, _exited_status(_kb.KANBAN_RATE_LIMIT_EXIT_CODE)
            )

            crashed = kb.detect_crashed_workers(conn)
            # Rate-limited requeues are NOT crashes.
            assert tid not in crashed
            rl = getattr(_kb.detect_crashed_workers, "_last_rate_limited", [])
            assert tid in rl

            task = kb.get_task(conn, tid)
            assert task.status == "ready", (
                f"hit {i}: should requeue ready, got {task.status}"
            )
            assert task.consecutive_failures == 0, (
                f"hit {i}: rate-limit must not count a failure, "
                f"got {task.consecutive_failures}"
            )

        # Last failure error stamped so the respawn guard recognizes the
        # quota wall.
        assert task.last_failure_error and "rate-limited" in task.last_failure_error

        # A ``rate_limited`` run outcome was recorded (not ``crashed``).
        outcomes = [
            r["outcome"] for r in conn.execute(
                "SELECT outcome FROM task_runs WHERE task_id=?", (tid,),
            ).fetchall()
        ]
        assert "rate_limited" in outcomes
        assert "crashed" not in outcomes




def test_respawn_guard_defers_rate_limited_within_cooldown(
    kanban_home, monkeypatch,
):
    """Within the cooldown after a rate-limit requeue, the guard defers the
    respawn; after the cooldown it allows a probe — and crucially does NOT
    fall into ``blocker_auth`` (which would defer forever)."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")
    now = 5_000_000

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="rl-guard", assignee="a")
        # Seed a rate_limited run that just ended + the stamped error.
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='rate_limited', status='rate_limited', "
            "ended_at=? WHERE id=?",
            (now, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, "
            "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL, "
            "last_failure_error=? WHERE id=?",
            ("pid 1 exited rate-limited (quota wall) — requeued", tid),
        )
        conn.commit()

        # Inside cooldown → defer with the rate-limit-specific reason.
        monkeypatch.setattr(_kb.time, "time", lambda: now + 100)
        assert kb.check_respawn_guard(conn, tid) == "rate_limit_cooldown"

        # Past cooldown → allowed (None), NOT trapped by blocker_auth even
        # though last_failure_error contains "rate-limited".
        monkeypatch.setattr(_kb.time, "time", lambda: now + 400)
        assert kb.check_respawn_guard(conn, tid) is None








# ---------------------------------------------------------------------------
# Complete / block / unblock / archive / assign
# ---------------------------------------------------------------------------







def test_recompute_ready_honours_dispatcher_failure_limit(kanban_home):
    """The guard's effective limit must follow the same resolution order
    as the circuit breaker (#35072): per-task max_retries → dispatcher
    failure_limit → DEFAULT_FAILURE_LIMIT.

    Without threading the dispatcher's ``kanban.failure_limit`` through,
    the guard falls back to DEFAULT_FAILURE_LIMIT and disagrees with the
    breaker — sticking a task prematurely (config limit > default) or
    letting a tripped task escape (config limit < default).
    """
    with kb.connect() as conn:
        # Config allows MORE retries than the default. A task blocked
        # with failures below the configured limit must still recover.
        t = kb.create_task(conn, title="lenient", assignee="a")
        conn.execute(
            "UPDATE tasks SET status='blocked', consecutive_failures=? "
            "WHERE id=?",
            (kb.DEFAULT_FAILURE_LIMIT, t),
        )
        conn.commit()
        # Default-limit call would stick it (failures >= default).
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, t).status == "blocked"
        # Dispatcher configured a higher limit → recover, preserve counter.
        promoted = kb.recompute_ready(
            conn, failure_limit=kb.DEFAULT_FAILURE_LIMIT + 2
        )
        assert promoted == 1
        task = kb.get_task(conn, t)
        assert task.status == "ready"
        assert task.consecutive_failures == kb.DEFAULT_FAILURE_LIMIT

        # Config allows FEWER retries than the default. A task at the
        # stricter limit must stay blocked even though it's below default.
        t2 = kb.create_task(conn, title="strict", assignee="a")
        conn.execute(
            "UPDATE tasks SET status='blocked', consecutive_failures=1 "
            "WHERE id=?",
            (t2,),
        )
        conn.commit()
        # Default-limit (2) would recover it (1 < 2).
        # Stricter config limit (1) must keep it blocked (1 >= 1).
        assert kb.recompute_ready(conn, failure_limit=1) == 0
        assert kb.get_task(conn, t2).status == "blocked"




# ---------------------------------------------------------------------------
# Parent-completion invariant at the claim gate (RCA t_a6acd07d)
# ---------------------------------------------------------------------------














def test_delete_archived_task_removes_related_rows(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        tid = kb.create_task(conn, title="child", parents=[parent], assignee="worker")
        kb.add_comment(conn, tid, "user", "cleanup me")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, result="done")
        assert kb.archive_task(conn, tid)
        conn.execute(
            "INSERT INTO kanban_notify_subs(task_id, platform, chat_id, thread_id, user_id, created_at, last_event_id) "
            "VALUES (?, 'telegram', '123', '', 'u', 0, 0)",
            (tid,),
        )
        conn.commit()

        assert kb.delete_archived_task(conn, tid) is True
        assert kb.get_task(conn, tid) is None
        assert conn.execute("SELECT COUNT(*) FROM task_links WHERE child_id = ? OR parent_id = ?", (tid, tid)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (tid,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (tid,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM kanban_notify_subs WHERE task_id = ?", (tid,)).fetchone()[0] == 0


def test_delete_task_removes_task_and_cascades(kanban_home):
    with kb.connect() as conn:
        t = kb.create_task(conn, title="to-delete", assignee="alice")
        kb.add_comment(conn, t, "user", "comment")
        kb.add_comment(conn, t, "user", "another")
        assert kb.delete_task(conn, t)
        assert kb.get_task(conn, t) is None
        assert len(kb.list_comments(conn, t)) == 0
        assert len(kb.list_events(conn, t)) == 0
        assert len(kb.list_runs(conn, t)) == 0




# ---------------------------------------------------------------------------
# Comments / events / worker context
# ---------------------------------------------------------------------------







# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------





# ---------------------------------------------------------------------------
# Respawn guard (check_respawn_guard + dispatch_once integration)
# ---------------------------------------------------------------------------







def test_respawn_guard_blocker_auth_on_authentication_error(kanban_home):
    """Full word 'Authentication' triggers blocker_auth (regex covers auth\\w*)."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="authn-task", assignee="alice")
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            ("Authentication failed: invalid credentials", t),
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason == "blocker_auth"


def test_respawn_guard_blocker_auth_on_authorization_error(kanban_home):
    """Full word 'authorization' triggers blocker_auth (regex covers auth\\w*)."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="authz-task", assignee="alice")
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            ("authorization denied for scope repo", t),
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason == "blocker_auth"


def test_respawn_guard_recent_success(kanban_home):
    """A completed run within the guard window triggers recent_success."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="already-done", assignee="alice")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (t, now - 120, now - 60),
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason == "recent_success"


def test_respawn_guard_recent_success_bypassed_by_requeue(kanban_home):
    """An explicit re-queue after a recent success (operator done->ready,
    promote, unblock, reclaim) is a deliberate re-run and must bypass the
    recent_success guard — otherwise a manual done->ready just sits there
    until the window elapses."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="rerun-me", assignee="alice")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (t, now - 120, now - 60),
        )
        # Baseline: a recent completion defers the respawn.
        assert kb.check_respawn_guard(conn, t) == "recent_success"
        # Operator drags done -> ready: a 'status' event after completion.
        conn.execute(
            "INSERT INTO task_events (task_id, kind, created_at) "
            "VALUES (?, 'status', ?)",
            (t, now - 10),
        )
        assert kb.check_respawn_guard(conn, t) is None


def test_respawn_guard_stale_success_not_guarded(kanban_home):
    """A completed run outside the guard window does not block re-spawn."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="old-done", assignee="alice")
        old_end = int(time.time()) - kb._RESPAWN_GUARD_SUCCESS_WINDOW - 60
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (t, old_end - 300, old_end),
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason is None


def test_respawn_guard_active_pr_in_comment(kanban_home):
    """A GitHub PR URL in a recent comment by an OWN-WORKER triggers active_pr
    when the PR is OPEN (composed guard: own-worker author + OPEN state)."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="has-pr", assignee="alice")
        # Seed a prior run so the comment author "worker" counts as an
        # own-worker (task_runs.profile) for the author-restriction half.
        kb.claim_task(conn, t)
        run_id = kb.get_task(conn, t).current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='completed', status='completed', "
            "ended_at=? WHERE id=?",
            (int(time.time()) - 7200, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, "
            "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL WHERE id=?",
            (t,),
        )
        kb.add_comment(
            conn, t, "alice",
            "PR created: https://github.com/totemx-AI/subsidysmart/pull/42",
        )
        with unittest.mock.patch.object(kb, "_github_pr_state", return_value="OPEN"):
            reason = kb.check_respawn_guard(conn, t)
    assert reason == "active_pr"


def test_respawn_guard_active_pr_unknown_state_fails_closed(kanban_home):
    """When gh cannot resolve PR state (None), the guard stays (fail closed)."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="unknown-pr", assignee="alice")
        kb.claim_task(conn, t)
        run_id = kb.get_task(conn, t).current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='completed', status='completed', "
            "ended_at=? WHERE id=?",
            (int(time.time()) - 7200, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, "
            "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL WHERE id=?",
            (t,),
        )
        kb.add_comment(
            conn, t, "alice",
            "PR created: https://github.com/totemx-AI/subsidysmart/pull/43",
        )
        with unittest.mock.patch.object(kb, "_github_pr_state", return_value=None):
            reason = kb.check_respawn_guard(conn, t)
    assert reason == "active_pr"


def test_respawn_guard_merged_pr_not_guarded(kanban_home):
    """A MERGED GitHub PR in a recent comment must NOT block re-spawn (t_9799c507).

    The PR-state-blind guard used to stall MERGED-PR cards; here a third-party
    comment references a MERGED PR and the card was never spawned by that
    commenter, so the composed guard returns None regardless.

    Replay of the sycode-trading/t_30c13209 stall: PR #856 is MERGED, so the
    active_pr guard must not fire even though the PR URL is in recent comments.
    """
    with kb.connect() as conn:
        t = kb.create_task(conn, title="merged-pr", assignee="alice")
        kb.add_comment(
            conn, t, "fable-reviewer",
            "PR merged: https://github.com/sycamoregroupltd/sycode-trading/pull/856",
        )
        with unittest.mock.patch.object(kb, "_github_pr_state", return_value="MERGED"):
            reason = kb.check_respawn_guard(conn, t)
    assert reason is None


def test_respawn_guard_closed_pr_not_guarded(kanban_home):
    """A CLOSED GitHub PR in a recent comment must NOT block re-spawn.

    Third-party comment (reviewer lane pattern) — never spawned by the
    commenter — so the author-restriction half keeps the card dispatchable.
    """
    with kb.connect() as conn:
        t = kb.create_task(conn, title="closed-pr", assignee="alice")
        kb.add_comment(
            conn, t, "fable-reviewer",
            "PR closed: https://github.com/totemx-AI/subsidysmart/pull/44",
        )
        with unittest.mock.patch.object(kb, "_github_pr_state", return_value="CLOSED"):
            reason = kb.check_respawn_guard(conn, t)
    assert reason is None


def test_respawn_guard_mixed_pr_states_guards_on_open(kanban_home):
    """One OPEN PR among MERGED/CLOSED PRs still blocks re-spawn.

    The comment is authored by an own-worker (seeded prior run) so the
    author-restriction half admits the scan, and the OPEN PR wins (fail
    closed) over the MERGED one.
    """
    with kb.connect() as conn:
        t = kb.create_task(conn, title="mixed-pr", assignee="alice")
        kb.claim_task(conn, t)
        run_id = kb.get_task(conn, t).current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='completed', status='completed', "
            "ended_at=? WHERE id=?",
            (int(time.time()) - 7200, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, "
            "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL WHERE id=?",
            (t,),
        )
        kb.add_comment(
            conn, t, "alice",
            "https://github.com/a/b/pull/1 https://github.com/c/d/pull/2",
        )

        def fake_state(repo, number):
            return "MERGED" if number == "1" else "OPEN"

        with unittest.mock.patch.object(kb, "_github_pr_state", side_effect=fake_state):
            reason = kb.check_respawn_guard(conn, t)
    assert reason == "active_pr"


def test_respawn_guard_pr_state_check_disabled_keeps_legacy(kanban_home):
    """HERMES_KANBAN_PR_STATE_CHECK=0 keeps the legacy URL-only guard.

    With the state check disabled, an own-worker recent PR comment (seeded
    prior run) defers regardless of PR state — legacy behaviour preserved
    under the author-restriction half.
    """
    with unittest.mock.patch.dict(os.environ, {"HERMES_KANBAN_PR_STATE_CHECK": "0"}):
        with kb.connect() as conn:
            t = kb.create_task(conn, title="legacy-pr", assignee="alice")
            kb.claim_task(conn, t)
            run_id = kb.get_task(conn, t).current_run_id
            conn.execute(
                "UPDATE task_runs SET outcome='completed', status='completed', "
                "ended_at=? WHERE id=?",
                (int(time.time()) - 7200, run_id),
            )
            conn.execute(
                "UPDATE tasks SET status='ready', current_run_id=NULL, "
                "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL WHERE id=?",
                (t,),
            )
            kb.add_comment(
                conn, t, "alice",
                "PR created: https://github.com/totemx-AI/subsidysmart/pull/45",
            )
            reason = kb.check_respawn_guard(conn, t)
    assert reason == "active_pr"


def test_respawn_guard_old_pr_comment_not_guarded(kanban_home):
    """A GitHub PR URL in a comment older than the PR window does not block."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="old-pr", assignee="alice")
        old_ts = int(time.time()) - kb._RESPAWN_GUARD_PR_WINDOW - 60
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, 'worker', "
            "'PR: https://github.com/totemx-AI/subsidysmart/pull/10', ?)",
            (t, old_ts),
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason is None


def test_dispatch_respawn_guard_defers_auth_error_without_auto_block(
    kanban_home, all_assignees_spawnable
):
    """dispatch_once defers (does NOT auto-block) a ready task whose last
    error is a blocker_auth.

    The old behaviour auto-blocked on first occurrence, which was too
    aggressive: a transient 429 rate-limit (which typically clears in
    seconds to minutes) would end up requiring manual unblock. The new
    behaviour defers the spawn this tick; the task stays in ``ready``
    and gets another chance next tick. If the auth error genuinely
    persists, the existing ``consecutive_failures`` circuit breaker
    will auto-block via the normal failure-limit path.
    """
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kb.connect() as conn:
        t = kb.create_task(conn, title="quota-storm", assignee="alice")
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            ("rate limit exceeded: 429 Too Many Requests", t),
        )
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    # Critical: task is NOT auto-blocked on first occurrence.
    assert t not in res.auto_blocked, (
        f"blocker_auth should defer, not auto-block on first occurrence; "
        f"got auto_blocked={res.auto_blocked!r}"
    )
    # It IS recorded as respawn_guarded with the reason.
    assert (t, "blocker_auth") in res.respawn_guarded, (
        f"expected (task_id, 'blocker_auth') in respawn_guarded; "
        f"got {res.respawn_guarded!r}"
    )
    # And it's NOT spawned this tick.
    assert t not in spawned_ids
    # Status stays ``ready`` so a future tick (or operator action) can
    # retry without manual unblock.
    with kb.connect() as conn:
        assert kb.get_task(conn, t).status == "ready"


def test_dispatch_respawn_guard_skips_recent_success(
    kanban_home, all_assignees_spawnable
):
    """dispatch_once skips (but does not block) a task with a recent completed run."""
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kb.connect() as conn:
        t = kb.create_task(conn, title="recent-winner", assignee="alice")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (t, now - 300, now - 60),
        )
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    assert (t, "recent_success") in res.respawn_guarded
    assert t not in spawned_ids
    assert t not in res.auto_blocked
    with kb.connect() as conn:
        assert kb.get_task(conn, t).status == "ready"  # not blocked, just skipped


def test_dispatch_respawn_guard_skips_active_pr(
    kanban_home, all_assignees_spawnable
):
    """dispatch_once defers (does NOT auto-block) a ready task whose own worker
    previously opened an OPEN PR (composed guard: own-worker author + OPEN state).
    """
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kb.connect() as conn:
        t = kb.create_task(conn, title="has-pr", assignee="alice")
        kb.claim_task(conn, t)
        run_id = kb.get_task(conn, t).current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='completed', status='completed', "
            "ended_at=? WHERE id=?",
            (int(time.time()) - 7200, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, "
            "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL WHERE id=?",
            (t,),
        )
        kb.add_comment(
            conn, t, "alice",
            "Opened https://github.com/totemx-AI/subsidysmart/pull/99",
        )
        with unittest.mock.patch.object(kb, "_github_pr_state", return_value="OPEN"):
            res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    assert (t, "active_pr") in res.respawn_guarded
    assert t not in spawned_ids
    assert t not in res.auto_blocked
    with kb.connect() as conn:
        assert kb.get_task(conn, t).status == "ready"


def test_dispatch_respawn_guard_spawns_when_pr_merged(
    kanban_home, all_assignees_spawnable
):
    """dispatch_once SPAWNS a ready task whose own worker's recent PR comment is
    MERGED (t_9799c507 regression: the PR-state-blind guard used to block it;
    t_0536fe58 keeps the own-worker scan, but a MERGED PR does not block).
    """
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kb.connect() as conn:
        t = kb.create_task(conn, title="merged-pr", assignee="alice")
        kb.claim_task(conn, t)
        run_id = kb.get_task(conn, t).current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='completed', status='completed', "
            "ended_at=? WHERE id=?",
            (int(time.time()) - 7200, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, "
            "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL WHERE id=?",
            (t,),
        )
        kb.add_comment(
            conn, t, "alice",
            "Opened https://github.com/sycamoregroupltd/sycode-trading/pull/856",
        )
        with unittest.mock.patch.object(kb, "_github_pr_state", return_value="MERGED"):
            res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    assert (t, "active_pr") not in res.respawn_guarded
    assert t in spawned_ids
    assert t not in res.auto_blocked


def test_dispatch_respawn_guard_dry_run_no_auto_block(
    kanban_home, all_assignees_spawnable
):
    """In dry_run mode, blocker_auth tasks are recorded in respawn_guarded (not auto-blocked)."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="dry-quota", assignee="alice")
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            ("quota exceeded", t),
        )
        res = kb.dispatch_once(conn, dry_run=True)

    assert (t, "blocker_auth") in res.respawn_guarded
    assert t not in res.auto_blocked
    with kb.connect() as conn:
        assert kb.get_task(conn, t).status == "ready"  # dry_run: no writes


def test_dispatch_respawn_guard_allows_clean_task(
    kanban_home, all_assignees_spawnable
):
    """A task with no guard triggers is spawned normally."""
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kb.connect() as conn:
        t = kb.create_task(conn, title="clean-task", assignee="alice")
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    assert t in spawned_ids
    assert not res.respawn_guarded
    assert t not in res.auto_blocked


def test_dispatch_respawn_guard_emits_event_for_skipped_task(
    kanban_home, all_assignees_spawnable
):
    """dispatch_once emits a respawn_guarded task_event so operators can diagnose stuck-ready tasks."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="event-check", assignee="alice")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
            "VALUES (?, 'done', 'completed', ?, ?)",
            (t, now - 300, now - 60),
        )
        kb.dispatch_once(conn, spawn_fn=lambda task, ws: None)
        events = kb.list_events(conn, t)

    kinds = [e.kind for e in events]
    assert "respawn_guarded" in kinds
    guarded_evt = next(e for e in events if e.kind == "respawn_guarded")
    # Event.payload is already parsed as a dict by list_events.
    assert isinstance(guarded_evt.payload, dict)
    assert guarded_evt.payload.get("reason") == "recent_success"


# ---------------------------------------------------------------------------
# Workspace resolution
# ---------------------------------------------------------------------------









def test_worktree_workspace_explicit_target_materializes_linked_worktree(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    target = repo / ".worktrees" / "custom-task"
    branch = "wt/custom-task"
    with kb.connect() as conn:
        t = kb.create_task(
            conn,
            title="ship",
            workspace_kind="worktree",
            workspace_path=str(target),
            branch_name=branch,
        )
        task = kb.get_task(conn, t)
        assert task is not None
        ws = kb.resolve_workspace(task)

    assert ws == target
    assert ws.exists()
    repo_common = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    ws_common = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert ws_common == repo_common
    listed = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert f"worktree {target}" in listed
    assert f"branch refs/heads/{branch}" in listed


# ---------------------------------------------------------------------------
# Scratch cleanup containment (#28818)
# ---------------------------------------------------------------------------



def test_complete_task_persists_scratch_artifacts_before_cleanup(kanban_home):
    """Completion artifacts from scratch workspaces survive workspace cleanup."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="render chart")
        task = kb.get_task(conn, t)
        ws = kb.resolve_workspace(task)
        kb.set_workspace_path(conn, t, ws)
        artifact = ws / "chart.png"
        artifact.write_bytes(b"png-bytes")

        assert kb.complete_task(
            conn,
            t,
            result="ok",
            metadata={"artifacts": [str(artifact)]},
        )

        completed = [e for e in kb.list_events(conn, t) if e.kind == "completed"][-1]
        persisted = Path(completed.payload["artifacts"][0])
        run = kb.latest_run(conn, t)

    assert not ws.exists(), "scratch workspace should still be cleaned up"
    assert persisted.exists(), "artifact copy should survive scratch cleanup"
    assert persisted.parent == kb.task_attachments_dir(t)
    assert persisted.name == "chart.png"
    assert persisted.read_bytes() == b"png-bytes"
    assert str(persisted) != str(artifact)
    assert run is not None
    assert run.metadata["artifacts"] == [str(persisted)]
    with kb.connect() as conn:
        attachments = kb.list_attachments(conn, t)
    assert [(a.filename, a.stored_path) for a in attachments] == [
        ("chart.png", str(persisted.resolve()))
    ]




# ---------------------------------------------------------------------------
# Deferred scratch cleanup for parent/child handoff (#33774)
# ---------------------------------------------------------------------------




def test_dir_child_completion_unblocks_deferred_scratch_parent(kanban_home, tmp_path):
    """A non-scratch ('dir') child completing must still sweep its scratch parent.

    Regression for the gap where ``_cleanup_workspace`` returned early for a
    non-scratch task and never ran the parent sweep — leaking the parent's
    deferred scratch dir forever.
    """
    child_dir = tmp_path / "persistent-child"
    child_dir.mkdir()
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="scratch parent")
        child = kb.create_task(
            conn, title="dir child", workspace_kind="dir",
            workspace_path=str(child_dir),
        )
        kb.link_tasks(conn, parent, child)
        p_task = kb.get_task(conn, parent)
        parent_ws = kb.resolve_workspace(p_task)
        kb.set_workspace_path(conn, parent, parent_ws)

        kb.complete_task(conn, parent, result="handoff")
        assert parent_ws.exists(), "deferred while dir child active"

        kb.complete_task(conn, child, result="built")

    assert not parent_ws.exists(), (
        "A 'dir' child completing must trigger the parent scratch sweep"
    )
    assert child_dir.exists(), "Non-scratch 'dir' child workspace is never deleted"




def test_is_managed_scratch_path_rejects_kanban_metadata_subtrees(kanban_home):
    """Hermes' own DB/metadata/log subtrees under ``<kanban_home>/kanban`` are NOT managed.

    Regression guard for the Copilot finding on #28819: a scratch task whose
    ``workspace_path`` was mis-set to the kanban home, the logs dir, or a
    board's metadata dir (i.e. the board root itself, not its ``workspaces/``
    child) must be refused. Without this, the containment check would happily
    ``shutil.rmtree`` Hermes' DB/metadata/logs on task completion.
    """
    kanban_root = kanban_home / "kanban"
    kanban_root.mkdir(parents=True, exist_ok=True)
    assert not kb._is_managed_scratch_path(kanban_root)

    logs_dir = kanban_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    assert not kb._is_managed_scratch_path(logs_dir)

    board_root = kanban_root / "boards" / "my-board"
    board_root.mkdir(parents=True, exist_ok=True)
    # The board root itself is NOT a managed scratch dir — only the
    # ``workspaces/`` child (and its descendants) are.
    assert not kb._is_managed_scratch_path(board_root)

    # Sibling subtrees of ``workspaces/`` under a board (e.g. its kanban.db
    # or board.json living next to ``workspaces/``) are also not managed.
    board_logs = board_root / "logs"
    board_logs.mkdir(parents=True, exist_ok=True)
    assert not kb._is_managed_scratch_path(board_logs)

    # Now create the board's workspaces dir and a task scratch dir under it —
    # the latter is the only thing the guard should allow.
    board_workspaces = board_root / "workspaces"
    board_workspaces.mkdir(parents=True, exist_ok=True)
    # The workspaces root itself is also NOT managed — deleting it would
    # wipe every task's scratch dir at once.
    assert not kb._is_managed_scratch_path(board_workspaces)
    task_dir = board_workspaces / "task-42"
    task_dir.mkdir(parents=True, exist_ok=True)
    assert kb._is_managed_scratch_path(task_dir)


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------









# ---------------------------------------------------------------------------
# Originating session id (ACP propagation)
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# Shared-board path resolution (issue #19348)
#
# The kanban board is a cross-profile coordination primitive: a worker
# spawned with `hermes -p <profile>` must read/write the same kanban.db
# as the dispatcher that claimed the task. These tests exercise the
# path-resolution layer directly and would have caught the regression
# where `kanban_db_path()` resolved to the active profile's HERMES_HOME.
# ---------------------------------------------------------------------------

class TestSharedBoardPaths:
    """`kanban_home`/`kanban_db_path`/`workspaces_root`/`worker_log_path`
    must anchor at the **shared root**, not the active profile's HERMES_HOME."""

    def _set_home(self, monkeypatch, tmp_path, hermes_home):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)


    def test_profile_worker_resolves_to_shared_root(
        self, tmp_path, monkeypatch
    ):
        # Reproduces the bug: dispatcher uses ~/.hermes/kanban.db,
        # worker spawned with -p <profile> previously resolved to
        # ~/.hermes/profiles/<profile>/kanban.db. After the fix both
        # converge on ~/.hermes/kanban.db.
        default_home = tmp_path / ".hermes"
        default_home.mkdir()
        profile_home = default_home / "profiles" / "nehemiahkanban"
        profile_home.mkdir(parents=True)
        self._set_home(monkeypatch, tmp_path, profile_home)

        # All four resolvers must anchor at the shared root, not the
        # profile-local HERMES_HOME.
        assert kb.kanban_home() == default_home
        assert kb.kanban_db_path() == default_home / "kanban.db"
        assert kb.workspaces_root() == default_home / "kanban" / "workspaces"
        assert (
            kb.worker_log_path("t_0d214f19")
            == default_home / "kanban" / "logs" / "t_0d214f19.log"
        )

        # Sanity: the profile-local path that used to be returned is
        # explicitly NOT what we resolve to anymore.
        assert kb.kanban_db_path() != profile_home / "kanban.db"






    def test_dispatcher_and_worker_share_a_real_database(
        self, tmp_path, monkeypatch
    ):
        # Belt-and-suspenders: round-trip a task across the two
        # HERMES_HOME perspectives via a real SQLite file. Without the
        # fix the worker would open a different file and see no rows.
        default_home = tmp_path / ".hermes"
        default_home.mkdir()
        profile_home = default_home / "profiles" / "nehemiahkanban"
        profile_home.mkdir(parents=True)

        # Dispatcher creates the board and a task.
        self._set_home(monkeypatch, tmp_path, default_home)
        kb.init_db()
        with kb.connect() as conn:
            task_id = kb.create_task(conn, title="cross-profile")

        # Worker switches to the profile HERMES_HOME and reads.
        monkeypatch.setenv("HERMES_HOME", str(profile_home))
        with kb.connect() as conn:
            task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.title == "cross-profile"




    def test_dispatcher_spawn_injects_kanban_paths_without_stale_session(
        self, tmp_path, monkeypatch
    ):
        # The dispatcher must pin board paths while stripping any unrelated
        # HERMES_SESSION_* identity inherited from the long-lived gateway.
        # The one exception is HERMES_SESSION_SOURCE, which the dispatcher
        # re-sets to its own `kanban` tag AFTER the strip — a value it owns,
        # never one inherited from whatever the gateway last routed.
        default_home = tmp_path / ".hermes"
        default_home.mkdir()
        self._set_home(monkeypatch, tmp_path, default_home)

        from gateway import session_context as sc

        # A dispatcher can launch before the gateway binds its first session.
        monkeypatch.setattr(sc, "_session_context_engaged", False)
        sc.reset_session_vars()
        for key in sc._VAR_MAP:
            monkeypatch.setenv(key, "stale-routing-value")

        captured = {}

        class _FakePopen:
            def __init__(self, cmd, **kwargs):
                captured["cmd"] = cmd
                captured["env"] = kwargs.get("env", {})
                self.pid = 4242

        monkeypatch.setattr("subprocess.Popen", _FakePopen)

        task = kb.Task(
            id="t_dispatch_env",
            title="x",
            body=None,
            assignee="coder",
            status="ready",
            priority=0,
            created_by=None,
            created_at=0,
            started_at=None,
            completed_at=None,
            workspace_kind="worktree",
            workspace_path=str(tmp_path / "ws"),
            claim_lock=None,
            claim_expires=None,
            tenant=None,
            branch_name="wt/t_dispatch_env",
        )
        kb._default_spawn(task, str(tmp_path / "ws"))

        env = captured["env"]
        assert env["HERMES_KANBAN_DB"] == str(default_home / "kanban.db")
        assert env["HERMES_KANBAN_WORKSPACES_ROOT"] == str(
            default_home / "kanban" / "workspaces"
        )
        assert env["HERMES_KANBAN_TASK"] == "t_dispatch_env"
        assert env["HERMES_KANBAN_BRANCH"] == "wt/t_dispatch_env"
        for key in sc._VAR_MAP:
            if key == "HERMES_SESSION_SOURCE":
                # Re-set by the dispatcher, so what matters is that it carries
                # the worker's own tag rather than the inherited routing value.
                assert env[key] == "kanban"
                continue
            assert key not in env


# ---------------------------------------------------------------------------
# latest_summary / latest_summaries — surface task_runs.summary handoffs
# ---------------------------------------------------------------------------








# ---------------------------------------------------------------------------
# NFS / network-filesystem fallback (see hermes_state.apply_wal_with_fallback)
# ---------------------------------------------------------------------------

def test_connect_falls_back_to_delete_on_locking_protocol(tmp_path, monkeypatch, caplog):
    """kanban_db.connect() must handle ``locking protocol`` on NFS/SMB.

    Without this fallback, the gateway's kanban dispatcher crashes every
    60s and the kanban migration (``consecutive_failures`` ADD COLUMN) is
    retried forever — which is what the real-world user report shows
    (see hermes-agent issue #22032).

    NOTE: We do NOT use the ``kanban_home`` fixture here because that
    fixture pre-initializes the DB via ``kb.init_db()`` — putting the
    file in WAL on disk. The Bug D safety guard now refuses to downgrade
    to DELETE when the on-disk header is already WAL, so testing the
    NFS-fallback path requires a truly-fresh DB file (NFS scenario in
    production: first connection of the first process ever to touch the
    file, where downgrading is safe because nobody else has WAL state
    yet).
    """
    import sqlite3 as _sqlite3
    from unittest.mock import patch as _patch

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    # These tests exercise the WAL-attempt path; assume a fixed SQLite so the
    # WAL-reset vulnerability gate doesn't short-circuit before the pragma.
    import hermes_state as _hermes_state
    monkeypatch.setattr(
        _hermes_state, "is_sqlite_wal_reset_vulnerable",
        lambda version_info=None: False,
    )
    _hermes_state._wal_fallback_warned_paths.clear()

    # Clear module cache so a fresh connect() is attempted
    kb._INITIALIZED_PATHS.clear()
    hermes_state._wal_fallback_warned_paths.clear()

    real_connect = _sqlite3.connect

    class _WalBlockingConnection(_sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):  # type: ignore[override]
            if "journal_mode=wal" in sql.lower().replace(" ", ""):
                raise _sqlite3.OperationalError("locking protocol")
            return super().execute(sql, *args, **kwargs)

    def wal_blocking_connect(*args, **kwargs):
        # connect_tracked passes a tracking-augmented factory; drop it and
        # substitute the double, which connect_tracked re-applies to the
        # returned instance.
        kwargs.pop("factory", None)
        return real_connect(
            *args, factory=_WalBlockingConnection, **kwargs
        )

    with _patch("hermes_cli.kanban_db.sqlite3.connect", side_effect=wal_blocking_connect):
        with caplog.at_level("ERROR", logger="hermes_state"):
            conn = kb.connect()

    # One fallback error, naming kanban.db
    errors = [
        r
        for r in caplog.records
        if r.levelname == "ERROR" and "kanban.db" in r.getMessage()
    ]
    assert len(errors) >= 1, (
        f"Expected a kanban.db ERROR, got: {[r.getMessage() for r in caplog.records]}"
    )

    # DB still usable end-to-end — create + list a task
    t = kb.create_task(conn, title="post-fallback task")
    tasks = kb.list_tasks(conn)
    assert any(row.id == t for row in tasks)
    conn.close()


def test_connect_works_when_wal_is_silently_refused(tmp_path, monkeypatch, caplog):
    """kanban_db.connect() must stay usable when WAL silently no-ops to DELETE."""
    import sqlite3 as _sqlite3
    from unittest.mock import patch as _patch

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    kb._INITIALIZED_PATHS.clear()
    hermes_state._wal_fallback_warned_paths.clear()
    # Assume a fixed SQLite so the WAL-reset gate doesn't short-circuit.
    monkeypatch.setattr(
        hermes_state, "is_sqlite_wal_reset_vulnerable",
        lambda version_info=None: False,
    )

    real_connect = _sqlite3.connect

    class _WalSilentNoOpConnection(_sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):  # type: ignore[override]
            if "journal_mode=wal" in sql.lower().replace(" ", ""):
                return super().execute("PRAGMA journal_mode=delete", *args, **kwargs)
            return super().execute(sql, *args, **kwargs)

    def wal_silent_noop_connect(*args, **kwargs):
        kwargs.pop("factory", None)
        return real_connect(
            *args, factory=_WalSilentNoOpConnection, **kwargs
        )

    with _patch(
        "hermes_cli.kanban_db.sqlite3.connect",
        side_effect=wal_silent_noop_connect,
    ):
        with caplog.at_level("ERROR", logger="hermes_state"):
            conn = kb.connect()

    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
    t = kb.create_task(conn, title="post-silent-fallback task")
    tasks = kb.list_tasks(conn)
    assert any(row.id == t for row in tasks)
    conn.close()

    errors = [
        r
        for r in caplog.records
        if r.levelname == "ERROR" and "kanban.db" in r.getMessage()
    ]
    assert len(errors) >= 1, (
        f"Expected a kanban.db ERROR, got: {[r.getMessage() for r in caplog.records]}"
    )


def test_sqlite_connect_closes_tracked_conn_on_setup_failure(tmp_path, monkeypatch):
    """A PRAGMA failure after connect must not abandon a tracked kanban fd."""
    from hermes_cli import sqlite_safe_read

    db_path = tmp_path / "kanban.db"
    real_connect = sqlite3.connect
    opened = []

    class _BusyTimeoutFailure(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):  # type: ignore[override]
            if str(sql).startswith("PRAGMA busy_timeout="):
                raise sqlite3.OperationalError("simulated setup failure")
            return super().execute(sql, *args, **kwargs)

    def failing_connect(*args, **kwargs):
        kwargs.pop("factory", None)
        conn = real_connect(*args, factory=_BusyTimeoutFailure, **kwargs)
        opened.append(conn)
        return conn

    key = sqlite_safe_read._key(db_path)
    with sqlite_safe_read._live_lock:
        before = sqlite_safe_read._live_connections.get(key, 0)
    monkeypatch.setattr(kb.sqlite3, "connect", failing_connect)

    with pytest.raises(sqlite3.OperationalError, match="simulated setup failure"):
        kb._sqlite_connect(db_path)

    with sqlite_safe_read._live_lock:
        after = sqlite_safe_read._live_connections.get(key, 0)
    assert after == before


def test_unlink_tasks_triggers_recompute_ready(kanban_home):
    """Regression test for issue #22459.

    Removing a dependency via unlink_tasks must immediately promote the child
    to ready when all remaining parents are done — same contract as
    complete_task and unblock_task.

    Before the fix, child stayed 'todo' indefinitely after unlink; only the
    next dispatcher tick or a manual 'hermes kanban recompute' would promote it.
    """
    with kb.connect() as conn:
        # A is done.
        a = kb.create_task(conn, title="parent-done")
        kb.complete_task(conn, a)

        # C is running (not done) — blocks child B.
        c = kb.create_task(conn, title="parent-running")
        kb.claim_task(conn, c, claimer="worker:1")

        # B depends on both A (done) and C (running) → stays todo.
        b = kb.create_task(conn, title="child", parents=[a, c])
        assert kb.get_task(conn, b).status == "todo"

        # Remove the blocking dependency C → B.
        removed = kb.unlink_tasks(conn, c, b)
        assert removed is True

        # B's only remaining parent is A (done) → must be ready immediately.
        assert kb.get_task(conn, b).status == "ready", (
            "child should promote to ready immediately after unlink_tasks "
            "removes its last blocking dependency"
        )



# ---------------------------------------------------------------------------
# _add_column_if_missing / _migrate_add_optional_columns idempotency (#21708)
# ---------------------------------------------------------------------------

def test_add_column_if_missing_is_idempotent_on_race(kanban_home):
    """``_add_column_if_missing`` must swallow 'duplicate column name' errors.

    Regression for #21708: the kanban dispatcher opens the DB twice per tick
    (once via _tick_once_for_board, once via init_db's discard-and-reconnect
    path).  A second concurrent connection runs _migrate_add_optional_columns
    before the first one commits, so ALTER TABLE raises OperationalError with
    'duplicate column name: consecutive_failures'.  Without the idempotency
    guard that crashes the dispatcher on the first tick after every restart.
    """
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT NOT NULL)"
    )

    # First call adds the column — returns True.
    added = kb._add_column_if_missing(conn, "tasks", "extra_col", "extra_col TEXT")
    assert added is True
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    assert "extra_col" in cols

    # Second call on same connection — column already exists — must return
    # False without raising, simulating the race the dispatcher hits.
    added_again = kb._add_column_if_missing(
        conn, "tasks", "extra_col", "extra_col TEXT"
    )
    assert added_again is False

    conn.close()


def test_migrate_add_optional_columns_tolerates_concurrent_migration(kanban_home):
    """Full _migrate_add_optional_columns must not raise when columns already
    exist (issue #21708 race window — two connections migrate concurrently)."""
    import sqlite3

    # Schema already in fully-migrated state (all optional columns present).
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            tenant TEXT,
            result TEXT,
            idempotency_key TEXT,
            branch_name TEXT,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            worker_pid INTEGER,
            last_failure_error TEXT,
            max_runtime_seconds INTEGER,
            last_heartbeat_at INTEGER,
            current_run_id INTEGER,
            workflow_template_id TEXT,
            current_step_key TEXT,
            skills TEXT,
            max_retries INTEGER,
            session_id TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE task_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id    TEXT NOT NULL DEFAULT '',
            run_id     INTEGER,
            kind       TEXT NOT NULL DEFAULT '',
            payload    TEXT,
            created_at INTEGER NOT NULL DEFAULT 0
        )
        """
    )

    # Running migration on an already-migrated schema must not raise.
    kb._migrate_add_optional_columns(conn)
    conn.close()


# ---------------------------------------------------------------------------
# Dispatcher spawn invocation — _resolve_hermes_argv()
#
# Workers spawned by the dispatcher must use a `hermes` invocation that does
# not depend on PATH being set up correctly. cron jobs, systemd User= services,
# launchd jobs, and other detached processes routinely run with a stripped
# $PATH that doesn't include the venv's bin/, so a bare `["hermes", ...]`
# spawn fails with FileNotFoundError and the task gets stuck. The resolver
# prefers the PATH shim (familiar `ps` output) but falls back to the module
# form so the spawn keeps working when PATH is missing the shim.
# ---------------------------------------------------------------------------


def test_resolve_hermes_argv_falls_back_to_module_form_when_no_path_shim(monkeypatch):
    """When the shim is not on PATH, fall back to `python -m hermes_cli.main`.

    Pins the correct module name (NOT `hermes` — there is no top-level
    `hermes` package). Regression for #23198: the original PR shipped
    `python -m hermes` which fails with `No module named hermes` on every
    invocation.
    """
    import shutil
    import sys
    import hermes_cli.kanban_db as kb

    monkeypatch.delenv("HERMES_BIN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    argv = kb._resolve_hermes_argv()
    assert argv == [sys.executable, "-m", "hermes_cli.main"]


def test_resolve_hermes_argv_module_actually_runs():
    """The fallback module name must be importable + runnable.

    A unit test that pins the literal string is necessary but not
    sufficient — if `hermes_cli.main` ever loses `if __name__ == "__main__"`
    handling or its argparse setup, `python -m hermes_cli.main --version`
    would fail and so would every dispatcher spawn that hits the fallback.
    Run it as a real subprocess to catch that regression.
    """
    import subprocess
    import hermes_cli.kanban_db as kb
    import shutil
    import unittest.mock as mock

    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("HERMES_BIN", None)
        with mock.patch.object(shutil, "which", return_value=None):
            argv = kb._resolve_hermes_argv()
    r = subprocess.run(argv + ["--version"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, (
        f"`{' '.join(argv)} --version` failed (rc={r.returncode}); "
        f"stderr={r.stderr[:200]!r}"
    )
    assert "Hermes Agent" in r.stdout, f"unexpected output: {r.stdout[:200]!r}"


# ---------------------------------------------------------------------------
# task_age — guard against corrupt timestamp values
#
# The Task dataclass declares ``created_at: int`` but rows come from sqlite
# without coercion at the boundary. A row that ever held a non-int (e.g. an
# unsubstituted ``'%s'`` from a logged format string, ``None``, an arbitrary
# string, or a float-as-string) used to crash ``task_age`` with ``ValueError``
# and turn ``GET /api/plugins/kanban/board`` into a 500 because the dashboard
# calls ``task_age`` unguarded for every task in the response.
#
# After the fix, ``_safe_int`` returns ``None`` on bad input and ``task_age``
# degrades gracefully (per-field ``None`` rather than a hard crash).
# ---------------------------------------------------------------------------


def _make_task(**overrides) -> "kb.Task":
    """Minimal Task with all required fields filled in. Override anything."""
    defaults = dict(
        id="t_age",
        title="x",
        body=None,
        assignee=None,
        status="ready",
        priority=0,
        created_by=None,
        created_at=0,
        started_at=None,
        completed_at=None,
        workspace_kind="scratch",
        workspace_path=None,
        claim_lock=None,
        claim_expires=None,
        tenant=None,
    )
    defaults.update(overrides)
    return kb.Task(**defaults)












# ---------------------------------------------------------------------------
# Board-level default_workdir
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# dispatch_once — max_in_progress
# ---------------------------------------------------------------------------


def test_dispatch_max_in_progress_blocks_review_when_at_limit(
    kanban_home, all_assignees_spawnable,
):
    """Review-only backlog must still respect max_in_progress."""
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kb.connect() as conn:
        running = kb.create_task(conn, title="running", assignee="alice")
        kb.claim_task(conn, running)
        review = kb.create_task(conn, title="review", assignee="bob")
        _set_task_status(conn, review, "review")
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn, max_in_progress=1)
        review_task = kb.get_task(conn, review)

    assert not res.spawned
    assert not spawns
    assert review_task is not None
    assert review_task.status == "review"

# Review column dispatch
# ---------------------------------------------------------------------------


def _set_task_status(conn: sqlite3.Connection, task_id: str, status: str) -> None:
    """Test helper: set a task's status directly."""
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))








# Stale detection — detect_stale_running
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Corruption guard (issue #30687)
# ---------------------------------------------------------------------------

def _write_corrupt_db(path: Path) -> bytes:
    """Write a kanban DB with a VALID SQLite header but malformed page content.

    This is the corruption shape the integrity guard specifically targets
    (e.g. issue #29507 follow-up reports where the file's first 16 bytes
    pass the header byte check but ``PRAGMA integrity_check`` then fails
    because the internal pages are damaged). It's what main's header-only
    validator was letting through, and what this PR adds the full guard
    for.
    """
    # 100-byte SQLite header (magic + minimal valid-looking fields) so the
    # cheap header check passes, then deliberate garbage so sqlite refuses
    # to read the file past the header.
    header = b"SQLite format 3\x00" + b"\x10\x00\x02\x02\x00\x40\x20\x20"
    header += b"\x00\x00\x00\x0c\x00\x00\x23\x46\x00\x00\x00\x00"
    header = header.ljust(100, b"\x00")
    payload = b"definitely not a valid sqlite page \x00\x01\x02\x03" * 64
    blob = header + payload
    path.write_bytes(blob)
    return blob




def test_repeated_corrupt_open_reuses_single_backup(tmp_path):
    """Repeated quarantines of the same corrupt bytes must not amplify disk usage.

    Regression for the gateway dispatcher's 5-min retry loop on shared kanban
    DBs across multi-profile fleets: each retry on an unchanged corrupt file
    used to create a fresh ``.corrupt.<timestamp>.bak`` until disk filled. The
    content-addressed backup name is deterministic in the DB's sha256, so
    N retries of the same bytes share one backup.
    """
    db_path = tmp_path / "kanban.db"
    original = _write_corrupt_db(db_path)

    backups: set[Path] = set()
    for _ in range(10):
        kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
        with pytest.raises(kb.KanbanDbCorruptError) as excinfo:
            kb.connect(db_path=db_path)
        assert excinfo.value.backup_path is not None
        backups.add(excinfo.value.backup_path)

    assert len(backups) == 1, f"expected 1 deterministic backup, got {len(backups)}"
    (backup,) = backups
    assert backup.exists()
    assert backup.read_bytes() == original

    # Mutate the corrupt bytes — fingerprint changes, separate backup preserved.
    with db_path.open("r+b") as f:
        f.seek(4096)
        f.write(b"\xAB" * 64)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with pytest.raises(kb.KanbanDbCorruptError) as excinfo2:
        kb.connect(db_path=db_path)
    second_backup = excinfo2.value.backup_path
    assert second_backup is not None
    assert second_backup != backup
    assert second_backup.exists()


def test_locked_healthy_db_does_not_classify_as_corrupt(tmp_path, monkeypatch):
    """A transient lock during the probe must not produce a .corrupt backup
    and must not be reported as :class:`KanbanDbCorruptError`. Raw sqlite
    ``OperationalError`` (lock/busy) is acceptable and expected."""
    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path=db_path)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    real_connect = sqlite3.connect

    def flaky_connect(*args, **kwargs):
        # First call is the integrity probe — simulate a lock.
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(kb.sqlite3, "connect", flaky_connect)

    with pytest.raises(sqlite3.OperationalError):
        kb.connect(db_path=db_path)

    # No .corrupt backup may be produced for a healthy-but-locked DB.
    backups = list(tmp_path.glob("*.corrupt.*"))
    assert backups == [], f"unexpected corrupt backups: {backups}"

    # And once the lock clears, normal access still works.
    monkeypatch.setattr(kb.sqlite3, "connect", real_connect)
    with kb.connect(db_path=db_path) as conn:
        kb.create_task(conn, title="still here")
        titles = [t.title for t in kb.list_tasks(conn)]
    assert "still here" in titles




# ---------------------------------------------------------------------------
# First-use tip for scratch workspaces
# ---------------------------------------------------------------------------

def test_maybe_emit_scratch_tip_fires_once_per_install(kanban_home, caplog):
    """First scratch workspace materialization warns + emits an event.

    Subsequent scratch workspaces on the SAME install stay silent — the
    sentinel file under kanban_home() flips after the first emit.
    """
    import logging

    with kb.connect() as conn:
        t1 = kb.create_task(conn, title="first scratch")
        t2 = kb.create_task(conn, title="second scratch")

    # Sentinel must not exist yet on a fresh install.
    assert not kb._scratch_tip_shown()

    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
        with kb.connect() as conn:
            kb._maybe_emit_scratch_tip(conn, t1, "scratch")

    # Sentinel is now set.
    assert kb._scratch_tip_shown()
    assert kb._scratch_tip_sentinel_path().exists()

    # Warning was logged exactly once.
    tip_records = [
        r for r in caplog.records
        if "scratch workspaces are ephemeral" in r.getMessage()
    ]
    assert len(tip_records) == 1, (
        f"Expected exactly one tip warning, got {len(tip_records)}: "
        f"{[r.getMessage() for r in tip_records]!r}"
    )

    # An event row was appended on the first task.
    with kb.connect() as conn:
        events = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id",
            (t1,),
        ).fetchall()
    kinds = [e["kind"] for e in events]
    assert "tip_scratch_workspace" in kinds, (
        f"Expected tip_scratch_workspace event on first scratch task; "
        f"got {kinds!r}"
    )

    # Second scratch materialization on the same install stays silent.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
        with kb.connect() as conn:
            kb._maybe_emit_scratch_tip(conn, t2, "scratch")
    tip_records2 = [
        r for r in caplog.records
        if "scratch workspaces are ephemeral" in r.getMessage()
    ]
    assert tip_records2 == [], (
        f"Tip should not re-fire after sentinel is set; got "
        f"{[r.getMessage() for r in tip_records2]!r}"
    )
    with kb.connect() as conn:
        events2 = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id",
            (t2,),
        ).fetchall()
    assert "tip_scratch_workspace" not in [e["kind"] for e in events2], (
        "Tip event should not be appended for subsequent scratch tasks."
    )




# ---------------------------------------------------------------------------
# Connection pragmas (secure_delete, cell_size_check, synchronous=FULL)
# ---------------------------------------------------------------------------


def test_connect_sets_secure_delete_on(tmp_path):
    """secure_delete=ON must be active on every new connection."""
    db_path = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect(db_path=db_path) as conn:
        row = conn.execute("PRAGMA secure_delete").fetchone()
    assert row[0] == 1, f"expected secure_delete=1, got {row[0]}"





# write_txn — rollback handler must not mask the original exception
# ---------------------------------------------------------------------------


def test_write_txn_preserves_original_exception_when_rollback_fails(kanban_home):
    """When a write inside write_txn raises an OperationalError that SQLite
    has already auto-rolled-back (e.g. ``disk I/O error``,
    ``database is locked``, ``database disk image is malformed``), the
    explicit ROLLBACK in ``write_txn.__exit__`` itself raises
    ``cannot rollback - no transaction is active``. The original cause
    must NOT be masked by the secondary rollback failure — operators rely
    on the original cause to diagnose the underlying issue.
    """

    class FailingConnWrapper:
        """Delegate to a real connection, simulating an EIO during an INSERT
        that SQLite has already auto-rolled-back."""

        def __init__(self, real):
            self._real = real
            self._fail_armed = True

        def execute(self, sql, *args, **kwargs):
            if (
                self._fail_armed
                and sql.lstrip().upper().startswith("INSERT")
                and "task_events" in sql.lower()
            ):
                self._fail_armed = False  # one-shot
                # Simulate SQLite auto-rolling back the transaction by
                # issuing a real ROLLBACK now. After this, BEGIN IMMEDIATE
                # is no longer active and an explicit ROLLBACK would error.
                try:
                    self._real.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise sqlite3.OperationalError("disk I/O error")
            return self._real.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

    with kb.connect() as conn:
        wrapper = FailingConnWrapper(conn)
        with pytest.raises(sqlite3.OperationalError) as excinfo:
            with kb.write_txn(wrapper):
                kb._append_event(wrapper, "t_bogus", "promoted", None)

    msg = str(excinfo.value)
    assert "disk I/O error" in msg, (
        f"write_txn masked the original exception with rollback failure; "
        f"got {msg!r} (expected to contain 'disk I/O error')"
    )
    assert "cannot rollback" not in msg, (
        f"write_txn surfaced the rollback failure instead of the original "
        f"OperationalError; got {msg!r}"
    )


def test_write_txn_check_reads_correct_header_fields(tmp_path):
    """A genuinely truncated DB is never reported as passing the invariant.

    The check no longer opens the database file to read header bytes (that
    open/close would cancel this process's POSIX advisory locks — the
    corruption route in sqlite.org/howtocorrupt.html §2.2). It asks SQLite for
    ``page_count`` instead. On a truncated file SQLite refuses that pragma, so
    the helper reports "not healthy" rather than a page-count mismatch; either
    way the file must never come back clean.
    """
    import struct
    from hermes_cli.kanban_db import connect
    from hermes_cli.sqlite_safe_read import file_length_matches_header

    db = tmp_path / "synthetic.db"
    conn = connect(db_path=db)
    conn.execute("PRAGMA journal_mode=DELETE")
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    conn.close()

    with open(db, "rb") as f:
        data = bytearray(f.read())
    real_page_count = struct.unpack(">I", data[28:32])[0]
    if real_page_count < 2:
        pytest.skip("DB too small for synthetic truncation test")
    truncated = bytes(data[: (real_page_count - 1) * page_size])
    with open(db, "wb") as f:
        f.write(truncated)

    raw_conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        assert file_length_matches_header(raw_conn) is not True
    finally:
        raw_conn.close()


# ---------------------------------------------------------------------------
# reap_worker_zombies() tests
# ---------------------------------------------------------------------------










# ---------------------------------------------------------------------------
# connect_closing(): context manager that actually closes the FD
# Regression coverage for #33159 (kanban.db FD leak — gateway crashes after
# ~4 days). sqlite3.Connection's built-in __exit__ commits/rollbacks but
# does NOT close, so `with kb.connect() as conn:` leaks the FD in
# long-lived processes (gateway run_slash, dashboard decompose handler).
# `connect_closing()` is the leak-safe replacement.
# ---------------------------------------------------------------------------




def test_bare_connect_does_not_close_on_context_exit(tmp_path):
    """Document the leak that connect_closing exists to prevent.

    sqlite3.Connection's __exit__ commits/rollbacks but doesn't close.
    This is the upstream behaviour we cannot change; the regression
    guard is to make sure connect_closing() does the right thing.
    """
    db_path = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect(db_path=db_path) as conn:
        pass
    # Still usable after with-block exit (the leak).
    conn.execute("SELECT 1").fetchone()
    conn.close()  # explicit close to avoid leaking THIS test


# ---------------------------------------------------------------------------
# Review-lane and service-gate de-dup helpers (Phase 2F)
# ---------------------------------------------------------------------------

def test_review_lane_dependency_warning_flags_inverted_reviewer_child(kanban_home):
    with kb.connect() as conn:
        source = kb.create_task(conn, title="ship ACL change", assignee="worker")
        kb.block_task(conn, source, reason="review-required: guardian eyes before landing")
        review = kb.create_task(
            conn,
            title="REVIEW: ACL change",
            body=f"Source task {source}; post REVIEW_VERDICT=APPROVE or CHANGES_REQUESTED.",
            assignee="os-reviewer",
            parents=[source],
        )

        warning = kb.review_lane_dependency_warning(conn, review)

    assert warning is not None
    assert warning["source_task_id"] == source
    assert warning["source_status"] == "blocked"


def test_review_lane_dependency_warning_ignores_implementation_child(kanban_home):
    with kb.connect() as conn:
        source = kb.create_task(conn, title="ship ACL change", assignee="worker")
        kb.block_task(conn, source, reason="review-required: guardian eyes before landing")
        impl = kb.create_task(
            conn,
            title="IMPLEMENT: reviewed ACL change",
            body=f"Run only after source {source} is terminal.",
            assignee="worker",
            parents=[source],
        )

        warning = kb.review_lane_dependency_warning(conn, impl)

    assert warning is None


def test_service_gate_dedupe_suppresses_duplicate_for_active_lane(kanban_home):
    with kb.connect() as conn:
        source = kb.create_task(conn, title="Discord delivery blocked", assignee="pm")
        lane = kb.create_task(
            conn,
            title="SERVICE-GATE delivery-health repair",
            body=f"source={source} delivery-health config-owner repair lane",
            assignee="infra-optimizer",
        )

        decision = kb.service_gate_dedupe_decision(
            conn,
            source_task_id=source,
            gate_family="delivery-health",
            candidate_text="SERVICE-GATE delivery-health scan",
        )

    assert decision["create_escalation"] is False
    assert decision["decision"] == "dedupe_to_active_lane"
    assert decision["active_lane"]["task_id"] == lane
    assert "SERVICE-GATE-DEDUPE" in decision["pointer_comment"]
    assert f"active_lane={lane}" in decision["pointer_comment"]


def test_service_gate_create_task_writes_pointer_comment_without_duplicate(kanban_home):
    with kb.connect() as conn:
        source = kb.create_task(conn, title="Discord delivery blocked", assignee="pm")
        lane = kb.create_task(
            conn,
            title="SERVICE-GATE delivery-health repair",
            body=f"source={source} delivery-health config-owner repair lane",
            assignee="infra-optimizer",
        )

        duplicate = kb.create_task(
            conn,
            title="SERVICE-GATE delivery-health repair retry",
            body=f"source={source} delivery-health duplicate scan",
            assignee="infra-optimizer",
            created_by="jarvis-os-pm",
        )
        comments = kb.list_comments(conn, source)
        service_gate_rows = [
            task
            for task in kb.list_tasks(conn, include_archived=True)
            if (task.title or "").startswith("SERVICE-GATE delivery-health")
        ]

    assert duplicate == lane
    assert len(service_gate_rows) == 1
    assert any(
        comment.author == "jarvis-os-pm"
        and "SERVICE-GATE-DEDUPE" in comment.body
        and f"active_lane={lane}" in comment.body
        and "no_duplicate_escalation=true" in comment.body
        for comment in comments
    )


def test_service_gate_dedupe_keeps_true_critical_approval_packet(kanban_home):
    with kb.connect() as conn:
        source = kb.create_task(
            conn,
            title="Need credential approval",
            body="credentials/secrets rotation requires Frank approval",
            assignee="pm",
        )
        first = kb.service_gate_dedupe_decision(
            conn,
            source_task_id=source,
            gate_family="config-owner",
            candidate_text="SERVICE-GATE config-owner credentials blocker",
        )
        approval = kb.create_task(
            conn,
            title="SERVICE-GATE config-owner approval packet",
            body=f"source={source} config-owner credentials/secrets approval packet",
            assignee="jarvis-os-pm",
            initial_status="blocked",
        )
        second = kb.service_gate_dedupe_decision(
            conn,
            source_task_id=source,
            gate_family="config-owner",
            candidate_text="SERVICE-GATE config-owner credentials blocker",
        )

    assert first["create_escalation"] is True
    assert first["decision"] == "create_approval_packet"
    assert first["critical_list_blocker"] is True
    assert second["create_escalation"] is False
    assert second["decision"] == "hold_for_existing_approval_packet"
    assert second["active_lane"]["task_id"] == approval


def test_service_gate_create_task_preserves_one_true_critical_approval_packet(kanban_home):
    with kb.connect() as conn:
        source = kb.create_task(
            conn,
            title="Need credential approval",
            body="credentials/secrets rotation requires Frank approval",
            assignee="pm",
        )

        approval = kb.create_task(
            conn,
            title="SERVICE-GATE config-owner approval packet",
            body=f"source={source} config-owner credentials/secrets approval packet",
            assignee="jarvis-os-pm",
            initial_status="blocked",
        )
        duplicate = kb.create_task(
            conn,
            title="SERVICE-GATE config-owner approval packet retry",
            body=f"source={source} config-owner credentials/secrets approval packet duplicate",
            assignee="jarvis-os-pm",
            initial_status="blocked",
        )
        comments = kb.list_comments(conn, source)
        approval_rows = [
            task
            for task in kb.list_tasks(conn, include_archived=True)
            if "config-owner approval packet" in (task.title or "")
        ]

    assert duplicate == approval
    assert len(approval_rows) == 1
    assert any(
        "SERVICE-GATE-DEDUPE" in comment.body
        and f"active_lane={approval}" in comment.body
        and "no_duplicate_escalation=true" in comment.body
        for comment in comments
    )


def test_service_gate_dedupe_treats_workforce_scaler_activation_as_critical(kanban_home):
    with kb.connect() as conn:
        source = kb.create_task(
            conn,
            title="Runtime hold requires operator packet",
            body="Generic source body without critical-list wording.",
            assignee="pm",
        )

        decision = kb.service_gate_dedupe_decision(
            conn,
            source_task_id=source,
            gate_family="runtime-hold",
            candidate_text="SERVICE-GATE runtime-hold workforce-scaler dynamic-spawning activation",
        )
        ordinary = kb.service_gate_dedupe_decision(
            conn,
            source_task_id=source,
            gate_family="runtime-hold",
            candidate_text="SERVICE-GATE runtime-hold ordinary retry lane",
        )

    assert decision["create_escalation"] is True
    assert decision["decision"] == "create_approval_packet"
    assert decision["critical_list_blocker"] is True
    assert ordinary["decision"] == "create_triage_or_service_gate_lane"
    assert ordinary["critical_list_blocker"] is False


def test_service_gate_dedupe_treats_guardrail_weakening_as_critical(kanban_home):
    with kb.connect() as conn:
        source = kb.create_task(
            conn,
            title="Guardrail boundary repair",
            body="Generic source body without critical-list wording.",
            assignee="pm",
        )

        weakening = kb.service_gate_dedupe_decision(
            conn,
            source_task_id=source,
            gate_family="runtime-hold",
            candidate_text="SERVICE-GATE runtime-hold guardrail weakening request",
        )
        disablement = kb.service_gate_dedupe_decision(
            conn,
            source_task_id=source,
            gate_family="runtime-hold",
            candidate_text="SERVICE-GATE runtime-hold disable guardrail request",
        )

    assert weakening["create_escalation"] is True
    assert weakening["decision"] == "create_approval_packet"
    assert weakening["critical_list_blocker"] is True
    assert disablement["decision"] == "create_approval_packet"
    assert disablement["critical_list_blocker"] is True


def test_phase2f_inverted_reviewer_child_warning_does_not_weaken_parent_claim_gate(kanban_home):
    """Phase 2F warning is advisory: unfinished parents still block claims."""
    with kb.connect() as conn:
        source = kb.create_task(conn, title="landing handoff", assignee="worker")
        kb.block_task(conn, source, reason="review-required: os-reviewer must approve")
        review = kb.create_task(
            conn,
            title="REVIEW: landing handoff",
            body=f"Review source {source} and post REVIEW_VERDICT=APPROVE.",
            assignee="os-reviewer",
            parents=[source],
        )
        # Simulate a stale/dry-run writer accidentally marking the child ready.
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (review,))
        conn.commit()

        warning = kb.review_lane_dependency_warning(conn, review)
        claimed = kb.claim_task(conn, review, claimer="test-reviewer")
        review_after = kb.get_task(conn, review)
        events = kb.list_events(conn, review)

    assert warning is not None
    assert warning["source_task_id"] == source
    assert claimed is None
    assert review_after is not None
    assert review_after.status == "todo"
    assert any(
        ev.kind == "claim_rejected" and ev.payload == {"reason": "parents_not_done"}
        for ev in events
    )


def test_phase2f_independent_reviewer_lane_is_claimable_without_parent_warning(kanban_home):
    """Reviewer lanes linked by source text/comment stay independent and runnable."""
    with kb.connect() as conn:
        source = kb.create_task(conn, title="feature implementation", assignee="worker")
        kb.block_task(conn, source, reason="review-required: guardian review before closure")
        review = kb.create_task(
            conn,
            title="REVIEW: feature implementation",
            body=f"Independent review lane for source {source}; post REVIEW_VERDICT.",
            assignee="guardian-reviewer",
        )
        kb.add_comment(conn, review, "pm", f"source={source} review-required packet")

        warning = kb.review_lane_dependency_warning(conn, review)
        claimed = kb.claim_task(conn, review, claimer="test-reviewer")
        review_after = kb.get_task(conn, review)

    assert warning is None
    assert claimed is not None
    assert claimed.id == review
    assert review_after is not None
    assert review_after.status == "running"


def test_phase2f_service_gate_dedupe_requires_same_source_and_gate_family(kanban_home):
    """Active lanes suppress duplicates only for the exact source + gate family."""
    with kb.connect() as conn:
        source = kb.create_task(conn, title="Gateway alert", assignee="pm")
        other_source = kb.create_task(conn, title="Other alert", assignee="pm")
        same_source_wrong_family = kb.create_task(
            conn,
            title="SERVICE-GATE billing repair",
            body=f"source={source} billing repair lane",
            assignee="infra-optimizer",
        )
        same_family_wrong_source = kb.create_task(
            conn,
            title="SERVICE-GATE delivery-health repair",
            body=f"source={other_source} delivery-health repair lane",
            assignee="infra-optimizer",
        )
        matching_lane = kb.create_task(
            conn,
            title="SERVICE-GATE delivery-health repair",
            body=f"source={source} delivery-health repair lane",
            assignee="infra-optimizer",
        )

        no_match = kb.find_active_service_gate_lane(
            conn,
            source_task_id=source,
            gate_family="runtime-owner",
        )
        match = kb.find_active_service_gate_lane(
            conn,
            source_task_id=source,
            gate_family="delivery-health",
        )

    assert no_match is None
    assert match is not None
    assert match["task_id"] == matching_lane
    assert match["task_id"] not in {same_source_wrong_family, same_family_wrong_source}


def test_phase2f_service_gate_dedupe_does_not_match_shared_runtime_token(kanban_home):
    """Same source still needs the same gate family; shared tokens are too broad."""
    with kb.connect() as conn:
        source = kb.create_task(conn, title="Runtime alert", assignee="pm")
        runtime_hold_lane = kb.create_task(
            conn,
            title="SERVICE-GATE runtime-hold repair",
            body=f"source={source} runtime-hold repair lane",
            assignee="infra-optimizer",
        )

        wrong_family = kb.service_gate_dedupe_decision(
            conn,
            source_task_id=source,
            gate_family="runtime-owner",
            candidate_text="SERVICE-GATE runtime-owner scan",
        )
        same_family = kb.service_gate_dedupe_decision(
            conn,
            source_task_id=source,
            gate_family="runtime-hold",
            candidate_text="SERVICE-GATE runtime-hold scan",
        )

    assert wrong_family["create_escalation"] is True
    assert wrong_family["decision"] == "create_triage_or_service_gate_lane"
    assert wrong_family["active_lane"] is None
    assert same_family["create_escalation"] is False
    assert same_family["decision"] == "dedupe_to_active_lane"
    assert same_family["active_lane"]["task_id"] == runtime_hold_lane


def test_phase2f_service_gate_dedupe_does_not_match_gate_family_prefix_or_suffix(kanban_home):
    """Exact family matching rejects longer families that only share a token prefix."""
    with kb.connect() as conn:
        source = kb.create_task(conn, title="Runtime owner alert", assignee="pm")
        suffix_lane = kb.create_task(
            conn,
            title="SERVICE-GATE runtime-owner-extra repair",
            body=f"source={source} runtime-owner-extra repair lane",
            assignee="infra-optimizer",
        )
        underscore_suffix_lane = kb.create_task(
            conn,
            title="SERVICE-GATE runtime_owner_extra repair",
            body=f"source={source} runtime_owner_extra repair lane",
            assignee="infra-optimizer",
        )

        suffix_candidate = kb.service_gate_dedupe_decision(
            conn,
            source_task_id=source,
            gate_family="runtime-owner",
            candidate_text="SERVICE-GATE runtime-owner scan",
        )
        exact_lane = kb.create_task(
            conn,
            title="SERVICE-GATE runtime_owner repair",
            body=f"source={source} runtime_owner repair lane",
            assignee="infra-optimizer",
        )
        exact_candidate = kb.service_gate_dedupe_decision(
            conn,
            source_task_id=source,
            gate_family="runtime-owner",
            candidate_text="SERVICE-GATE runtime-owner scan",
        )

    assert suffix_candidate["create_escalation"] is True
    assert suffix_candidate["decision"] == "create_triage_or_service_gate_lane"
    assert suffix_candidate["active_lane"] is None
    assert exact_candidate["create_escalation"] is False
    assert exact_candidate["decision"] == "dedupe_to_active_lane"
    assert exact_candidate["active_lane"]["task_id"] == exact_lane
    assert exact_candidate["active_lane"]["task_id"] not in {suffix_lane, underscore_suffix_lane}


def test_phase2f_service_gate_dedupe_does_not_bypass_completion_created_cards_or_running_app_gates(kanban_home):
    """De-dupe is advisory only; existing completion and evidence gates still apply."""
    with kb.connect() as conn:
        source = kb.create_task(
            conn,
            title="Build frontend dashboard route",
            body="Implement apps/web dashboard route component. Running-app VERIFY_PASS is required.",
            assignee="web-worker",
        )
        lane = kb.create_task(
            conn,
            title="SERVICE-GATE delivery-health repair",
            body=f"source={source} delivery-health diagnostics lane",
            assignee="infra-optimizer",
        )
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (source,))
        conn.commit()

        decision = kb.service_gate_dedupe_decision(
            conn,
            source_task_id=source,
            gate_family="delivery-health",
            candidate_text="SERVICE-GATE delivery-health duplicate scan",
        )
        not_completed_from_todo = kb.complete_task(conn, source, summary="deduped but not claimed")
        task_after_todo_completion = kb.get_task(conn, source)

        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (source,))
        conn.commit()
        claimed = kb.claim_task(conn, source, claimer="web-worker")
        assert claimed is not None
        wrong_run_id = int(claimed.current_run_id or 0) + 1
        not_completed_with_wrong_run = kb.complete_task(
            conn,
            source,
            summary="deduped with stale completion run id",
            expected_run_id=wrong_run_id,
        )
        task_after_wrong_run = kb.get_task(conn, source)

        with pytest.raises(kb.HallucinatedCardsError) as exc_info:
            kb.complete_task(
                conn,
                source,
                summary="deduped but claimed phantom child",
                created_cards=["t_deadbeefcafe"],
            )
        task_after_phantom = kb.get_task(conn, source)
        events = kb.list_events(conn, source)

    assert decision["create_escalation"] is False
    assert decision["decision"] == "dedupe_to_active_lane"
    assert decision["active_lane"]["task_id"] == lane
    # The generated pointer is not running-app evidence; the external completion
    # hook accepts VERIFY_PASS only from current completion input or explicit
    # RUNNING_APP_VERIFICATION comments.
    assert "SERVICE-GATE-DEDUPE" in decision["pointer_comment"]
    assert "VERIFY_PASS" not in decision["pointer_comment"]
    assert "RUNNING_APP_VERIFICATION" not in decision["pointer_comment"]
    assert not_completed_from_todo is False
    assert task_after_todo_completion is not None
    assert task_after_todo_completion.status == "todo"
    assert not_completed_with_wrong_run is False
    assert task_after_wrong_run is not None
    assert task_after_wrong_run.status == "running"
    assert exc_info.value.phantom == ["t_deadbeefcafe"]
    assert task_after_phantom is not None
    assert task_after_phantom.status == "running"
    assert any(ev.kind == "completion_blocked_hallucination" for ev in events)


def test_phase2f_true_critical_source_requires_explicit_approval_packet_not_generic_lane(kanban_home):
    """Critical-list blockers surface one approval packet instead of hiding behind repair lanes."""
    with kb.connect() as conn:
        source = kb.create_task(
            conn,
            title="Enable gateway runtime activation",
            body="unapproved gateway/runtime activation requires operator approval",
            assignee="pm",
        )
        repair_lane = kb.create_task(
            conn,
            title="SERVICE-GATE runtime-owner repair lane",
            body=f"source={source} runtime-owner diagnostics repair lane",
            assignee="infra-optimizer",
        )

        first = kb.service_gate_dedupe_decision(
            conn,
            source_task_id=source,
            gate_family="runtime-owner",
            candidate_text="SERVICE-GATE runtime-owner gateway restart blocker",
        )
        approval = kb.create_task(
            conn,
            title="SERVICE-GATE runtime-owner approval packet",
            body=f"source={source} runtime-owner gateway restart approval packet",
            assignee="jarvis-os-pm",
            initial_status="blocked",
        )
        second = kb.service_gate_dedupe_decision(
            conn,
            source_task_id=source,
            gate_family="runtime-owner",
            candidate_text="SERVICE-GATE runtime-owner gateway restart blocker",
        )

    assert first["create_escalation"] is True
    assert first["decision"] == "create_approval_packet"
    assert first["critical_list_blocker"] is True
    assert first["active_lane"] is None
    assert repair_lane != approval
    assert second["create_escalation"] is False
    assert second["decision"] == "hold_for_existing_approval_packet"
    assert second["active_lane"]["task_id"] == approval


def test_phase2f_dedupe_does_not_bypass_completion_run_gate(kanban_home):
    """De-duping an active running-app lane must not let stale completions land."""
    with kb.connect() as conn:
        source = kb.create_task(conn, title="Route changed", assignee="web-worker")
        claimed = kb.claim_task(conn, source, claimer="web-worker")
        assert claimed is not None
        lane = kb.create_task(
            conn,
            title="SERVICE-GATE running-app verification",
            body=f"source={source} running-app VERIFY_FAIL route probe lane",
            assignee="test-engineer",
        )

        decision = kb.service_gate_dedupe_decision(
            conn,
            source_task_id=source,
            gate_family="running-app",
            candidate_text="SERVICE-GATE running-app verify-running-app probe failed",
        )
        completed = kb.complete_task(
            conn,
            source,
            summary="stale completion after duplicate running-app lane",
            expected_run_id=(claimed.current_run_id or 0) + 1,
        )
        task_after = kb.get_task(conn, source)
        events = kb.list_events(conn, source)

    assert decision["create_escalation"] is False
    assert decision["decision"] == "dedupe_to_active_lane"
    assert decision["active_lane"]["task_id"] == lane
    assert "VERIFY_PASS" not in (decision["pointer_comment"] or "")
    assert completed is False
    assert task_after is not None
    assert task_after.status == "running"
    assert all(ev.kind != "completed" for ev in events)


def test_phase2f_dedupe_does_not_bypass_created_cards_gate(kanban_home):
    """Duplicate service-gate suppression cannot weaken created_cards validation."""
    phantom_child = "t_deadbeefcafe"
    with kb.connect() as conn:
        source = kb.create_task(conn, title="Review-required source", assignee="worker")
        kb.claim_task(conn, source, claimer="worker")
        lane = kb.create_task(
            conn,
            title="SERVICE-GATE completion-contract review",
            body=f"source={source} completion-contract review lane",
            assignee="guardian-reviewer",
        )

        decision = kb.service_gate_dedupe_decision(
            conn,
            source_task_id=source,
            gate_family="completion-contract",
            candidate_text="SERVICE-GATE completion-contract duplicate scan",
        )
        with pytest.raises(kb.HallucinatedCardsError) as exc_info:
            kb.complete_task(
                conn,
                source,
                summary=f"claimed duplicate child {phantom_child}",
                created_cards=[phantom_child],
            )
        task_after = kb.get_task(conn, source)
        events = kb.list_events(conn, source)

    assert decision["create_escalation"] is False
    assert decision["decision"] == "dedupe_to_active_lane"
    assert decision["active_lane"]["task_id"] == lane
    assert exc_info.value.phantom == [phantom_child]
    assert task_after is not None
    assert task_after.status == "running"


# ---------------------------------------------------------------------------
# Regression: triage-lifecycle dead end + false "unknown id or terminal state"
# ---------------------------------------------------------------------------

class TestTriageLifecycleRegression:
    """Cover the three defects named in the task body.

    D1 — ``complete_task`` must be able to close a ``triage``-parked card
         whose work is done and approved.
    D2 — the CLI failure message must name the REAL status, never the
         factually false "unknown id or terminal state".
    """

    def test_complete_task_accepts_triage_status(self, kanban_home):
        """RED->GREEN: a triage-stranded, work-complete card closes as done."""
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="triage-stranded", assignee="os-architect")
            # Simulate the loop-breaker parking the card in triage.
            conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (tid,))
            ok = kb.complete_task(conn, tid, result="approved work done")
            assert ok is True
            t = kb.get_task(conn, tid)
            assert t.status == "done"

    def test_complete_task_rejects_unknown_id(self, kanban_home):
        """An id that truly does not exist still returns False."""
        with kb.connect() as conn:
            assert kb.complete_task(conn, "does-not-exist") is False

    def test_cli_message_names_real_status(self, kanban_home, monkeypatch, capsys):
        """_cmd_complete must report the actual status, not a false diagnosis."""
        from hermes_cli import kanban as kc
        tid = kb.create_task(kb.connect(), title="triage-stranded-cli", assignee="os-architect")
        with kb.connect() as conn:
            # A status OUTSIDE the accepted gate set -> complete_task fails,
            # so the CLI surfaces the REAL status instead of the false string.
            conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (tid,))
        ns = argparse.Namespace(task_ids=[tid], summary=None, metadata=None, result="ok")
        try:
            kc._cmd_complete(ns)
        except SystemExit:
            pass
        err = capsys.readouterr().err
        assert "triage" in err.lower() or "cannot complete" in err.lower()
        assert "unknown id or terminal state" not in err

    def test_promote_task_accepts_triage_to_todo(self, kanban_home):
        """D1 candidate (b): a triage-parked card routes back to the intake
        lane (`todo`), never directly to `ready`."""
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="triage-promote", assignee="os-architect")
            conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (tid,))
            ok, err = kb.promote_task(conn, tid, actor="ops")
            assert ok is True, err
            t = kb.get_task(conn, tid)
            assert t.status == "todo"
            kinds = [e.kind for e in kb.list_events(conn, tid)]
            assert kinds.count("promoted_manual") == 1

    def test_promote_task_triage_skips_parent_gate(self, kanban_home):
        """Routing triage -> todo must NOT be refused by an unsatisfied
        parent: `todo` is the dependency-waiting lane and recompute_ready
        gates the later todo -> ready hop."""
        with kb.connect() as conn:
            parent = kb.create_task(conn, title="parent", assignee="a")
            child = kb.create_task(
                conn, title="child", assignee="b", parents=[parent],
            )
            # Child lands in `todo` waiting on parent; park it in triage.
            conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (child,))
            ok, err = kb.promote_task(conn, child, actor="ops")
            assert ok is True, err
            assert kb.get_task(conn, child).status == "todo"

    def test_promote_task_rejects_other_statuses(self, kanban_home):
        """Statuses outside todo/blocked/triage still refuse with the real
        status named — never a 'unknown id or terminal state' style guess."""
        with kb.connect() as conn:
            for st in ("done", "archived", "running"):
                tid = kb.create_task(conn, title=f"st-{st}", assignee="a")
                conn.execute(
                    "UPDATE tasks SET status = ? WHERE id = ?", (st, tid)
                )
                ok, err = kb.promote_task(conn, tid, actor="ops")
                assert ok is False
                assert st in err

    def test_cli_promote_triage_reports_todo_target(self, kanban_home, monkeypatch, capsys):
        """_cmd_promote must print the ACTUAL target (`todo`) for a triage
        card, not a hard-coded `ready`."""
        from hermes_cli import kanban as kc
        tid = kb.create_task(kb.connect(), title="triage-promote-cli", assignee="os-architect")
        with kb.connect() as conn:
            conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (tid,))
        ns = argparse.Namespace(
            task_id=tid, ids=None, reason=[], force=False,
            dry_run=False, json=False,
        )
        rc = kc._cmd_promote(ns)
        out = capsys.readouterr().out
        assert rc == 0
        assert "-> todo" in out
        assert "-> ready" not in out

    def test_cli_unblock_names_real_status(self, kanban_home, monkeypatch, capsys):
        """_cmd_unblock must report the ACTUAL status when it refuses, not
        the factually thin '(not blocked/scheduled?)' guess."""
        from hermes_cli import kanban as kc
        tid = kb.create_task(kb.connect(), title="triage-unblock-cli", assignee="os-architect")
        with kb.connect() as conn:
            conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (tid,))
        ns = argparse.Namespace(task_ids=[tid], reason=None)
        rc = kc._cmd_unblock(ns)
        err = capsys.readouterr().err
        assert rc == 1
        assert "current status is 'triage'" in err


# ---------------------------------------------------------------------------
# Respawn guard author-restriction regression tests (t_0536fe58 / t_ed7ed09c)
# These verify the composed guard (t_ac710e3f) preserves BOTH halves:
# the own-worker author restriction AND the PR-state resolution.
# ---------------------------------------------------------------------------

def test_respawn_guard_active_pr_defers_own_worker_pr_comment(kanban_home):
    """A GitHub PR URL in a recent comment triggers active_pr ONLY when the
    comment author is a profile that has actually run this task (the prior
    worker opened the PR). Dedupe behaviour is preserved for cards whose own
    worker opened a PR.
    """
    with kb.connect() as conn:
        t = kb.create_task(conn, title="has-pr", assignee="alice")
        # Simulate a prior worker run under the assignee profile, then a
        # PR reference authored by that same worker. The completed run is
        # OLDER than the success window (3600s) but inside the PR window
        # (86400s), so the guard reaches the active_pr check instead of
        # short-circuiting on recent_success.
        kb.claim_task(conn, t)
        run_id = kb.get_task(conn, t).current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='completed', status='completed', "
            "ended_at=? WHERE id=?",
            (int(time.time()) - 7200, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, "
            "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL WHERE id=?",
            (t,),
        )
        kb.add_comment(
            conn, t, "alice",
            "PR created: https://github.com/totemx-AI/subsidysmart/pull/42",
        )
        with unittest.mock.patch.object(kb, "_github_pr_state", return_value="OPEN"):
            reason = kb.check_respawn_guard(conn, t)
    assert reason == "active_pr"


def test_respawn_guard_active_pr_ignores_third_party_pr_comment(kanban_home):
    """REGRESSION (t_24c405ba / t_439547d4): a PR URL in a comment authored
    by a THIRD PARTY (e.g. a reviewer lane card referencing the PR it
    reviews) must NOT trigger active_pr when this task never spawned — there
    is no prior worker to dedupe against, so the card must be dispatched.
    """
    with kb.connect() as conn:
        t = kb.create_task(conn, title="review-pr", assignee="alice")
        kb.add_comment(
            conn, t, "fable-reviewer",
            "Independent review: https://github.com/totemx-AI/subsidysmart/pull/42",
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason is None


def test_respawn_guard_active_pr_ignores_third_party_comment_even_with_prior_run(
    kanban_home,
):
    """A PR URL in a THIRD-PARTY comment must not defer even when the task
    HAS a prior run: the run belongs to a different profile, so the PR was
    not opened by this task's own worker (reviewer lane pattern).
    """
    with kb.connect() as conn:
        t = kb.create_task(conn, title="review-pr-2", assignee="alice")
        kb.claim_task(conn, t)
        run_id = kb.get_task(conn, t).current_run_id
        conn.execute(
            "UPDATE task_runs SET outcome='blocked', status='blocked', "
            "ended_at=? WHERE id=?",
            (int(time.time()) - 60, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, "
            "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL WHERE id=?",
            (t,),
        )
        kb.add_comment(
            conn, t, "fable-reviewer",
            "reviewed https://github.com/totemx-AI/subsidysmart/pull/42",
        )
        reason = kb.check_respawn_guard(conn, t)
    assert reason is None


def test_dispatch_spawns_ready_card_with_third_party_pr_comment(
    kanban_home, all_assignees_spawnable
):
    """REGRESSION (t_24c405ba / t_439547d4): a ready card with a real
    profile assignee and a PR-url comment from a third party, with no prior
    run, IS spawned by dispatch_once and does NOT appear in respawn_guarded.
    """
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kb.connect() as conn:
        t = kb.create_task(conn, title="reviewer-lane", assignee="alice")
        kb.add_comment(
            conn, t, "fable-reviewer",
            "Independent review: https://github.com/totemx-AI/subsidysmart/pull/42",
        )
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    assert t in spawned_ids, (
        f"never-spawned card with third-party PR comment must spawn; "
        f"spawned={spawned_ids!r}"
    )
    assert (t, "active_pr") not in res.respawn_guarded, (
        f"third-party PR comment must not respawn-guard a never-spawned "
        f"card; respawn_guarded={res.respawn_guarded!r}"
    )


def test_dispatch_respawn_guard_skips_own_worker_active_pr(
    kanban_home, all_assignees_spawnable
):
    """dispatch_once still skips (but does not block) a card whose own
    worker previously opened an OPEN PR (composed guard: own-worker author +
    OPEN state). The PR state is mocked OPEN so the test isolates the
    composed guard's behaviour from live GitHub resolution.
    """
    spawned_ids = []

    def fake_spawn(task, workspace):
        spawned_ids.append(task.id)

    with kb.connect() as conn:
        t = kb.create_task(conn, title="has-pr", assignee="alice")
        kb.claim_task(conn, t)
        run_id = kb.get_task(conn, t).current_run_id
        # Completed run older than the success window (3600s) but inside
        # the PR window (86400s) so the guard reaches active_pr instead of
        # short-circuiting on recent_success.
        conn.execute(
            "UPDATE task_runs SET outcome='completed', status='completed', "
            "ended_at=? WHERE id=?",
            (int(time.time()) - 7200, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, "
            "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL WHERE id=?",
            (t,),
        )
        kb.add_comment(
            conn, t, "alice",
            "Opened https://github.com/totemx-AI/subsidysmart/pull/99",
        )
        with unittest.mock.patch.object(kb, "_github_pr_state", return_value="OPEN"):
            res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    assert (t, "active_pr") in res.respawn_guarded
    assert t not in spawned_ids
    assert t not in res.auto_blocked
    with kb.connect() as conn:
        assert kb.get_task(conn, t).status == "ready"
