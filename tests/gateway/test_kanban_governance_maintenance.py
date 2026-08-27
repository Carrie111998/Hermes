"""Tests for default-owned kanban governance maintenance scans.

Tests the maintenance scan function that detects governance anomalies
and creates default-owned maintainer defects in the watcher path.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_governance as kg


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Set up a temporary HERMES_HOME with initialized kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_maintenance_scan_flags_review_loop_anomaly(kanban_home):
    """Maintenance scan detects review loop anomaly and creates default defect."""
    conn = kb.connect()
    try:
        # Create a task with review loop
        tid = kb.create_task(
            conn,
            title="looping task",
            body="this gets rejected repeatedly",
            assignee="alice",
        )
        
        # Set governance_review_count to threshold to trigger anomaly
        conn.execute(
            "UPDATE tasks SET governance_review_count = ? WHERE id = ?",
            (kg.REVIEW_LOOP_THRESHOLD, tid),
        )
        conn.commit()
        
        # Refresh task to get updated counters
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.governance_review_count == kg.REVIEW_LOOP_THRESHOLD
        
        # Run the maintenance scan
        anomalies = kg.scan_board_for_governance_defects(conn)
        
        # Assert a review loop anomaly was detected for this task
        assert len(anomalies) >= 1
        loop_anomaly = next(
            (a for a in anomalies if a.task_id == tid), None
        )
        assert loop_anomaly is not None
        assert loop_anomaly.kind == "repeat_review_loop"
        
        # Verify that a defect was created and is assigned to default
        defect_tasks = kb.list_tasks(
            conn,
            assignee="default",
        )
        
        # Should have at least one governance defect
        governance_defects = [
            t for t in defect_tasks
            if "governance" in t.title.lower() and tid in t.title
        ]
        assert len(governance_defects) > 0, (
            f"Expected governance defect for {tid}, "
            f"found: {len(defect_tasks)} default tasks, defects: {[t.title for t in governance_defects]}"
        )
        
    finally:
        conn.close()


def test_maintenance_scan_idempotent(kanban_home):
    """Maintenance scan does not create duplicate defects on repeated runs."""
    conn = kb.connect()
    try:
        # Create a task with review loop
        tid = kb.create_task(
            conn,
            title="looping task",
            body="this gets rejected repeatedly",
            assignee="alice",
        )
        
        # Set governance_review_count to threshold
        conn.execute(
            "UPDATE tasks SET governance_review_count = ? WHERE id = ?",
            (kg.REVIEW_LOOP_THRESHOLD, tid),
        )
        conn.commit()
        
        # Run scan first time
        anomalies1 = kg.scan_board_for_governance_defects(conn)
        defects_before = [
            t for t in kb.list_tasks(conn, assignee="default")
            if "governance" in t.title.lower()
        ]
        
        # Run scan again - should not create duplicate
        anomalies2 = kg.scan_board_for_governance_defects(conn)
        defects_after = [
            t for t in kb.list_tasks(conn, assignee="default")
            if "governance" in t.title.lower()
        ]
        
        # Should not have created a duplicate defect
        assert len(defects_before) == len(defects_after), (
            f"Scan created duplicate defect: "
            f"{len(defects_before)} defects before, {len(defects_after)} after"
        )
        
    finally:
        conn.close()


def test_maintenance_scan_legitimate_review_cycles_no_defect(kanban_home):
    """Tasks with review cycles below threshold do not trigger defects."""
    conn = kb.connect()
    try:
        # Create a task with a few (but below threshold) review cycles
        tid = kb.create_task(
            conn,
            title="normal task",
            body="reviewed a couple times then passed",
            assignee="alice",
        )
        
        # Set governance_review_count to just below threshold
        conn.execute(
            "UPDATE tasks SET governance_review_count = ? WHERE id = ?",
            (kg.REVIEW_LOOP_THRESHOLD - 1, tid),
        )
        conn.commit()
        
        # Run maintenance scan
        anomalies = kg.scan_board_for_governance_defects(conn)
        
        # Should not detect an anomaly for this task
        assert not any(a.task_id == tid for a in anomalies), (
            f"Task {tid} should not be flagged (count < threshold)"
        )
        
    finally:
        conn.close()
