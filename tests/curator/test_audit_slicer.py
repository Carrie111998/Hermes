"""Unit tests for curator.audit_slicer."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


from curator.audit_slicer import slice_agent_events

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE = FIXTURES / "sample_audit.jsonl"
# Anchor "now" to fixture's max timestamp + 1 hour (NOW used to generate fixture).
FIXTURE_NOW = datetime(2026, 4, 26, 13, 0, 0, tzinfo=timezone.utc)


def test_slice_returns_only_agent_events():
    """Slicer returns only events whose source matches scout patterns."""
    result = slice_agent_events(SAMPLE, "scout", window_days=30, now=FIXTURE_NOW)
    assert result["agent"] == "scout"
    for ev in result["events"]:
        assert ev["source"] in {"scout", "jobflow-scout"}, ev["source"]


def test_slice_respects_window():
    """No events older than window_days from `now` may appear."""
    result = slice_agent_events(SAMPLE, "scout", window_days=30, now=FIXTURE_NOW)
    cutoff = FIXTURE_NOW - timedelta(days=30)
    for ev in result["events"]:
        ts = datetime.fromisoformat(ev["timestamp"])
        assert ts >= cutoff, f"event {ev['event_id']} at {ts} predates cutoff {cutoff}"


def test_slice_counts_runs_correctly():
    """Scout fixture: 7 cron_completed (ok) + 1 cron_failed (fail) within 30d."""
    result = slice_agent_events(SAMPLE, "scout", window_days=30, now=FIXTURE_NOW)
    assert result["runs_total"] == 8, result
    assert result["runs_ok"] == 7, result
    assert result["runs_fail"] == 1, result


def test_slice_extracts_avg_duration():
    """Mean of payload.duration across cron_completed events."""
    result = slice_agent_events(SAMPLE, "scout", window_days=30, now=FIXTURE_NOW)
    # Scout cron_completed payloads: 30, 35, 40, 45, 50, 55, 60 = avg 45.0
    assert result["avg_duration_s"] is not None
    assert 44.0 <= result["avg_duration_s"] <= 46.0, result["avg_duration_s"]


def test_slice_handles_rotated_archives(tmp_path):
    """Slicer reads audit.jsonl AND audit.jsonl.1 transparently."""
    main_audit = tmp_path / "audit.jsonl"
    rotated = tmp_path / "audit.jsonl.1"
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
    # 2 scout cron_completed in main file
    with main_audit.open("w", encoding="utf-8") as f:
        for i in range(2):
            f.write(json.dumps({
                "event_id": f"main-{i}",
                "event_type": "cron_completed",
                "source": "scout",
                "timestamp": (now - timedelta(days=1 + i)).isoformat(),
                "priority": "normal",
                "payload": {"duration": 10.0},
            }) + "\n")
    # 3 scout cron_completed in rotated file
    with rotated.open("w", encoding="utf-8") as f:
        for i in range(3):
            f.write(json.dumps({
                "event_id": f"rot-{i}",
                "event_type": "cron_completed",
                "source": "scout",
                "timestamp": (now - timedelta(days=10 + i)).isoformat(),
                "priority": "normal",
                "payload": {"duration": 10.0},
            }) + "\n")
    result = slice_agent_events(main_audit, "scout", window_days=30, now=now)
    # Total of 5 (2 main + 3 rotated)
    assert result["runs_total"] == 5, result


def test_slice_returns_empty_for_unknown_agent():
    """Unknown agent returns empty stats but no exception."""
    result = slice_agent_events(SAMPLE, "ghost", window_days=30, now=FIXTURE_NOW)
    assert result["agent"] == "ghost"
    assert result["runs_total"] == 0
    assert result["events"] == []


def test_slice_groups_by_event_type():
    """Result includes event_type_counts dict."""
    result = slice_agent_events(SAMPLE, "scout", window_days=30, now=FIXTURE_NOW)
    assert "event_type_counts" in result
    counts = result["event_type_counts"]
    # Scout has 7 cron_started, 7 cron_completed, 1 cron_failed, 1 mailbox_message in window
    assert counts.get("cron_started", 0) == 7
    assert counts.get("cron_completed", 0) == 7
    assert counts.get("cron_failed", 0) == 1
    assert counts.get("mailbox_message", 0) == 1
