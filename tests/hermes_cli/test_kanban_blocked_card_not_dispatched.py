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
