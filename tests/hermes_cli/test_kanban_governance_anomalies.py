"""Anomaly detection tests: pathological review/block/retry loops.

These tests verify that tasks exceeding configured thresholds for repeat
review cycles, failures, or identical blocker reasons are detected and
converted into explicit maintainer defects rather than continuing silently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_repeat_review_loop_creates_default_maintainer_defect(kanban_home: Path) -> None:
    """A task that cycles through review 4+ times creates a maintainer defect.
    
    The task goes: claim -> review -> changes -> claim -> review -> changes...
    After 4 review cycles, a defect card should be created for 'default' assignee
    with title containing 'review loop'.
    """
    with kb.connect() as conn:
        # Create the initial task
        tid = kb.create_task(conn, title="loop", assignee="ocr")
        
        # Simulate 4 review cycles (review -> changes -> review -> changes...)
        for cycle in range(4):
            # Ensure task is ready or running
            task = kb.get_task(conn, tid)
            if task.status == "ready":
                claimed = kb.claim_task(conn, tid)
            else:
                claimed = kb.claim_review_task(conn, tid)
            
            run_id = claimed.current_run_id
            assert run_id is not None
            
            # Request review
            ok = kb.request_review(
                conn, tid,
                summary=f"done (cycle {cycle + 1})",
                goal="implement feature",
                judge="goal_judge",
                evidence_contract="artifacts exist",
                expected_run_id=run_id
            )
            assert ok is True
            
            # Claim review task
            review_claim = kb.claim_review_task(conn, tid)
            assert review_claim is not None
            
            # Request changes (back to rework)
            ok, reason = kb.request_changes(
                conn, tid,
                reason="same issue",
                expected_run_id=review_claim.current_run_id
            )
            assert ok is True, f"request_changes failed: {reason}"
        
        # After 4 review cycles, there should be a defect card for default
        defects = kb.list_tasks(conn, assignee="default", status="ready")
        assert any("review loop" in (t.title or "").lower() for t in defects), \
            f"No review loop defect found. Defects: {[(t.title, t.assignee) for t in defects]}"


def test_legitimate_review_cycles_dont_trigger_defect(kanban_home: Path) -> None:
    """Legitimate review -> rework -> review cycles should not create defects.
    
    If a task goes through review cycles but succeeds eventually, no defect
    should be created. Only pathological loops (repeated same failures) trigger.
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="work", assignee="ocr")
        
        # 2 legitimate review cycles (won't exceed threshold)
        for cycle in range(2):
            task = kb.get_task(conn, tid)
            if task.status == "ready":
                claimed = kb.claim_task(conn, tid)
            else:
                claimed = kb.claim_review_task(conn, tid)
            
            run_id = claimed.current_run_id
            assert run_id is not None
            
            ok = kb.request_review(
                conn, tid,
                summary=f"done (cycle {cycle + 1})",
                goal="implement feature",
                judge="goal_judge",
                evidence_contract="artifacts exist",
                expected_run_id=run_id
            )
            assert ok is True
            
            review_claim = kb.claim_review_task(conn, tid)
            assert review_claim is not None
            
            ok, reason = kb.request_changes(
                conn, tid,
                reason="different feedback each time",
                expected_run_id=review_claim.current_run_id
            )
            assert ok is True, f"request_changes failed: {reason}"
        
        # No defect should exist (below threshold)
        defects = kb.list_tasks(conn, assignee="default", status="ready")
        # Filter to only review loop defects
        review_loop_defects = [t for t in defects if "review loop" in (t.title or "").lower()]
        assert len(review_loop_defects) == 0, \
            f"Unexpected review loop defects: {[(t.title, t.assignee) for t in review_loop_defects]}"
