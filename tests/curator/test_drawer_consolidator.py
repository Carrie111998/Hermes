"""Unit tests for curator.drawer_consolidator."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


from curator.drawer_consolidator import consolidate_for_agent

FIXTURES = Path(__file__).parent / "fixtures"
DRAWERS = FIXTURES / "sample_drawers.json"
FIXTURE_NOW = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)


def _load_fixture():
    with DRAWERS.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def test_consolidator_uses_injected_search():
    """Consolidator calls search_fn once per agent with the right query."""
    calls = []

    def fake_search(query: str, params: dict) -> dict:
        calls.append((query, params))
        return _load_fixture()

    result = consolidate_for_agent("scout", fake_search, window_days=30, now=FIXTURE_NOW)
    assert len(calls) >= 1
    # Query must contain the agent name
    assert any("scout" in c[0].lower() for c in calls), calls
    assert result["agent"] == "scout"


def test_consolidator_filters_by_window():
    """Drawers older than window_days are excluded from recent_drawers."""
    def search_fn(q, p):
        return _load_fixture()

    result = consolidate_for_agent("scout", search_fn, window_days=30, now=FIXTURE_NOW)
    # Fixture has 1 drawer at 2026-02-25 (60d ago) — must be excluded
    drawer_ids = [d["drawer_id"] for d in result["recent_drawers"]]
    assert "drawer_openclaw_scout_011" not in drawer_ids
    # All recent drawers must be in window
    for d in result["recent_drawers"]:
        ts = datetime.fromisoformat(d["created_at"])
        # FIXTURE_NOW - 30d = 2026-03-27
        assert (FIXTURE_NOW - ts).days <= 30


def test_consolidator_extracts_pattern_candidates():
    """3 fixture drawers about 'linkedin returned 0 results' surface as pattern candidates."""
    def search_fn(q, p):
        return _load_fixture()

    result = consolidate_for_agent("scout", search_fn, window_days=30, now=FIXTURE_NOW)
    candidate_titles = [c["title"].lower() for c in result["pattern_candidates"]]
    # The 3 linkedin-zero-results drawers should appear among candidates
    linkedin_zero = sum(1 for t in candidate_titles if "linkedin" in t and "0 results" in t)
    assert linkedin_zero >= 3, f"expected ≥3 linkedin-0-results candidates, got {candidate_titles}"


def test_consolidator_groups_by_room():
    """Result includes drawers_by_room dict."""
    def search_fn(q, p):
        return _load_fixture()

    result = consolidate_for_agent("scout", search_fn, window_days=30, now=FIXTURE_NOW)
    assert "drawers_by_room" in result
    assert isinstance(result["drawers_by_room"], dict)
    # Most fixture drawers are in room=agents (within window)
    assert result["drawers_by_room"].get("agents", 0) > 0


def test_consolidator_handles_search_failure():
    """If search_fn raises, consolidator returns sentinel with error field."""
    def broken_search(q, p):
        raise RuntimeError("MCP unreachable")

    result = consolidate_for_agent("scout", broken_search, window_days=30, now=FIXTURE_NOW)
    assert result["error"] is not None
    assert "MCP unreachable" in result["error"] or "unreachable" in result["error"].lower()
    assert result["recent_drawers"] == []
    assert result["pattern_candidates"] == []


def test_consolidator_caps_drawer_text_length():
    """Each drawer's body truncated to ≤500 chars."""
    def search_fn(q, p):
        return _load_fixture()

    result = consolidate_for_agent("scout", search_fn, window_days=30, now=FIXTURE_NOW)
    for d in result["recent_drawers"]:
        assert len(d["body"]) <= 500, f"drawer {d['drawer_id']} body is {len(d['body'])} chars"
