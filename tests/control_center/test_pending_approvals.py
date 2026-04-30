"""Tests for control_center.storage.list_pending_approvals."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from control_center import storage


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_window_h_filters_old_entries(tmp_storage):
    now = datetime.now(timezone.utc)
    tmp_storage.write_log([
        {  # pending, in window (1h ago)
            "event": "approval_requested",
            "at": _iso(now - timedelta(hours=1)),
            "job_id": "fresh-1",
            "job_title": "Fresh job",
            "auto_resolved": False,
        },
        {  # pending, OUTSIDE window (48h ago)
            "event": "approval_requested",
            "at": _iso(now - timedelta(hours=48)),
            "job_id": "stale-1",
            "job_title": "Stale job",
            "auto_resolved": False,
        },
    ])

    pending = storage.list_pending_approvals(window_h=24)

    assert len(pending) == 1
    assert pending[0]["job_id"] == "fresh-1"


def test_auto_resolved_excluded(tmp_storage):
    now = datetime.now(timezone.utc)
    tmp_storage.write_log([
        {
            "event": "approval_requested",
            "at": _iso(now - timedelta(minutes=5)),
            "job_id": "auto-1",
            "job_title": "Auto resolved",
            "auto_resolved": True,
        },
        {
            "event": "approval_requested",
            "at": _iso(now - timedelta(minutes=5)),
            "job_id": "pending-1",
            "job_title": "Pending Diego",
            "auto_resolved": False,
        },
    ])

    pending = storage.list_pending_approvals(window_h=24)

    assert len(pending) == 1
    assert pending[0]["job_id"] == "pending-1"


def test_resolved_in_state_db_excluded(tmp_storage):
    now = datetime.now(timezone.utc)
    tmp_storage.write_log([
        {
            "event": "approval_requested",
            "at": _iso(now - timedelta(minutes=10)),
            "job_id": "approved-1",
            "job_title": "Already approved",
            "auto_resolved": False,
        },
        {
            "event": "approval_requested",
            "at": _iso(now - timedelta(minutes=10)),
            "job_id": "pending-2",
            "job_title": "Still pending",
            "auto_resolved": False,
        },
    ])
    tmp_storage.record_resolution("job-approved-1", "approved")

    pending = storage.list_pending_approvals(window_h=24)

    assert len(pending) == 1
    assert pending[0]["job_id"] == "pending-2"


def test_window_h_none_includes_all_unresolved(tmp_storage):
    """Backward-compat: window_h=None preserves the pre-task behaviour."""
    now = datetime.now(timezone.utc)
    tmp_storage.write_log([
        {
            "event": "approval_requested",
            "at": _iso(now - timedelta(hours=72)),
            "job_id": "old-pending",
            "job_title": "Very old pending",
            "auto_resolved": False,
        },
    ])

    pending = storage.list_pending_approvals(window_h=None)

    assert len(pending) == 1
    assert pending[0]["job_id"] == "old-pending"
