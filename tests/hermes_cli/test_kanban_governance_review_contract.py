"""Review-contract governance tests: goal + judge + evidence contract enforcement.

These tests validate that a card cannot enter review without a proper
governance contract: explicit goal, explicit judge, and explicit evidence
(pass/fail contract).

The lifecycle contract is:
* A review must include goal, judge, and evidence_contract fields
* Missing any of these three causes the review to be rejected
* Existing review reruns (review -> rework -> review) are still valid and
  preserve the original contract fields
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


def test_request_review_requires_goal_judge_and_evidence(kanban_home: Path) -> None:
    """A review with only some contract fields must be rejected (all-or-nothing)."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="worker")
        claimed = kb.claim_task(conn, tid)
        # When only goal is provided (partial), validation should fail
        ok, reason = kb.request_review(
            conn,
            tid,
            summary="done",
            expected_run_id=claimed.current_run_id,
            goal="pass tests",  # Provided
            judge=None,  # Missing
            evidence_contract=None,  # Missing
            with_reason=True,
        )
        assert ok is False
        assert "judge" in reason.lower()


def test_request_review_legacy_call_with_no_contract_fields_succeeds(
    kanban_home: Path,
) -> None:
    """For backwards compatibility, calling request_review without contract fields works."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="worker")
        claimed = kb.claim_task(conn, tid)
        # Legacy call: no contract fields provided at all (all default to None)
        ok, reason = kb.request_review(
            conn,
            tid,
            summary="done",
            expected_run_id=claimed.current_run_id,
            with_reason=True,
        )
        assert ok is True, f"Unexpected failure: {reason}"


def test_request_review_with_all_three_fields_succeeds(kanban_home: Path) -> None:
    """A review with goal, judge, and evidence_contract fields succeeds."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="worker")
        claimed = kb.claim_task(conn, tid)
        ok, reason = kb.request_review(
            conn,
            tid,
            summary="implementation complete",
            expected_run_id=claimed.current_run_id,
            goal="pass all tests",
            judge="qa_checker",
            evidence_contract="artifact passes lint + tests",
            with_reason=True,
        )
        assert ok is True, f"Unexpected failure: {reason}"

        # Verify the contract fields are persisted in the event
        events = _events(conn, tid, kind="review_requested")
        assert len(events) == 1
        event_kind, payload = events[0]
        assert payload is not None
        assert payload.get("goal") == "pass all tests"
        assert payload.get("judge") == "qa_checker"
        assert payload.get("evidence_contract") == "artifact passes lint + tests"


def test_request_review_rejects_missing_goal(kanban_home: Path) -> None:
    """A review missing only goal must be rejected."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="worker")
        claimed = kb.claim_task(conn, tid)
        ok, reason = kb.request_review(
            conn,
            tid,
            summary="done",
            expected_run_id=claimed.current_run_id,
            goal=None,
            judge="reviewer",
            evidence_contract="it works",
            with_reason=True,
        )
        assert ok is False
        assert "goal" in reason.lower()


def test_request_review_rejects_missing_judge(kanban_home: Path) -> None:
    """A review missing only judge must be rejected."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="worker")
        claimed = kb.claim_task(conn, tid)
        ok, reason = kb.request_review(
            conn,
            tid,
            summary="done",
            expected_run_id=claimed.current_run_id,
            goal="test everything",
            judge=None,
            evidence_contract="it works",
            with_reason=True,
        )
        assert ok is False
        assert "judge" in reason.lower()


def test_request_review_rejects_missing_evidence_contract(kanban_home: Path) -> None:
    """A review missing only evidence_contract must be rejected."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="worker")
        claimed = kb.claim_task(conn, tid)
        ok, reason = kb.request_review(
            conn,
            tid,
            summary="done",
            expected_run_id=claimed.current_run_id,
            goal="test everything",
            judge="reviewer",
            evidence_contract=None,
            with_reason=True,
        )
        assert ok is False
        assert "evidence" in reason.lower()


def test_review_reruns_preserve_contract_fields(kanban_home: Path) -> None:
    """When re-reviewing after rework, contract fields remain available for re-use."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="worker")
        claimed = kb.claim_task(conn, tid)

        # First review: set all contract fields
        ok, reason = kb.request_review(
            conn,
            tid,
            summary="ready for review",
            expected_run_id=claimed.current_run_id,
            goal="all tests pass",
            judge="qa_team",
            evidence_contract="zero failing tests",
            with_reason=True,
        )
        assert ok is True

        # Reviewer claims the task in review status to request changes
        review_claim = kb.claim_review_task(conn, tid)
        assert review_claim is not None

        # Reviewer requests changes
        ok, reason = kb.request_changes(
            conn,
            tid,
            reason="Fix failing test_foo",
            expected_run_id=review_claim.current_run_id,
        )
        assert ok is True

        # Worker claims again and reruns review
        # (this should still accept all three fields and not lose them)
        claimed2 = kb.claim_task(conn, tid)
        ok, reason = kb.request_review(
            conn,
            tid,
            summary="test_foo now passes",
            expected_run_id=claimed2.current_run_id,
            goal="all tests pass",
            judge="qa_team",
            evidence_contract="zero failing tests",
            with_reason=True,
        )
        assert ok is True

        # Verify both review events have proper contract fields
        events = _events(conn, tid, kind="review_requested")
        assert len(events) == 2

        for event_kind, payload in events:
            assert payload is not None
            assert payload.get("goal") == "all tests pass"
            assert payload.get("judge") == "qa_team"
            assert payload.get("evidence_contract") == "zero failing tests"


def test_review_contract_fields_empty_string_rejected(kanban_home: Path) -> None:
    """Empty strings for goal/judge/evidence_contract are treated as missing."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="worker")
        claimed = kb.claim_task(conn, tid)
        ok, reason = kb.request_review(
            conn,
            tid,
            summary="done",
            expected_run_id=claimed.current_run_id,
            goal="",  # Empty string
            judge="reviewer",
            evidence_contract="it works",
            with_reason=True,
        )
        assert ok is False
        assert "goal" in reason.lower()
