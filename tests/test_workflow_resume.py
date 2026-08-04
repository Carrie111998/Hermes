"""
Tests for workflow auto-resume hooks — _reopen_completed_run,
_get_board_conn, and the kanban_task_claimed re-entry.

Run: python3 -m pytest tests/test_workflow_resume.py -v
"""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


def _make_state(
    *,
    workflow_name="test-workflow",
    kanban_board="test-board",
    run_id="test-workflow-20260804-120000-123456",
    final_status: str | None = "completed",
    states=None,
):
    """Build a retained (completed) engine state dict."""
    if states is None:
        states = {
            "implement": {
                "node_id": "implement",
                "status": "done",
                "kanban_card_id": "t_implement",
                "completed_at": "2026-08-04T12:00:00+00:00",
            },
            "verify": {
                "node_id": "verify",
                "status": "done",
                "kanban_card_id": "t_verify",
                "completed_at": "2026-08-04T12:00:00+00:00",
            },
        }
    state = {
        "workflow_name": workflow_name,
        "kanban_board": kanban_board,
        "run_id": run_id,
        "current_layer": 0,
        "layers": [["implement"], ["verify"]],
        "states": states,
        "results": {"implement": "done", "verify": "done"},
        "session_info": {"platform": "discord", "chat_id": "123", "profile": "newton"},
    }
    if final_status:
        state["final_status"] = final_status
    return state


class TestGetBoardConn:
    """_get_board_conn returns (kanban_db, conn) or (kanban_db, None)."""

    def test_returns_conn_for_board(self):
        from plugins.workflow import _get_board_conn

        with patch("hermes_cli.kanban_db.connect") as mock_connect:
            mock_conn = MagicMock()
            mock_connect.return_value = mock_conn
            kb, conn = _get_board_conn({"kanban_board": "fleet-workflow"})
            mock_connect.assert_called_once_with(board="fleet-workflow")
            assert conn is mock_conn
            assert kb is not None

    def test_returns_none_without_board(self):
        from plugins.workflow import _get_board_conn

        with patch("hermes_cli.kanban_db.connect") as mock_connect:
            kb, conn = _get_board_conn({})
            mock_connect.assert_not_called()
            assert conn is None


class TestReopenCompletedRun:
    """_reopen_completed_run re-opens a finished run when a card re-activates."""

    def test_reopens_completed_run(self, tmp_path):
        from plugins.workflow import _reopen_completed_run

        state = _make_state()
        state_path = tmp_path / "test-workflow_test-workflow-20260804-120000-123456_state.json"
        state_path.write_text(json.dumps(state, default=str))

        with patch("plugins.workflow._find_state_for_card") as mock_find, \
             patch("plugins.workflow._mark_execution_running") as mock_mark, \
             patch("plugins.workflow._spawn_supervisor_for_resume") as mock_spawn:
            mock_find.return_value = (state, str(state_path))
            result = _reopen_completed_run("t_implement")

        assert result is True
        mock_mark.assert_called_once_with("test-workflow-20260804-120000-123456")
        mock_spawn.assert_called_once()

        # State file rewritten: final_status dropped, node reset, layer rewound
        rewritten = json.loads(state_path.read_text())
        assert "final_status" not in rewritten
        assert rewritten["states"]["implement"]["status"] == "ready"
        assert rewritten["states"]["implement"]["completed_at"] is None
        assert rewritten["current_layer"] == 0  # implement is in layer 0

    def test_resets_reviewer_cards(self, tmp_path):
        """Reviewer kanban_card_id is cleared so a fresh card gets created."""
        from plugins.workflow import _reopen_completed_run

        state = _make_state()
        state_path = tmp_path / "test-workflow_test-workflow-20260804-120000-123456_state.json"
        state_path.write_text(json.dumps(state, default=str))

        # Fake YAML declaring implement's reviewer
        wf_dir = tmp_path / "wf"
        wf_dir.mkdir()
        (wf_dir / "test-workflow.yaml").write_text(json.dumps({
            "name": "test-workflow",
            "nodes": {
                "implement": {"reviews": ["verify"]},
                "verify": {},
            },
        }))

        with patch("plugins.workflow._find_state_for_card") as mock_find, \
             patch("plugins.workflow._spawn_supervisor_for_resume") as mock_spawn, \
             patch("plugins.workflow.os.environ", {"HERMES_WORKFLOW_FILES": str(wf_dir)}):
            mock_find.return_value = (state, str(state_path))
            _reopen_completed_run("t_implement")

        rewritten = json.loads(state_path.read_text())
        assert rewritten["states"]["verify"]["kanban_card_id"] is None
        assert rewritten["states"]["verify"]["status"] == "pending"
        assert rewritten["states"]["verify"]["completed_at"] is None

    def test_active_run_not_reopened(self, tmp_path):
        """No final_status → active run, normal hook path handles it."""
        from plugins.workflow import _reopen_completed_run

        state = _make_state(final_status=None)
        with patch("plugins.workflow._find_state_for_card") as mock_find, \
             patch("plugins.workflow._spawn_supervisor_for_resume") as mock_spawn:
            mock_find.return_value = (state, "/fake/path")
            result = _reopen_completed_run("t_implement")

        assert result is False
        mock_spawn.assert_not_called()

    def test_unknown_card_not_reopened(self):
        from plugins.workflow import _reopen_completed_run

        with patch("plugins.workflow._find_state_for_card") as mock_find, \
             patch("plugins.workflow._spawn_supervisor_for_resume") as mock_spawn:
            mock_find.return_value = None
            result = _reopen_completed_run("t_nope")

        assert result is False
        mock_spawn.assert_not_called()


class TestClaimedHook:
    """kanban_task_claimed routes to _reopen_completed_run."""

    def test_claimed_hook_routes(self):
        from plugins.workflow import _on_kanban_task_claimed

        with patch("plugins.workflow._reopen_completed_run") as mock_reopen:
            mock_reopen.return_value = True
            _on_kanban_task_claimed(task_id="t_implement", assignee="newton")
            mock_reopen.assert_called_once_with("t_implement")

    def test_claimed_hook_swallows_errors(self):
        from plugins.workflow import _on_kanban_task_claimed

        with patch("plugins.workflow._reopen_completed_run", side_effect=RuntimeError("boom")):
            # Must not raise — hook failures are best-effort
            _on_kanban_task_claimed(task_id="t_implement")
