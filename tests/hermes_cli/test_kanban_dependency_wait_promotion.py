"""Regression tests for t_4f14b90d — the dispatcher must not promote a
dependency-waiting card on a *vacuous* (empty) satisfied-parent set.

Bug shape (live repro: child ``t_7cf95f5a`` on parent ``t_3365a1dd``):

A worker declared a dependency wait via ``kanban_block(kind="dependency")``.
That correctly routes the child to ``todo`` (not the human ``blocked`` bucket)
and emits a ``dependency_wait`` event, relying on ``recompute_ready`` to gate
the card on its parent link. But that contract only holds while the parent
edge is intact. When a re-decompose / unlink->recompute sweep transiently
removed the child's parent link, ``recompute_ready``'s ``all(...)`` gate saw
ZERO parent rows — and ``all([])`` is vacuously True — so it promoted the card
``todo -> ready`` with ``satisfied_parent_ids: []``. The child was claimed,
its worker immediately re-declared the SAME ``dependency_wait``, and the card
thrashed on a ~5-10 minute respawn cadence, burning a worker every cycle with
zero forward progress (Prime-Directive violation).

Acceptance criteria pinned here:

1. A dependency-waiting child whose parent link has been churned away (EMPTY
   parent set) must NOT be promoted — no ``promoted`` event with
   ``satisfied_parent_ids: []`` may fire for it; it stays in ``todo``.
2. The dependency-waiting child DOES promote (exactly once) while its real
   ``done`` parent link is intact, recording that parent in
   ``satisfied_parent_ids`` — the guard must not deadlock a legitimately
   satisfiable card.
3. A genuinely ordinary ``todo`` card (never a dependency wait) is unaffected
   and still promotes when its parent set empties — we must not over-correct.
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


def _promoted_events(conn, task_id):
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'promoted' "
        "ORDER BY id",
        (task_id,),
    ).fetchall()
    return [json.loads(r["payload"]) for r in rows if r["payload"]]


def _make_dependency_waiting_child(conn):
    """Drive a child through the real lifecycle into a dependency wait.

    ``block_task`` only fires from ``ready``/``running``, and a child gated by a
    parent link only reaches ``ready`` once that parent is ``done``. So the only
    honest way to reach ``dependency_wait`` is: complete the parent (promoting
    the child), claim the child to ``running``, then have the worker declare the
    dependency wait. Returns ``(parent, child)`` where the parent is ``done``,
    the hard link is intact, and the child is in ``todo`` with ``dependency_wait``
    as its most-recent lifecycle event.

    Note the promotion counter: completing the parent promotes the child once
    (promotion #1). Tests that care count from there.
    """
    parent = kb.create_task(conn, title="parent: SSH adapter wiring")
    child = kb.create_task(conn, title="child: verify fail-closed",
                           parents=[parent])
    kb.claim_task(conn, parent)
    kb.complete_task(conn, parent, summary="parent done",
                     expected_run_id=kb.get_task(conn, parent).current_run_id)
    assert kb.get_task(conn, child).status == "ready"
    kb.claim_task(conn, child)
    assert kb.get_task(conn, child).status == "running"
    assert kb.block_task(
        conn, child, kind="dependency", reason="peer output not ready yet",
        expected_run_id=kb.get_task(conn, child).current_run_id,
    )
    assert kb.get_task(conn, child).status == "todo"
    assert kb._is_dependency_wait(conn, child)
    return parent, child


# ---------------------------------------------------------------------------
# Criterion 1 (the t_4f14b90d bug): a dependency-waiting child whose parent
# link has been churned away must NOT promote on the empty parent set.
# ---------------------------------------------------------------------------


def test_dependency_wait_child_not_promoted_on_empty_parent_set(kanban_home):
    with kb.connect() as conn:
        parent, child = _make_dependency_waiting_child(conn)
        promos_before = len(_promoted_events(conn, child))  # 1 from the helper

        # Simulate the re-decompose / unlink->recompute churn that removes the
        # child's parent edge. This is exactly the live-repro state: task_links
        # has ZERO rows for this child.
        assert kb.unlink_tasks(conn, parent, child)
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM task_links WHERE child_id = ?", (child,)
        ).fetchone()["n"] == 0
        # Unlink itself must not have re-promoted the dependency-waiting child.
        assert kb.get_task(conn, child).status == "todo"

        # Before the fix: all([]) == True -> promoted todo->ready with
        # satisfied_parent_ids: []. After the fix: a dependency wait with no
        # live parent is a broken link, NOT a satisfied gate -> stays in todo.
        for _ in range(5):
            assert kb.recompute_ready(conn) == 0
            assert kb.get_task(conn, child).status == "todo"

        promotions = _promoted_events(conn, child)
        assert len(promotions) == promos_before, (
            "a dependency-waiting card must never be promoted on an empty "
            f"parent set; new promotions={promotions[promos_before:]}"
        )
        # Belt-and-suspenders: no promotion ever recorded an empty satisfied set.
        assert all(p["satisfied_parent_ids"] for p in promotions), promotions


# ---------------------------------------------------------------------------
# Criterion 2: the guard does not deadlock — a dependency-waiting child with a
# real, intact 'done' parent link still promotes.
# ---------------------------------------------------------------------------


def test_dependency_wait_child_promotes_when_parent_link_intact(kanban_home):
    with kb.connect() as conn:
        parent, child = _make_dependency_waiting_child(conn)
        # Link is intact and parent is 'done'. The very act of completing the
        # parent in the helper already promoted the child once; assert that
        # promotion was real and attributed to the done parent.
        assert kb.get_task(conn, child).status in ("ready", "todo")

        promotions = _promoted_events(conn, child)
        assert len(promotions) >= 1, promotions
        first = promotions[0]
        assert first["trigger"] == "parents_terminal"
        assert first["satisfied_parent_ids"] == [parent], (
            "the real done parent must appear in satisfied_parent_ids"
        )

        # A fresh sweep on the still-todo card promotes it again (it was pushed
        # back to todo by the dependency block) BECAUSE the parent link is a
        # real 'done' parent — the guard only blocks the EMPTY-parent case.
        if kb.get_task(conn, child).status == "todo":
            assert kb.recompute_ready(conn) == 1
        assert kb.get_task(conn, child).status == "ready"


# ---------------------------------------------------------------------------
# Criterion 3: don't over-correct — an ordinary todo card that was never a
# dependency wait still promotes when its parent set empties.
# ---------------------------------------------------------------------------


def test_ordinary_todo_card_still_promotes_on_empty_parent_set(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        # Child gated in 'todo' by its parent link, but NEVER dependency-blocked
        # by a worker (no dependency_wait event in its history).
        tid = kb.create_task(conn, title="ordinary gated child", parents=[parent])
        assert kb.get_task(conn, tid).status == "todo"

        # The link is removed (e.g. the parent was pruned / the plan changed).
        # With no dependency_wait in its history, the empty parent set is a
        # legitimate vacuous-truth promotion and must still happen — the guard
        # must not over-correct ordinary todo cards.
        assert kb.unlink_tasks(conn, parent, tid)
        assert not kb._is_dependency_wait(conn, tid)

        # unlink_tasks may already recompute; either way the card must end ready.
        if kb.get_task(conn, tid).status == "todo":
            assert kb.recompute_ready(conn) == 1
        assert kb.get_task(conn, tid).status == "ready"
