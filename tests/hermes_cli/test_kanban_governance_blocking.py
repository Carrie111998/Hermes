"""Tests for kanban governance blocking contracts.

Verifies that blocked cards enforce typed blocker classes and governance rules.
"""

from __future__ import annotations

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


def test_block_without_blocker_class_succeeds_for_backwards_compat(kanban_home):
    """A block without blocker_class is accepted for backwards compatibility."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="default")
        kb.claim_task(conn, tid)
        # This should succeed for backwards compatibility
        result = kb.block_task(
            conn,
            tid,
            reason="router uncertain",
            blocker_class=None,
        )
        ok = result if not isinstance(result, tuple) else result[0]
        assert ok is True
        # Verify the task is blocked
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"


def test_operational_hold_on_christopher_is_rejected_without_human_decision(kanban_home):
    """A block mentioning Christopher requires human_decision classification."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="default")
        kb.claim_task(conn, tid)
        # This should fail because it mentions Christopher but has no human_decision
        result = kb.block_task(
            conn,
            tid,
            reason="awaiting Christopher to decide next operational step",
            blocker_class="infra_default",
            with_reason=True,
        )
        ok = result[0] if isinstance(result, tuple) else result
        assert ok is False
        # Verify the error message mentions human_decision
        if isinstance(result, tuple):
            reason = result[1]
            assert "human_decision" in reason


def test_block_with_valid_blocker_class_succeeds(kanban_home):
    """A block with a valid blocker_class is accepted."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="default")
        kb.claim_task(conn, tid)
        # This should succeed
        result = kb.block_task(
            conn,
            tid,
            reason="waiting on dependency",
            blocker_class="dependency_wait",
        )
        ok = result if not isinstance(result, tuple) else result[0]
        assert ok is True
        # Verify the task is blocked
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"


def test_block_christopher_hold_with_human_decision_succeeds(kanban_home):
    """A block mentioning Christopher with human_decision classification is accepted."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="default")
        kb.claim_task(conn, tid)
        # This should succeed with human_decision class
        result = kb.block_task(
            conn,
            tid,
            reason="awaiting Christopher to decide next operational step",
            blocker_class="human_decision",
            decision_class="scope_authorization",
        )
        ok = result if not isinstance(result, tuple) else result[0]
        assert ok is True
        # Verify the task is blocked
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
