"""Regression test: a card created with ``initial_status='blocked'`` must NOT
be claimed by the auto-dispatch mechanism.

The bug: ``create_task`` with ``initial_status='blocked'`` writes
``status='blocked'`` into the DB row but emits only a ``"created"`` event
(not a ``"blocked"`` event).  Because ``_has_sticky_block()`` looks for a
``"blocked"`` event, it returns ``False``, so the dispatcher's
``recompute_ready()`` promotes the task to ``ready`` and ``dispatch_once()``
claims + spawns it — defeating the entire point of creating a blocked card.

Acceptance:
  - test fails (task gets promoted/claimed) with current code
  - test passes (task stays blocked, no spawn) after the fix
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


# ── fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def all_assignees_spawnable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Assume every assignee is a real Hermes profile so the dispatcher
    does not skip them as nonspawnable."""
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


def test_initial_status_blocked_card_is_not_dispatched(
    kanban_home: Path,
    all_assignees_spawnable: None,
) -> None:
    """A card created with ``initial_status='blocked'`` must stay blocked
    after a full ``dispatch_once`` cycle.

    Before the fix, ``create_task`` did not emit a ``"blocked"`` event for
    ``initial_status='blocked'``, so ``_has_sticky_block()`` returned False,
    ``recompute_ready()`` promoted it to ``ready``, and ``dispatch_once()``
    claimed it — a blocked card that gets dispatched.
    """
    with kb.connect() as conn:
        # Create a blocked card with an assignee so the dispatcher can
        # attempt to spawn it.
        tid = kb.create_task(
            conn,
            title="must not be dispatched",
            assignee="test-profile",
            initial_status="blocked",
        )
        assert kb.get_task(conn, tid).status == "blocked"

        # Track whether spawn_fn was ever called.
        spawn_calls: list[str] = []

        def spy_spawn(task, workspace_path, board=None):
            spawn_calls.append(getattr(task, "id", str(task)))
            return 999999  # fake PID

        # Run one full dispatch tick — this calls recompute_ready then
        # attempts to claim any ready tasks and spawn them.
        result = kb.dispatch_once(conn, spawn_fn=spy_spawn)

        # The blocked card must NOT have been promoted or claimed.
        task = kb.get_task(conn, tid)
        assert task.status == "blocked", (
            f"Blocked card must stay blocked after dispatch_once; "
            f"got status={task.status!r} "
            f"(spawned={result.spawned!r})"
        )
        assert len(result.spawned) == 0, (
            f"dispatch_once must not spawn any task for a blocked card; "
            f"got spawned={result.spawned!r}"
        )
        assert len(spawn_calls) == 0, (
            f"spawn_fn must not have been called for a blocked card; "
            f"calls={spawn_calls!r}"
        )


def test_initial_status_blocked_card_not_promoted_by_recompute_ready(
    kanban_home: Path,
) -> None:
    """Even the intermediate ``recompute_ready`` step must not promote a
    card that was created with ``initial_status='blocked'``.

    This isolates the promotion half of the bug from the claim/spawn half,
    making it easier to tell which layer failed.
    """
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="blocked-promotion-test",
            assignee="test-profile",
            initial_status="blocked",
        )
        assert kb.get_task(conn, tid).status == "blocked"

        # Hammer recompute_ready, same as the dispatcher does every tick.
        for _ in range(5):
            promoted = kb.recompute_ready(conn)
            assert promoted == 0, (
                f"recompute_ready must not promote a blocked card; "
                f"got promoted={promoted}"
            )
            assert kb.get_task(conn, tid).status == "blocked"


# ── helpers for extended tests ───────────────────────────────────────────


def _spy_spawn():
    """Return a ``spawn_fn`` that records calls + returns a fake PID."""
    calls: list[str] = []

    def spy(task, workspace_path, board=None):
        calls.append(getattr(task, "id", str(task)))
        return 999999  # fake PID

    spy.calls = calls  # type: ignore[attr-defined]
    return spy


def _blocked_event_count(conn, task_id: str) -> int:
    """Count ``blocked`` events in ``task_events`` for ``task_id``."""
    rows = conn.execute(
        "SELECT COUNT(*) AS cnt FROM task_events "
        "WHERE task_id = ? AND kind = 'blocked'",
        (task_id,),
    ).fetchone()
    return rows["cnt"] if rows else 0


def _block_gate_audit_count(conn, task_id: str) -> int:
    """Count ``block_gate_audit`` events in ``task_events`` for ``task_id``."""
    rows = conn.execute(
        "SELECT COUNT(*) AS cnt FROM task_events "
        "WHERE task_id = ? AND kind = 'block_gate_audit'",
        (task_id,),
    ).fetchone()
    return rows["cnt"] if rows else 0


# ── extended regression: block_task after creation + dispatch ────────────


@pytest.mark.parametrize("kind", ["needs_input", "capability", "transient", None])
def test_block_task_blocks_card_from_dispatch(
    kanban_home: Path,
    all_assignees_spawnable: None,
    kind: str | None,
) -> None:
    """A card created as ``running`` then blocked via ``block_task`` must NOT
    be dispatched — tests ``kanban_block`` path for all ``VALID_BLOCK_KINDS``.
    """
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title=f"block-after-creation-{kind}",
            assignee="test-profile",
            initial_status="running",
        )
        # Without parents, initial_status="running" creates the task as
        # "ready" (immediately dispatchable).  Block it to test the gate.
        assert kb.get_task(conn, tid).status == "ready"

        prev_events = _blocked_event_count(conn, tid)
        kb.block_task(conn, tid, reason="deliberate test block", kind=kind)
        assert _blocked_event_count(conn, tid) == prev_events + 1, (
            f"block_task must emit a 'blocked' event for kind={kind!r}"
        )
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"

        # Dispatch must not claim it
        spy = _spy_spawn()
        result = kb.dispatch_once(conn, spawn_fn=spy)
        task = kb.get_task(conn, tid)
        assert task.status == "blocked", (
            f"Blocked card must stay blocked after dispatch_once; "
            f"got status={task.status!r} (kind={kind!r})"
        )
        assert len(result.spawned) == 0, (
            f"dispatch_once must not spawn a blocked card; "
            f"got spawned={result.spawned!r} (kind={kind!r})"
        )


# ── extended regression: blind-spot guard ────────────────────────────────


def test_blind_spot_blocked_status_without_blocked_event(
    kanban_home: Path,
    all_assignees_spawnable: None,
) -> None:
    """A card with ``status='blocked'`` set directly in the DB without a
    corresponding ``'blocked'`` event must also NOT be dispatched.

    This tests the blind-spot guard (``90d03e99``): ``recompute_ready``
    must not auto-promote ``status='blocked'`` rows even when no matching
    ``'blocked'`` event exists.
    """
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="blind-spot-blocked",
            assignee="test-profile",
            initial_status="running",
        )
        assert kb.get_task(conn, tid).status == "ready"

        # Directly set status to 'blocked' WITHOUT emitting any event.
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))

        # The blind-spot guard in recompute_ready must NOT promote it.
        for i in range(3):
            promoted = kb.recompute_ready(conn)
            assert promoted == 0, (
                f"Blind-spot guard failed: recompute_ready promoted a "
                f"blocked card without a blocked event (iteration {i})"
            )
            task = kb.get_task(conn, tid)
            assert task.status == "blocked", (
                f"Card bypassed blind-spot guard: status changed "
                f"to {task.status!r} (iteration {i})"
            )

        # Full dispatch tick must also keep it blocked.
        spy = _spy_spawn()
        result = kb.dispatch_once(conn, spawn_fn=spy)
        task = kb.get_task(conn, tid)
        assert task.status == "blocked", (
            f"Blind-spot card must stay blocked after dispatch; "
            f"got status={task.status!r}"
        )
        assert len(result.spawned) == 0


# ── extended regression: dependency block auto-recovery ──────────────────


def test_dependency_block_still_auto_recovers_via_todo(
    kanban_home: Path,
    all_assignees_spawnable: None,
) -> None:
    """A ``dependency``-kind blocked card whose parent completes must still
    auto-recover via ``todo`` → ``ready`` promotion.

    This is the intentional auto-recovery path — the fix must not break it.
    """
    with kb.connect() as conn:
        parent_id = kb.create_task(conn, title="parent", assignee="parent-profile")
        kb.complete_task(conn, parent_id, result="parent done")
        assert kb.get_task(conn, parent_id).status == "done"

        child_id = kb.create_task(
            conn,
            title="dependency-child",
            assignee="test-profile",
            parents=[parent_id],
        )
        # Parent is already done, so child starts as ready.
        # Block with dependency kind → routes to todo.
        kb.block_task(conn, child_id, reason="waiting on parent", kind="dependency")
        status = kb.get_task(conn, child_id).status
        assert status == "todo", (
            f"dependency-blocked card must route to 'todo'; got {status!r}"
        )

        # Parent is done → recompute_ready promotes the child.
        promoted = kb.recompute_ready(conn)
        assert promoted >= 1, (
            "recompute_ready must promote the dependency child; parent is done"
        )
        assert kb.get_task(conn, child_id).status == "ready"


# ── extended regression: unblock_task is the only exit ───────────────────


def test_unblock_task_is_the_only_exit(
    kanban_home: Path,
    all_assignees_spawnable: None,
) -> None:
    """Calling ``unblock_task`` must emit an ``unblocked`` event and return
    the card to the ready/todo pool, making it dispatachable again.
    """
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="unblock-exit-test",
            assignee="test-profile",
            initial_status="blocked",
        )
        assert kb.get_task(conn, tid).status == "blocked"

        # Must NOT be dispatched while blocked
        spy = _spy_spawn()
        result = kb.dispatch_once(conn, spawn_fn=spy)
        assert len(result.spawned) == 0

        # Unblock
        kb.unblock_task(conn, tid)
        task = kb.get_task(conn, tid)
        assert task.status in ("ready", "todo"), (
            f"After unblock_task, card must exit blocked; got {task.status!r}"
        )

        # Now dispatch must pick it up
        result = kb.dispatch_once(conn, spawn_fn=spy)
        assert len(result.spawned) > 0, (
            "After unblock, card must be dispatchable"
        )
        # Verify an 'unblocked' event was emitted
        rows = conn.execute(
            "SELECT COUNT(*) AS cnt FROM task_events "
            "WHERE task_id = ? AND kind = 'unblocked'",
            (tid,),
        ).fetchone()
        assert rows and rows["cnt"] >= 1, (
            "unblock_task must emit at least one 'unblocked' event"
        )
