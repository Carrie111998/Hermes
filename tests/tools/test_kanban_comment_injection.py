"""Live operator-note injection into a running kanban worker.

``tools.kanban_tools.inject_new_comments_from_env`` polls the worker's task
for comments added *after* the run started and folds them into the live turn
via the agent's OUT-OF-BAND steer channel — so a user can talk to a running
task without the block→comment→unblock dance or a restart.

Verifies: no-op off a worker, watermark seeding (history isn't re-injected),
new comments steer, own-authored comments are skipped, and the dispatcher's
run-start baseline (``HERMES_KANBAN_COMMENT_BASELINE``) closes the
spawn→first-poll swallow window.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb
import tools.kanban_tools as kt


class FakeAgent:
    def __init__(self):
        self.steers: list[str] = []

    def steer(self, text: str) -> bool:
        self.steers.append(text)
        return True


@pytest.fixture
def worker_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_HOME", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_COMMENT_BASELINE"):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    # Reset module-level poll state so tests don't leak into each other.
    kt._comment_watermark.clear()
    kt._comment_poll_last_attempt = 0.0
    return home


def _unthrottle():
    """Bypass the inter-poll rate limit for deterministic tests."""
    kt._comment_poll_last_attempt = 0.0


def test_noop_without_worker_env(worker_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    agent = FakeAgent()
    assert kt.inject_new_comments_from_env(agent) is False
    assert agent.steers == []


def test_seed_then_inject_new_comment(worker_home, monkeypatch):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="live task")
        kb.add_comment(conn, tid, author="desktop", body="pre-existing note")
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "worker-bot")
    agent = FakeAgent()

    # First poll seeds the watermark past the existing thread — no injection.
    _unthrottle()
    assert kt.inject_new_comments_from_env(agent) is False
    assert agent.steers == []

    conn = kb.connect()
    try:
        kb.add_comment(conn, tid, author="desktop", body="actually use the v2 API")
    finally:
        conn.close()

    _unthrottle()
    assert kt.inject_new_comments_from_env(agent) is True
    assert len(agent.steers) == 1
    assert "v2 API" in agent.steers[0]

    # Watermark advanced — a re-poll with no new comments injects nothing.
    _unthrottle()
    assert kt.inject_new_comments_from_env(agent) is False
    assert len(agent.steers) == 1


def test_skips_own_authored_comments(worker_home, monkeypatch):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="echo guard")
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "worker-bot")
    agent = FakeAgent()

    _unthrottle()
    kt.inject_new_comments_from_env(agent)  # seed

    conn = kb.connect()
    try:
        kb.add_comment(conn, tid, author="worker-bot", body="i did a thing")
    finally:
        conn.close()

    _unthrottle()
    assert kt.inject_new_comments_from_env(agent) is False
    assert agent.steers == []


def test_baseline_injects_comment_added_after_spawn_on_first_poll(worker_home, monkeypatch):
    """With a dispatcher-pinned run-start baseline, a comment that lands
    between spawn/context-build and the FIRST poll is injected, not swallowed."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="baseline live task")
        kb.add_comment(conn, tid, author="desktop", body="pre-spawn note 1")
        kb.add_comment(conn, tid, author="desktop", body="pre-spawn note 2")
        baseline = kb.list_comments(conn, tid)[-1].id
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "worker-bot")
    # Dispatcher pin: run-start baseline = newest comment id at spawn.
    monkeypatch.setenv("HERMES_KANBAN_COMMENT_BASELINE", str(baseline))
    agent = FakeAgent()

    # Comment lands in the spawn -> first-poll window (after the baseline).
    conn = kb.connect()
    try:
        kb.add_comment(conn, tid, author="desktop", body="spawn->poll window note")
    finally:
        conn.close()

    # FIRST poll injects it — no swallow.
    _unthrottle()
    assert kt.inject_new_comments_from_env(agent) is True
    assert len(agent.steers) == 1
    assert "spawn->poll window note" in agent.steers[0]


def test_baseline_does_not_inject_comments_at_or_below_baseline(worker_home, monkeypatch):
    """The pre-spawn thread (id <= baseline) is already in the worker's
    context, so a first poll with nothing past the baseline injects nothing."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="baseline floor task")
        kb.add_comment(conn, tid, author="desktop", body="old note 1")
        baseline = kb.add_comment(conn, tid, author="desktop", body="old note 2")
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "worker-bot")
    monkeypatch.setenv("HERMES_KANBAN_COMMENT_BASELINE", str(baseline))
    agent = FakeAgent()

    # First poll: everything is at/below the baseline -> nothing injected,
    # including the comment whose id exactly equals the baseline.
    _unthrottle()
    assert kt.inject_new_comments_from_env(agent) is False
    assert agent.steers == []
