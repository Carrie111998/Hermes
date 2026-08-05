"""Regression: a block-loop escalation must not be resurrected by the
dispatcher's auto-decomposer.

Live defect (board default, event 2955 / run 233, task ``t_bdf1001a``;
``t_80a3542a`` showed the same class): ``block_task`` routed a repeated
``needs_input`` block (``block_recurrences >= BLOCK_RECURRENCE_LIMIT``) to
``status='triage'`` and emitted ``block_loop_detected``. But ``triage`` is an
*automation* queue, not a human hold: with ``kanban.auto_decompose`` enabled
(the default) the dispatcher treats every triage row as a decompose/specify
candidate. On the next tick it silently advanced the escalated card
``triage -> todo`` (no lifecycle event) and ``recompute_ready`` would then make
it ``ready`` — exactly the blocked-task-resurrection class this branch fixes.

The invariant pinned here: a block-loop escalation lands in a *sticky*
``blocked`` state that auto-decompose, ``recompute_ready``, ``claim_task`` and
dispatcher ticks cannot advance. Only an explicit ``unblock_task`` resumes it.
Ordinary triage cards keep auto-decomposing.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_decompose as kd


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn: sqlite3.Connection, title: str = "t") -> str:
    tid = kb.create_task(conn, title=title, assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    return tid


def _escalate_block_loop(conn: sqlite3.Connection, kind: str = "needs_input") -> str:
    """Drive a task through the real block -> unblock -> re-block loop until
    ``block_task`` trips the recurrence breaker."""
    tid = _running_task(conn, title="worker keeps asking the same question")
    assert kb.block_task(conn, tid, reason="need creds", kind=kind)
    assert kb.unblock_task(conn, tid)  # the cron that keeps spinning it
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    assert kb.block_task(conn, tid, reason="need creds", kind=kind)
    row = conn.execute(
        "SELECT block_recurrences FROM tasks WHERE id=?", (tid,)
    ).fetchone()
    assert int(row["block_recurrences"]) >= kb.BLOCK_RECURRENCE_LIMIT
    kinds = [
        r["kind"] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (tid,)
        )
    ]
    assert "block_loop_detected" in kinds
    return tid


# ---------------------------------------------------------------------------
# The escalation itself
# ---------------------------------------------------------------------------


def test_block_loop_escalation_is_sticky_blocked_not_triage(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _escalate_block_loop(conn)
        task = kb.get_task(conn, tid)
        assert task.status == "blocked", (
            "block-loop escalation must land in a sticky blocked state, not in "
            "the triage automation queue"
        )
        assert task.block_kind == "needs_input"
        assert kb._has_sticky_block(conn, tid) is True


def test_block_loop_escalation_is_invisible_to_auto_decompose(kanban_home: Path) -> None:
    """The real dispatcher path: ``list_triage_ids`` feeds the auto-decomposer
    every tick. An escalated card must never appear in it."""
    with kb.connect_closing() as conn:
        tid = _escalate_block_loop(conn)
        ordinary = kb.create_task(conn, title="rough idea", triage=True)

    assert kd.list_triage_ids() == [ordinary], (
        "auto-decompose must not see a block-loop escalation as a triage "
        "candidate"
    )

    outcome = kd.decompose_task(tid)
    assert outcome.ok is False


def test_block_loop_escalation_survives_dispatcher_ticks(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _escalate_block_loop(conn)
        for _ in range(5):
            assert kb.recompute_ready(conn) == 0
            assert kb.get_task(conn, tid).status == "blocked"
        assert kb.claim_task(conn, tid, claimer="worker") is None


def test_block_loop_escalation_rejects_specify_and_decompose(kanban_home: Path) -> None:
    """Defense in depth: even if something parks an escalated row back in
    ``triage``, the two production promotion helpers must refuse it."""
    with kb.connect_closing() as conn:
        tid = _escalate_block_loop(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
        assert kb.specify_triage_task(conn, tid, body="spec") is False
        assert kb.decompose_triage_task(
            conn, tid, root_assignee="worker",
            children=[{"title": "child"}],
        ) is None
        assert kb.get_task(conn, tid).status == "triage"


def test_explicit_unblock_resumes_a_block_loop_escalation(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _escalate_block_loop(conn)
        assert kb.unblock_task(conn, tid) is True
        assert kb.get_task(conn, tid).status == "ready"
        assert kb._has_sticky_block(conn, tid) is False
        assert kb.claim_task(conn, tid, claimer="worker") is not None


# ---------------------------------------------------------------------------
# Legacy rows already parked in triage by the old routing
# ---------------------------------------------------------------------------


def _legacy_escalated_row(conn: sqlite3.Connection, title: str) -> str:
    """Write a row shaped exactly like the pre-fix escalation: parked in
    ``triage`` with ``block_kind`` set and a trailing ``block_loop_detected``
    event."""
    tid = kb.create_task(conn, title=title)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status='triage', block_kind='needs_input', "
            "block_recurrences=? WHERE id=?",
            (kb.BLOCK_RECURRENCE_LIMIT, tid),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'block_loop_detected', ?, ?)",
            (
                tid,
                json.dumps({
                    "reason": "need creds",
                    "kind": "needs_input",
                    "recurrences": kb.BLOCK_RECURRENCE_LIMIT,
                    "limit": kb.BLOCK_RECURRENCE_LIMIT,
                }),
                int(time.time()),
            ),
        )
    return tid


def test_migration_lifts_legacy_triage_escalations_out_of_triage(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        escalated = _legacy_escalated_row(conn, "legacy escalation")
        ordinary = kb.create_task(conn, title="ordinary idea", triage=True)
        # An escalation an operator already unblocked and re-parked in triage
        # by hand is NOT ours to move.
        cleared = _legacy_escalated_row(conn, "already handled")
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) "
                "VALUES (?, 'unblocked', NULL, ?)",
                (cleared, int(time.time())),
            )

    kb.init_db()  # activation re-runs the migration pass

    with kb.connect_closing() as conn:
        assert kb.get_task(conn, escalated).status == "blocked"
        assert kb._has_sticky_block(conn, escalated) is True
        assert kb.get_task(conn, ordinary).status == "triage"
        assert kb.get_task(conn, cleared).status == "triage"

    assert sorted(kd.list_triage_ids()) == sorted([ordinary, cleared])


def test_migration_leaves_ordinary_triage_auto_decomposable(kanban_home: Path) -> None:
    """The legitimate flow must keep working: a plain triage card is still a
    decompose candidate and still promotes to ``todo``."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="build me a thing", triage=True)

    kb.init_db()

    assert kd.list_triage_ids() == [tid]
    with kb.connect_closing() as conn:
        assert kb.specify_triage_task(conn, tid, body="spec") is True
        assert kb.get_task(conn, tid).status in ("todo", "ready")
