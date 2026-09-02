"""Unit tests for neutral Kanban lifecycle policy hooks: pre_kanban_task_complete and pre_kanban_task_claim."""

from __future__ import annotations

import sqlite3
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import plugins


def test_pre_kanban_task_complete_veto(tmp_path, monkeypatch):
    """Plugins can synchronously reject a task completion via pre_kanban_task_complete hook."""
    db_path = tmp_path / "kanban.db"
    conn = kb.connect(db_path=db_path)

    task_id = kb.create_task(conn, title="Gated Task", body="Test body", created_by="test")
    kb.claim_task(conn, task_id)

    # Register a hook that vetos completion
    def _veto_complete(**kwargs):
        if kwargs.get("task_id") == task_id:
            return {"allow": False, "reason": "preservation milestones missing"}
        return None

    mgr = plugins.get_plugin_manager()
    with mgr._discovery_lock:
        mgr._hooks.setdefault("pre_kanban_task_complete", []).append(_veto_complete)

    try:
        with pytest.raises(PermissionError) as exc_info:
            kb.complete_task(conn, task_id, result="Done without gate")
        assert "preservation milestones missing" in str(exc_info.value)

        # Task must still be in running status, not done
        t = kb.get_task(conn, task_id)
        assert t.status == "running"
    finally:
        with mgr._discovery_lock:
            if _veto_complete in mgr._hooks.get("pre_kanban_task_complete", []):
                mgr._hooks["pre_kanban_task_complete"].remove(_veto_complete)
        conn.close()


def test_pre_kanban_task_claim_veto(tmp_path):
    """Plugins can veto task claim via pre_kanban_task_claim hook."""
    db_path = tmp_path / "kanban.db"
    conn = kb.connect(db_path=db_path)

    task_id = kb.create_task(conn, title="Claim Gated Task", body="Test body", created_by="test")

    def _veto_claim(**kwargs):
        if kwargs.get("task_id") == task_id:
            return {"allow": False, "reason": "prerequisites not met"}
        return None

    mgr = plugins.get_plugin_manager()
    with mgr._discovery_lock:
        mgr._hooks.setdefault("pre_kanban_task_claim", []).append(_veto_claim)

    try:
        claimed = kb.claim_task(conn, task_id)
        assert claimed is None

        # Task remains in ready status
        t = kb.get_task(conn, task_id)
        assert t.status == "ready"
    finally:
        with mgr._discovery_lock:
            if _veto_claim in mgr._hooks.get("pre_kanban_task_claim", []):
                mgr._hooks["pre_kanban_task_claim"].remove(_veto_claim)
        conn.close()
