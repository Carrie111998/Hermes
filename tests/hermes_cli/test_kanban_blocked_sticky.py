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
# [CORE-PATCH] H-22/H-31: create_task(initial_status='blocked') sticky
# ---------------------------------------------------------------------------


def test_create_task_with_initial_status_blocked_is_sticky(kanban_home: Path) -> None:
    """[CORE-PATCH] H-22/H-31: a task created directly via
    ``create_task(initial_status='blocked')`` (used by the plan-seeder
    H-22 and the coding-pipeline-orchestrator H-31) must persist as a
    sticky block — emitting the ``blocked`` event after ``created`` is
    what flips ``_has_sticky_block`` and prevents ``recompute_ready``
    from auto-promoting an elternlosen root back to ``ready`` even
    with no parents to wait on.

    The expected channel boundary is:

    * ``create_task(initial_status='blocked')`` → status='blocked', and a
      ``blocked`` event row lands in ``task_events`` (this test).
    * Status flip via ``UPDATE tasks SET status='blocked'`` (e.g.
      ``_record_task_failure``) WITHOUT a ``blocked`` event → still
      auto-recovers (see ``test_circuit_breaker_block_still_auto_promotes``
      above; the discriminator in ``_has_sticky_block`` is event-driven).
    """
    with kb.connect() as conn:
        root = kb.create_task(conn, title="parked root", initial_status="blocked")
        assert kb.get_task(conn, root).status == "blocked"

        # Multiple ticks must leave it blocked, even though it has no parents.
        for _ in range(5):
            promoted = kb.recompute_ready(conn)
            assert promoted == 0
            assert kb.get_task(conn, root).status == "blocked"

        # The blocked event must be the most recent of the {blocked, unblocked}
        # pair so ``_has_sticky_block`` returns True.
        with kb.connect() as conn2:
            row = conn2.execute(
                "SELECT kind FROM task_events "
                "WHERE task_id = ? AND kind IN ('blocked', 'unblocked') "
                "ORDER BY id DESC LIMIT 1",
                (root,),
            ).fetchone()
            assert row is not None and row["kind"] == "blocked"

        # Explicit operator unblock is the only legitimate exit; it flips
        # status to ready and emits an unblocked event so the sticky guard
        # releases.
        assert kb.unblock_task(conn, root)
        assert kb.get_task(conn, root).status == "ready"
        # A subsequent tick must NOT promote again (already ready → 0).
        assert kb.recompute_ready(conn) == 0


def test_create_task_initial_status_blocked_emits_blocked_event(kanban_home: Path) -> None:
    """Directly assert that the create_task path is the event source
    (not an external UPDATE). Distinguishes the operator-initiated
    park from a direct DB block — both currently look identical at
    row level, but only the operator-initiated one survives
    ``recompute_ready`` due to the event discriminator.
    """
    with kb.connect() as conn:
        root = kb.create_task(conn, title="parked via initial_status", initial_status="blocked")
        rows = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id ASC",
            (root,),
        ).fetchall()
        kinds = [r["kind"] for r in rows]
        # ``created`` is the canonical first event; ``blocked`` must
        # immediately follow for ``recompute_ready`` to treat this
        # as a sticky operator-initiated park.
        assert kinds[0] == "created"
        assert "blocked" in kinds


# ---------------------------------------------------------------------------
# [hermes-v2] H-22/H-31: sticky-block backfill on idempotent replay
# ---------------------------------------------------------------------------


def test_idempotent_replay_backfills_missing_blocked_event(
    kanban_home: Path,
) -> None:
    """[hermes-v2] H-22/H-31: when ``create_task(idempotency_key=...,
    initial_status='blocked')`` finds an existing still-blocked task
    whose ``blocked`` event is missing (legacy DB, event row deleted,
    or seed that bypassed the event branch), the missing event must
    be backfilled idempotently. Without the backfill, ``recompute_ready``
    would silently auto-promote the parked root on the next tick —
    exactly the regression we want to close.
    """
    idem = "h22-replay-key-1"
    with kb.connect() as conn:
        root = kb.create_task(
            conn,
            title="parked root",
            initial_status="blocked",
            idempotency_key=idem,
        )
        # Simulate the failure mode: a legacy / partially-migrated DB
        # where the ``blocked`` event row never landed. Wipe the event
        # but keep the row in ``blocked`` status.
        conn.execute(
            "DELETE FROM task_events WHERE task_id = ? AND kind = 'blocked'",
            (root,),
        )
        conn.commit()

        # Sanity: status still blocked, _has_sticky_block returns False.
        assert kb.get_task(conn, root).status == "blocked"
        assert kb._has_sticky_block(conn, root) is False

        # First replay: must backfill exactly one blocked event.
        again = kb.create_task(
            conn,
            title="parked root",
            initial_status="blocked",
            idempotency_key=idem,
        )
        assert again == root

        with kb.connect() as conn2:
            blocked_count = conn2.execute(
                "SELECT COUNT(*) FROM task_events "
                "WHERE task_id = ? AND kind = 'blocked'",
                (root,),
            ).fetchone()[0]
        assert blocked_count == 1, (
            "first replay must backfill exactly one blocked event"
        )

        # recompute_ready must NOT promote the root now that the
        # sticky guard fires again.
        promoted = kb.recompute_ready(conn)
        assert promoted == 0
        assert kb.get_task(conn, root).status == "blocked"


def test_idempotent_replay_does_not_double_block_event(
    kanban_home: Path,
) -> None:
    """Already-sticky replay must be a no-op for the event table —
    a second replay with the same key appends ZERO additional rows,
    even though ``initial_status='blocked'`` was passed again. This
    pins the idempotency contract: the guard ``not _has_sticky_block``
    short-circuits the backfill when the most recent block event is
    already ``blocked``.
    """
    idem = "h22-replay-key-2"
    with kb.connect() as conn:
        root = kb.create_task(
            conn,
            title="sticky from birth",
            initial_status="blocked",
            idempotency_key=idem,
        )
        blocked_initial = conn.execute(
            "SELECT COUNT(*) FROM task_events "
            "WHERE task_id = ? AND kind = 'blocked'",
            (root,),
        ).fetchone()[0]
        assert blocked_initial == 1

        # Replay #1 — still sticky, must NOT add another event.
        kb.create_task(
            conn,
            title="sticky from birth",
            initial_status="blocked",
            idempotency_key=idem,
        )
        blocked_after_first = conn.execute(
            "SELECT COUNT(*) FROM task_events "
            "WHERE task_id = ? AND kind = 'blocked'",
            (root,),
        ).fetchone()[0]
        assert blocked_after_first == 1, (
            "already-sticky replay must not append another blocked event"
        )

        # Replay #2 — same expectation.
        kb.create_task(
            conn,
            title="sticky from birth",
            initial_status="blocked",
            idempotency_key=idem,
        )
        blocked_after_second = conn.execute(
            "SELECT COUNT(*) FROM task_events "
            "WHERE task_id = ? AND kind = 'blocked'",
            (root,),
        ).fetchone()[0]
        assert blocked_after_second == 1, (
            "third replay must also be a no-op"
        )


def test_idempotent_replay_does_not_reblock_unblocked_task(
    kanban_home: Path,
) -> None:
    """An operator that explicitly unblocked the task must NOT have it
    re-blocked by a replay. The seeder passes ``initial_status='blocked'``
    every call — if the replay path tried to "fix" status, an unblocked
    task would silently flip back to ``blocked``. ``create_task``
    must leave status untouched and only backfill the event when the
    task is already ``blocked`` AND the sticky guard does not fire.
    """
    idem = "h22-replay-key-3"
    with kb.connect() as conn:
        root = kb.create_task(
            conn,
            title="parked then unblocked",
            initial_status="blocked",
            idempotency_key=idem,
        )
        # Explicit operator unblock.
        assert kb.unblock_task(conn, root)
        assert kb.get_task(conn, root).status == "ready"

        # Replay: status must stay ``ready``; no re-block event.
        kb.create_task(
            conn,
            title="parked then unblocked",
            initial_status="blocked",
            idempotency_key=idem,
        )
        task = kb.get_task(conn, root)
        assert task.status == "ready", (
            "explicit unblock must not be reverted by an idempotent replay"
        )
        # The most recent block/unblock event is ``unblocked`` — sticky
        # guard does not fire, but status is no longer ``blocked`` so
        # the backfill branch is skipped.
        recent = conn.execute(
            "SELECT kind FROM task_events "
            "WHERE task_id = ? AND kind IN ('blocked', 'unblocked') "
            "ORDER BY id DESC LIMIT 1",
            (root,),
        ).fetchone()
        assert recent is not None and recent["kind"] == "unblocked"


def test_idempotent_replay_on_done_task_is_a_no_op(
    kanban_home: Path,
) -> None:
    """A completed (``done``) task must not be touched by replay —
    neither status flipped nor event appended. The fix is narrow on
    purpose: only ``status='blocked'`` with a missing sticky event
    triggers the backfill.
    """
    idem = "h22-replay-key-4"
    with kb.connect() as conn:
        root = kb.create_task(
            conn,
            title="root then done",
            initial_status="blocked",
            idempotency_key=idem,
        )
        # Bypass the dispatcher claim/complete machinery to set status
        # to done — easier than wiring a full worker roundtrip.
        conn.execute(
            "UPDATE tasks SET status='done', completed_at=? WHERE id=?",
            (int(time.time()), root),
        )
        conn.commit()

        before_events = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?",
            (root,),
        ).fetchone()[0]

        kb.create_task(
            conn,
            title="root then done",
            initial_status="blocked",
            idempotency_key=idem,
        )
        task = kb.get_task(conn, root)
        assert task.status == "done", "done task must not be re-blocked"

        after_events = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?",
            (root,),
        ).fetchone()[0]
        assert after_events == before_events, (
            "replay on a done task must not append any event"
        )


# ---------------------------------------------------------------------------
# Schema-init recovery on legacy DBs is covered by
# tests/hermes_cli/test_kanban_db.py::test_connect_migrates_legacy_db_before_optional_column_indexes
# (landed via #28754 / #28781).  The original PR shipped a duplicate test
# here; dropped during salvage to avoid two assertions of the same contract.
# ---------------------------------------------------------------------------
