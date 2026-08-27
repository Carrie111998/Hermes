"""Route-economics governance tests.

These tests validate that route-economics metadata can be persisted as task
events, providing signals for governance defect detection.

The lifecycle contract is:
* When a task is dispatched, route-economics metadata can be recorded
* Metadata includes: expected_route, actual_route, free_attempted, fallback_used, paid_seat_consumed, cactus_verdict
* This metadata is available for later governance scans
"""

from __future__ import annotations

import json
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


def _events(conn, tid, kind=None):
    """Fetch events for a task, optionally filtered by kind."""
    rows = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
        (tid,),
    ).fetchall()
    out = [
        (r["kind"], json.loads(r["payload"]) if r["payload"] else None)
        for r in rows
    ]
    if kind is not None:
        out = [e for e in out if e[0] == kind]
    return out


def test_record_route_economics_free_route(kanban_home: Path) -> None:
    """Free-routed tasks record route-economics metadata."""
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="free-route task",
            assignee="default",
            body="should use free route",
        )
        
        # Record route-economics metadata for a free-routed task
        kb.record_route_economics(
            conn,
            task_id,
            expected_route="openai",
            actual_route="openai",  # No fallback
            free_attempted=True,
            fallback_used=False,
            paid_seat_consumed=False,
            cactus_verdict="matched",
        )

        # Verify event was recorded
        events = _events(conn, task_id, kind="route_economics")
        assert len(events) >= 1, "route_economics event not recorded"
        
        kind, payload = events[0]
        assert kind == "route_economics"
        assert payload["expected_route"] == "openai"
        assert payload["actual_route"] == "openai"
        assert payload["free_attempted"] is True
        assert payload["fallback_used"] is False
        assert payload["paid_seat_consumed"] is False
        assert payload["cactus_verdict"] == "matched"


def test_record_route_economics_fallback_to_paid(kanban_home: Path) -> None:
    """Tasks falling back to paid routes record fallback_used and paid_seat_consumed."""
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="fallback to paid",
            assignee="default",
            body="free route failed, use paid",
        )

        # Record route-economics for a fallback scenario
        kb.record_route_economics(
            conn,
            task_id,
            expected_route="openai",
            actual_route="openrouter",  # Different from expected
            free_attempted=True,
            fallback_used=True,
            paid_seat_consumed=True,
            cactus_verdict="disagree",
        )

        events = _events(conn, task_id, kind="route_economics")
        assert len(events) >= 1
        
        kind, payload = events[0]
        assert kind == "route_economics"
        assert payload["expected_route"] == "openai"
        assert payload["actual_route"] == "openrouter"
        assert payload["fallback_used"] is True
        assert payload["paid_seat_consumed"] is True
        assert payload["cactus_verdict"] == "disagree"


def test_record_route_economics_no_free_attempt(kanban_home: Path) -> None:
    """Tasks that don't attempt free-routing still record metadata."""
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="direct paid route",
            assignee="default",
            body="no free attempt",
        )

        # Record route-economics when no free route was attempted
        kb.record_route_economics(
            conn,
            task_id,
            expected_route="openrouter",
            actual_route="openrouter",
            free_attempted=False,
            fallback_used=False,
            paid_seat_consumed=True,
            cactus_verdict="not_eligible",
        )

        events = _events(conn, task_id, kind="route_economics")
        assert len(events) >= 1
        
        kind, payload = events[0]
        assert kind == "route_economics"
        assert payload["free_attempted"] is False
        assert payload["paid_seat_consumed"] is True
