"""
End-to-end test for the workflow auto-resume hook chain:

  done card --(manual reset)--> ready --(dispatcher claim)-->
  kanban_task_claimed hook --> _reopen_completed_run -->
  state reset + execution flipped + supervisor spawned (--run-id <exact>)

Drives the REAL hook + state-file machinery against a temp kanban DB
and temp workflows dir. The supervisor subprocess is mocked (we only
verify the spawn command); everything else is real.

Run: python3 -m pytest tests/test_workflow_auto_resume_e2e.py -v
"""

import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """Point kanban + workflow files at temp locations."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    wf_dir = tmp_path / "wf"
    wf_dir.mkdir()
    # Kanban DB lives under HERMES_KANBAN_HOME for a temp board
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_WORKFLOW_FILES", str(wf_dir))
    # Board DB path for 'resume-test' board: <home>/kanban/boards/resume-test/kanban.db
    board_dir = home / "kanban" / "boards" / "resume-test"
    board_dir.mkdir(parents=True)
    db_path = board_dir / "kanban.db"
    from hermes_cli import kanban_db
    kanban_db._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    conn = kanban_db.connect(db_path=db_path)
    conn.close()
    return {"home": home, "wf_dir": wf_dir, "db_path": db_path}


@pytest.fixture
def resume_workflow_yaml(isolated_env):
    """A tiny workflow with one review node."""
    wf_dir = isolated_env["wf_dir"]
    yaml_content = """name: resume-test
description: "Auto-resume e2e"
trigger_events:
  - workflow_dispatch
roles:
  coder: newton
  reviewer: newton
nodes:
  implement:
    description: "Produce output"
    agent: "{coder}"
    task: "Do the work."
    timeout_minutes: 5
    reviews:
      - verify
  verify:
    description: "Check the work"
    agent: "{reviewer}"
    task: "Check the work."
    timeout_minutes: 5
"""
    path = wf_dir / "resume-test.yaml"
    path.write_text(yaml_content)
    return path


def _make_completed_state(wf_dir, run_id):
    """Build a retained state file for a completed run (as _clear_state leaves it)."""
    state = {
        "workflow_name": "resume-test",
        "kanban_board": "resume-test",
        "run_id": run_id,
        "current_layer": 1,
        "layers": [["implement"], ["verify"]],
        "context": {},
        "states": {
            "implement": {
                "node_id": "implement",
                "status": "done",
                "kanban_card_id": "t_implement",
                "attempts": 1,
            },
            "verify": {
                "node_id": "verify",
                "status": "done",
                "kanban_card_id": "t_verify",
                "attempts": 1,
            },
        },
        "results": {"implement": "done", "verify": "done"},
        "session_info": {"platform": "discord", "chat_id": "123", "profile": "newton"},
        "final_status": "completed",
    }
    # State dir lives under HERMES_WORKFLOW_FILES (the workflows dir)
    state_dir = wf_dir / ".engine-state"
    state_dir.mkdir(exist_ok=True)
    path = state_dir / f"resume-test_{run_id}_state.json"
    path.write_text(json.dumps(state, indent=2))
    return state, path


class TestAutoResumeE2E:
    """Real state file + real kanban claim + real hook wiring."""

    def test_claim_on_completed_card_reopens_run(
        self, isolated_env, resume_workflow_yaml, tmp_path, monkeypatch,
    ):
        """The full chain: state file → claim → re-open → supervisor spawn."""
        from plugins.workflow import _on_kanban_task_claimed
        from hermes_cli import kanban_db

        run_id = "resume-test-20260804-120000-123456"
        state, state_path = _make_completed_state(isolated_env["wf_dir"], run_id)

        # Record a terminal execution row for this run in the job log DB
        exec_db = isolated_env["home"] / "workflows" / "executions.db"
        exec_db.parent.mkdir(parents=True)
        with sqlite3.connect(str(exec_db)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS workflow_executions ("
                "run_id TEXT PRIMARY KEY, workflow_name TEXT NOT NULL, board TEXT, "
                "status TEXT NOT NULL DEFAULT 'running', started_at TEXT, "
                "finished_at TEXT, error TEXT, current_layer INTEGER DEFAULT 0, "
                "total_layers INTEGER DEFAULT 0)"
            )
            conn.execute(
                "INSERT INTO workflow_executions (run_id, workflow_name, board, status, finished_at) "
                "VALUES (?, ?, ?, 'completed', '2026-08-04T12:05:00+00:00')",
                (run_id, "resume-test", "resume-test"),
            )

        # Simulate the dispatcher claiming the re-activated card.
        # _fire_kanban_lifecycle_hook fires the registered plugin hook.
        captured_spawn = {}
        with patch("plugins.workflow._spawn_supervisor_for_resume") as mock_spawn:
            # The real hook function (registered in plugin setup) is called
            # directly with the same kwargs the lifecycle hook passes.
            _on_kanban_task_claimed(task_id="t_implement", assignee="newton")

            mock_spawn.assert_called_once()
            # Capture what _spawn_supervisor_for_resume would have spawned
            spawn_state = mock_spawn.call_args[0][0]
            captured_spawn["run_id"] = spawn_state.get("run_id")
            captured_spawn["board"] = spawn_state.get("kanban_board")

        # 1. State file rewritten correctly
        rewritten = json.loads(state_path.read_text())
        assert "final_status" not in rewritten, "final_status must be cleared"
        assert rewritten["states"]["implement"]["status"] == "ready"
        assert rewritten["states"]["implement"]["completed_at"] is None
        # Layer rewound to implement's layer (0)
        assert rewritten["current_layer"] == 0
        # Reviewer cleared for fresh dispatch
        assert rewritten["states"]["verify"]["kanban_card_id"] is None
        assert rewritten["states"]["verify"]["status"] == "pending"

        # 2. Execution row flipped back to running
        with sqlite3.connect(str(exec_db)) as conn:
            row = conn.execute(
                "SELECT status, finished_at FROM workflow_executions WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        assert row[0] == "running"
        assert row[1] is None

        # 3. Supervisor spawn targeted the exact run
        assert captured_spawn["run_id"] == run_id
        assert captured_spawn["board"] == "resume-test"

    def test_claim_on_active_card_does_not_reopen(
        self, isolated_env, resume_workflow_yaml, tmp_path, monkeypatch,
    ):
        """No final_status → active run; hook does nothing."""
        from plugins.workflow import _on_kanban_task_claimed

        run_id = "resume-test-20260804-120000-123456"
        state, state_path = _make_completed_state(isolated_env["wf_dir"], run_id)
        # Simulate an active run: no final_status marker
        state.pop("final_status")
        state_path.write_text(json.dumps(state, indent=2))

        with patch("plugins.workflow._spawn_supervisor_for_resume") as mock_spawn:
            _on_kanban_task_claimed(task_id="t_implement", assignee="newton")
            mock_spawn.assert_not_called()

    def test_claim_on_non_workflow_card_does_not_reopen(
        self, isolated_env, resume_workflow_yaml, tmp_path, monkeypatch,
    ):
        """Card not in any state file → hook does nothing."""
        from plugins.workflow import _on_kanban_task_claimed

        with patch("plugins.workflow._spawn_supervisor_for_resume") as mock_spawn:
            _on_kanban_task_claimed(task_id="t_random", assignee="newton")
            mock_spawn.assert_not_called()
