"""Test for FIX A: evidence-less completion is rejected."""

from __future__ import annotations

import sqlite3
from pathlib import Path
import pytest

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


def test_evidence_less_completion_rejected(kanban_home):
    """A task declaring a measurement done-condition must route through review."""
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="Measurable Task",
            body="DONE-CONDITION: measure the climb of points",
            assignee="default"
        )
        task = kb.get_task(conn, task_id)
        assert task.status == "ready"
        
        # Claim task
        claimed_task = kb.claim_task(conn, task_id, claimer="default")
        assert claimed_task is not None
        assert claimed_task.id == task_id
        assert claimed_task.status == "running"

        # Attempt to complete without review
        with pytest.raises(ValueError, match="Task has a measurable done-condition"):
            kb.complete_task(
                conn,
                task_id, 
                expected_run_id=claimed_task.current_run_id,
                result="I am done"
            )
            
        task = kb.get_task(conn, task_id)
        assert task.status == "running"  # Stays in flight

        # Now request review, and approve review completion
        ok = kb.request_review(conn, task_id, expected_run_id=claimed_task.current_run_id, force=True)
        assert ok is True
        task = kb.get_task(conn, task_id)
        assert task.status == "review"

        # Complete from review lane
        success = kb.complete_task(conn, task_id, result="Review approved evidence")
        assert success is True
        task = kb.get_task(conn, task_id)
        assert task.status == "done"


def test_missing_artifact_rejected(kanban_home):
    """A task declaring an artifact must have the artifact on disk."""
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="Artifact Task",
            body="Just a normal task",
            assignee="default"
        )
        claimed_task = kb.claim_task(conn, task_id, claimer="default")
        assert claimed_task is not None
        
        # Missing artifact
        with pytest.raises(kb.ArtifactPreservationError, match="unavailable or does not exist"):
            kb.complete_task(
                conn,
                task_id,
                expected_run_id=claimed_task.current_run_id,
                metadata={"artifacts": ["/tmp/nonexistent_artifact_xyz.txt"]}
            )
            
        task = kb.get_task(conn, task_id)
        assert task.status == "running"  # Stays in flight


def test_normal_task_completes_normally(kanban_home):
    """Normal tasks without measurement done-conditions complete without review."""
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="Standard Bug Fix",
            body="Fix minor typo in README",
            assignee="default"
        )
        claimed_task = kb.claim_task(conn, task_id, claimer="default")
        assert claimed_task is not None
        
        success = kb.complete_task(
            conn,
            task_id,
            expected_run_id=claimed_task.current_run_id,
            result="Fixed README typo"
        )
        assert success is True
        task = kb.get_task(conn, task_id)
        assert task.status == "done"
