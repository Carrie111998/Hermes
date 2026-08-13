"""The kanban probe behind the daily session-reset guard.

``GatewayRunner._has_active_kanban_for_session`` is the callable wired into
``SessionStore(has_active_kanban_fn=...)``. It answers "is this session's
profile mid-work on the board?" and its answer holds the daily reset open.

Two contracts matter here and both are exercised against a REAL kanban schema
(``init_db``), not a hand-rolled table — the query names real columns and would
silently degrade to "not busy" if either drifted:

1. Only ``running``/``blocked`` rows for *this profile's* assignee count.
2. Every lookup failure — missing board, unreadable file — resolves to False.
   A reset guard that fails closed on a missing DB would pin every session
   open forever on installs that never use kanban.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from gateway.run import GatewayRunner


class _Runner:
    """Minimal stand-in — the probe only needs the profile resolver."""

    def __init__(self, profile: str = "default"):
        self._profile = profile

    def _active_profile_name(self) -> str:
        return self._profile

    _profile_for_session_key = GatewayRunner._profile_for_session_key
    _has_active_kanban_for_session = GatewayRunner._has_active_kanban_for_session


@pytest.fixture
def board(tmp_path, monkeypatch):
    """A real kanban.db, pinned via HERMES_KANBAN_DB, with an insert helper."""
    from hermes_cli.kanban_db import init_db

    db_path = tmp_path / "kanban.db"
    init_db(db_path)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))

    def add(task_id: str, status: str, assignee: str | None) -> None:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "INSERT INTO tasks (id, title, status, assignee, created_at, "
                "workspace_kind) VALUES (?, ?, ?, ?, ?, 'scratch')",
                (task_id, f"task {task_id}", status, assignee, int(time.time())),
            )
            conn.commit()
        finally:
            conn.close()

    return add


_KEY = "agent:main:telegram:dm:123"


# ---------------------------------------------------------------------------
# Profile resolution from the session key
# ---------------------------------------------------------------------------

class TestProfileForSessionKey:

    def test_main_namespace_resolves_to_the_active_profile(self):
        assert _Runner("coder")._profile_for_session_key(_KEY) == "coder"

    def test_named_namespace_wins_over_the_active_profile(self):
        runner = _Runner("coder")
        key = "agent:research:telegram:dm:123"
        assert runner._profile_for_session_key(key) == "research"

    def test_malformed_key_falls_back_to_the_active_profile(self):
        runner = _Runner("coder")
        assert runner._profile_for_session_key("") == "coder"
        assert runner._profile_for_session_key("garbage") == "coder"


# ---------------------------------------------------------------------------
# What counts as live kanban work
# ---------------------------------------------------------------------------

class TestActiveKanbanProbe:

    @pytest.mark.parametrize("status", ["running", "blocked"])
    def test_live_task_for_this_profile_is_busy(self, board, status):
        board("t1", status, "default")
        assert _Runner()._has_active_kanban_for_session(_KEY) is True

    @pytest.mark.parametrize("status", ["todo", "ready", "review", "done", "archived"])
    def test_other_statuses_are_not_busy(self, board, status):
        board("t1", status, "default")
        assert _Runner()._has_active_kanban_for_session(_KEY) is False

    def test_empty_board_is_not_busy(self, board):
        assert _Runner()._has_active_kanban_for_session(_KEY) is False

    def test_another_profiles_running_task_does_not_block(self, board):
        board("t1", "running", "research")
        assert _Runner("default")._has_active_kanban_for_session(_KEY) is False
        assert _Runner("research")._has_active_kanban_for_session(_KEY) is True

    def test_assignee_matching_is_case_insensitive(self, board):
        """Rows are stored canonicalised; the probe normalises to match."""
        board("t1", "running", "research")
        runner = _Runner("Research")
        assert runner._has_active_kanban_for_session(_KEY) is True

    def test_session_key_namespace_selects_the_profile(self, board):
        board("t1", "running", "research")
        runner = _Runner("default")
        key = "agent:research:telegram:dm:123"
        assert runner._has_active_kanban_for_session(key) is True


# ---------------------------------------------------------------------------
# Failure modes must never block the reset
# ---------------------------------------------------------------------------

class TestProbeFailsOpen:

    def test_missing_board_is_not_busy(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "nope.db"))
        assert _Runner()._has_active_kanban_for_session(_KEY) is False

    def test_unreadable_board_is_not_busy(self, tmp_path, monkeypatch):
        garbage = tmp_path / "kanban.db"
        garbage.write_bytes(b"not a sqlite file at all")
        monkeypatch.setenv("HERMES_KANBAN_DB", str(garbage))
        assert _Runner()._has_active_kanban_for_session(_KEY) is False

    def test_schemaless_board_is_not_busy(self, tmp_path, monkeypatch):
        empty = tmp_path / "kanban.db"
        sqlite3.connect(empty).close()  # valid sqlite, no tasks table
        monkeypatch.setenv("HERMES_KANBAN_DB", str(empty))
        assert _Runner()._has_active_kanban_for_session(_KEY) is False

    def test_profile_resolution_error_is_not_busy(self, board):
        class _Broken(_Runner):
            def _active_profile_name(self):
                raise RuntimeError("profiles unavailable")

        board("t1", "running", "default")
        assert _Broken()._has_active_kanban_for_session(_KEY) is False
