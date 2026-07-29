"""Regression tests: apply_approvals must not re-fire on a stale approval.

t_jarvis_autopromote_20260728 — the approval-auto-clear loop. A card
carrying a historical REVIEW_VERDICT=APPROVED comment was auto-cleared
(unblocked -> promoted -> claimed) EVERY time it re-blocked for a new
reason (24h soak gate, needs_input park), because apply_approvals kept
re-firing on the same old approval comment_id. An approval verdict
covers what it reviewed; a later block for a different reason is a new
gate the stale verdict must not defeat.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    return tmp_path


def _approved_blocked_card(conn) -> str:
    tid = kb.create_task(conn, title="reviewed card")
    kb.add_comment(conn, tid, "reviewer", "REVIEW_VERDICT=APPROVED looks good")
    kb.claim_task(conn, tid)
    kb.block_task(
        conn,
        tid,
        reason="first block",
        expected_run_id=kb.get_task(conn, tid).current_run_id,
    )
    return tid


def test_apply_approvals_clears_approved_card_once(kanban_home):
    with kb.connect() as conn:
        tid = _approved_blocked_card(conn)
        cleared = kb.apply_approvals(conn)
        assert tid in cleared
        assert kb.get_task(conn, tid).status == "todo"


def test_apply_approvals_does_not_refire_on_same_approval(kanban_home):
    """The core regression: same approval comment_id must not clear twice."""
    with kb.connect() as conn:
        tid = _approved_blocked_card(conn)
        first = kb.apply_approvals(conn)
        assert tid in first

        # Card re-blocks for a NEW reason (e.g. soak gate / needs_input).
        kb.recompute_ready(conn)  # todo -> ready so it can be claimed
        kb.claim_task(conn, tid)
        kb.block_task(
            conn,
            tid,
            reason="24h soak gate (different reason)",
            kind="needs_input",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        assert kb.get_task(conn, tid).status == "blocked"

        # Stale approval must NOT clear it again.
        second = kb.apply_approvals(conn)
        assert tid not in second
        assert kb.get_task(conn, tid).status == "blocked"


def test_apply_approvals_fresh_approval_still_clears(kanban_home):
    """A NEW approval comment posted after the re-block clears normally."""
    with kb.connect() as conn:
        tid = _approved_blocked_card(conn)
        assert tid in kb.apply_approvals(conn)

        kb.recompute_ready(conn)  # todo -> ready so it can be claimed
        kb.claim_task(conn, tid)
        kb.block_task(
            conn,
            tid,
            reason="re-blocked",
            kind="needs_input",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        # Fresh verdict on the new state -> new comment_id -> clears.
        kb.add_comment(conn, tid, "reviewer", "REVIEW_VERDICT=APPROVED again, fresh state")
        cleared = kb.apply_approvals(conn)
        assert tid in cleared
        assert kb.get_task(conn, tid).status == "todo"


def test_apply_approvals_reopen_marker_still_respected(kanban_home):
    """Existing re-open guard must keep working alongside idempotence."""
    with kb.connect() as conn:
        tid = _approved_blocked_card(conn)
        kb.add_comment(conn, tid, "reviewer", "REVIEW_VERDICT=CHANGES_REQUESTED re-opened")
        cleared = kb.apply_approvals(conn)
        assert tid not in cleared
        assert kb.get_task(conn, tid).status == "blocked"
