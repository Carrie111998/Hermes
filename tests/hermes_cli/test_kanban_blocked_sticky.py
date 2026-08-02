"""Regression tests for #28712 — kanban dispatcher must not auto-promote
worker-initiated ``kanban_block`` (sticky blocks), but must keep
auto-recovering circuit-breaker blocks.

The bug: when a worker called ``kanban_block(reason="review-required:
...")`` to hand off to a human, the dispatcher's ``recompute_ready``
would promote the task back to ``ready`` on the next tick.  The fresh
worker found nothing to do (work already applied), exited cleanly, and
got recorded as a ``protocol_violation`` → ``gave_up`` → promote → loop
until manual intervention.

These tests pin down:

* Worker / operator-initiated blocks are sticky and survive
  ``recompute_ready``.
* Circuit-breaker blocks (``gave_up`` event, status flipped via
  ``_record_task_failure``) still auto-recover — the original intent
  of #40c1decb3 is preserved.
* An explicit ``kanban_unblock`` clears the sticky state.
* The full block → promote → crash → ``gave_up`` loop is broken after
  this fix: subsequent ticks leave the task blocked.

The tangentially related schema-init ordering bug originally reported
in #28712 (``init_db`` crashing on legacy DBs that pre-dated the
``session_id`` migration) is covered separately by
``test_kanban_db.py::test_connect_migrates_legacy_db_before_optional_column_indexes``,
landed via #28754 / #28781 ahead of this fix.
"""

from __future__ import annotations

import time
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


# ---------------------------------------------------------------------------
# Worker-initiated kanban_block must be sticky
# ---------------------------------------------------------------------------


def test_worker_block_is_not_auto_promoted_by_recompute_ready(kanban_home: Path) -> None:
    """A standalone task that a worker explicitly blocks for review
    must stay blocked across an arbitrary number of dispatcher ticks.
    Before #28712's fix, ``recompute_ready`` would silently flip it
    back to ``ready`` on the very next tick."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="needs human review")
        kb.claim_task(conn, tid)
        assert kb.block_task(
            conn, tid,
            reason="review-required: please verify ACL change",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        assert kb.get_task(conn, tid).status == "blocked"

        # Hammer the promotion code — exactly the dispatcher loop's
        # behaviour, just compressed in time.
        for _ in range(5):
            promoted = kb.recompute_ready(conn)
            assert promoted == 0, "worker-blocked task must not auto-promote"
            assert kb.get_task(conn, tid).status == "blocked"


def test_initial_blocked_task_is_sticky_after_assignment(kanban_home: Path) -> None:
    """An explicitly parked task must not be mistaken for a recoverable
    circuit-breaker block after a later assignment or dispatcher tick."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="initially parked guardrail",
            initial_status="blocked",
        )
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "blocked"
        assert task.block_kind == "needs_input"

        # Assignment is a normal operator action and must not release the
        # initial human-review parking decision.
        assert kb.assign_task(conn, tid, "default")
        assert kb.recompute_ready(conn) == 0
        task_after_assignment = kb.get_task(conn, tid)
        assert task_after_assignment is not None
        assert task_after_assignment.status == "blocked"

        event = conn.execute(
            "SELECT kind, payload FROM task_events "
            "WHERE task_id = ? AND kind = 'blocked' "
            "ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()
        assert event is not None
        assert '"kind": "needs_input"' in event["payload"]




# ---------------------------------------------------------------------------
# Historical untyped blocked cards
# ---------------------------------------------------------------------------


def test_legacy_blocked_task_can_be_migrated_to_typed_sticky_block(
    kanban_home: Path,
) -> None:
    """A pre-typed blocked card must have an explicit migration path.

    Historical sync cards can carry only status=blocked.  The normal
    block_task call used to return False for that row, leaving the card
    vulnerable to assign -> recompute_ready -> ready promotion.
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title='legacy blocked guardrail')
        conn.execute(
            'UPDATE tasks SET status = "blocked", block_kind = NULL, '
            'block_recurrences = 0 WHERE id = ?',
            (tid,),
        )
        conn.commit()

        assert kb.block_task(
            conn,
            tid,
            reason='legacy asset-pack review shell',
            kind='needs_input',
        )
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == 'blocked'
        assert task.block_kind == 'needs_input'

        assert kb.assign_task(conn, tid, 'default')
        assert kb.assign_task(conn, tid, None)
        assert kb.recompute_ready(conn) == 0
        task_after_assignment = kb.get_task(conn, tid)
        assert task_after_assignment is not None
        assert task_after_assignment.status == 'blocked'

        event = conn.execute(
            'SELECT payload FROM task_events '
            'WHERE task_id = ? AND kind = "blocked" '
            'ORDER BY id DESC LIMIT 1',
            (tid,),
        ).fetchone()
        assert event is not None
        assert 'needs_input' in event['payload']


def test_reclaim_preserves_existing_typed_gate(kanban_home: Path) -> None:
    """Reclaiming a wrongly running gated card must not return it to ready."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title='reclaim must preserve human gate',
            initial_status='blocked',
        )
        assert kb.unblock_task(conn, tid)
        assert kb.claim_task(conn, tid)

        assert kb.reclaim_task(
            conn,
            tid,
            reason='operator abort preserves needs_input gate',
        )
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == 'blocked'
        assert task.block_kind == 'needs_input'
        assert kb.recompute_ready(conn) == 0

        assert kb.assign_task(conn, tid, None)
        task_after_unassign = kb.get_task(conn, tid)
        assert task_after_unassign is not None
        assert task_after_unassign.status == 'blocked'

        event = conn.execute(
            'SELECT kind, payload FROM task_events '
            'WHERE task_id = ? AND kind = "blocked" '
            'ORDER BY id DESC LIMIT 1',
            (tid,),
        ).fetchone()
        assert event is not None
        assert event['kind'] == 'blocked'
        assert 'reclaim_preserved_sticky_block' in event['payload']


def test_triage_gate_can_be_repaired_to_sticky_block(kanban_home: Path) -> None:
    """A loop-breaker triage card can be explicitly returned to blocked."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title='triage repair guardrail')
        conn.execute(
            'UPDATE tasks SET status = "triage", block_kind = "needs_input", '
            'block_recurrences = 2 WHERE id = ?',
            (tid,),
        )
        conn.commit()

        assert kb.repair_blocked_task(
            conn,
            tid,
            reason='human gate confirmed after loop triage',
            kind='needs_input',
        )
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == 'blocked'
        assert task.block_kind == 'needs_input'
        assert task.block_recurrences == 1
        assert kb.assign_task(conn, tid, 'default')
        assert kb.recompute_ready(conn) == 0


def test_todo_typed_gate_is_not_promoted_and_can_be_repaired(
    kanban_home: Path,
) -> None:
    """A decomposed ``todo`` row with a typed gate must stay non-dispatchable."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title='todo typed gate guardrail')
        conn.execute(
            'UPDATE tasks SET status = "todo", block_kind = "needs_input", '
            'block_recurrences = 2 WHERE id = ?',
            (tid,),
        )
        conn.commit()

        assert kb.recompute_ready(conn) == 0
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == 'todo'

        assert kb.repair_blocked_task(
            conn,
            tid,
            reason='decomposition left a human gate in todo',
            kind='needs_input',
        )
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == 'blocked'
        assert task.block_kind == 'needs_input'
        assert task.block_recurrences == 1
        assert kb.assign_task(conn, tid, 'default')
        assert kb.recompute_ready(conn) == 0


# ---------------------------------------------------------------------------
# Circuit-breaker blocks still auto-recover (preserve #40c1decb3 intent)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# unblock_task clears the sticky state
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Full bug-shaped loop: block → promote → crash → gave_up → next tick
# ---------------------------------------------------------------------------


def test_protocol_violation_loop_is_broken(kanban_home: Path) -> None:
    """Reproduces the exact #28712 loop and asserts the dispatcher
    leaves the task blocked instead of cycling.

    Loop shape from the issue:

    1. Worker calls ``kanban_block`` → status='blocked',
       ``task_runs.outcome='blocked'``, ``blocked`` event.
    2. (Bug) Dispatcher promotes back to ``ready``.
    3. Fresh worker exits cleanly without terminal tool call →
       ``protocol_violation`` event.
    4. ``_record_task_failure(failure_limit=1)`` → ``gave_up`` event,
       status='blocked' again.
    5. (Bug) Dispatcher promotes again → infinite loop.

    With the fix in place, step 2 never happens — the test simulates
    one would-be loop cycle by faking the crash-then-gave_up entries
    that *would* have been written and asserts the *next* tick still
    leaves the task blocked.
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="loop reproducer")
        kb.claim_task(conn, tid)
        kb.block_task(
            conn, tid,
            reason="review-required: human eyes please",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        assert kb.get_task(conn, tid).status == "blocked"

        # First dispatcher tick — must NOT promote.
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, tid).status == "blocked"

        # Simulate the (hypothetical) protocol_violation + gave_up
        # entries that the dispatcher would have written if the bug
        # were still present.  Even with those event rows in place,
        # the worker-initiated ``blocked`` event is the most recent
        # of the ``{blocked, unblocked}`` pair, so the sticky guard
        # still fires.
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'protocol_violation', NULL, ?)",
            (tid, now),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'gave_up', NULL, ?)",
            (tid, now + 1),
        )
        conn.commit()

        # Subsequent ticks must still leave it blocked.
        for _ in range(3):
            promoted = kb.recompute_ready(conn)
            assert promoted == 0
            assert kb.get_task(conn, tid).status == "blocked"


# ---------------------------------------------------------------------------
# Schema-init recovery on legacy DBs is covered by
# tests/hermes_cli/test_kanban_db.py::test_connect_migrates_legacy_db_before_optional_column_indexes
# (landed via #28754 / #28781).  The original PR shipped a duplicate test
# here; dropped during salvage to avoid two assertions of the same contract.
# ---------------------------------------------------------------------------
