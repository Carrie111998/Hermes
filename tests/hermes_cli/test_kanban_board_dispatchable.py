"""P-71 — board-scoped dispatch guard (hermes-v2).

Regression tests for the 2026-07-20 spawn-storm: the gateway dispatcher
auto-assigned + spawned 22 workers on a *planning* board because
``kanban.default_assignee`` fills in an assignee for unassigned ready
tasks and there was no per-board switch to hold a checklist board back.

The guard: a board is dispatched only when its ``board.json`` carries
``dispatchable: true``. FAIL-CLOSED — every board (and any board with a
missing/malformed metadata file) defaults to non-dispatchable.
"""
from __future__ import annotations

import sys
import tempfile

import pytest


@pytest.fixture()
def isolated_kanban_home(monkeypatch):
    """Fresh HERMES_HOME with a clean kanban DB (mirrors the
    default-assignee regression fixture)."""
    test_home = tempfile.mkdtemp(prefix="kanban_dispatchable_test_")
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if (
            mod.startswith("hermes_cli")
            or mod.startswith("hermes_state")
            or mod == "hermes_constants"
        ):
            del sys.modules[mod]
    from hermes_cli import kanban_db
    yield kanban_db, test_home


def _fake_spawn(*args, **kwargs):
    return 12345


# --- schema / helper -------------------------------------------------------

def test_dispatchable_defaults_false_fail_closed(isolated_kanban_home):
    kb, _home = isolated_kanban_home
    kb.create_board(slug="chk", name="Checklist")
    # No dispatchable key written yet → fail-closed.
    assert kb.board_is_dispatchable("chk") is False
    assert kb.read_board_metadata("chk")["dispatchable"] is False


def test_set_dispatch_toggles_and_persists(isolated_kanban_home):
    kb, _home = isolated_kanban_home
    kb.create_board(slug="work", name="Worker")
    kb.write_board_metadata("work", dispatchable=True)
    assert kb.board_is_dispatchable("work") is True
    kb.write_board_metadata("work", dispatchable=False)
    assert kb.board_is_dispatchable("work") is False


def test_dispatchable_preserved_across_unrelated_metadata_write(isolated_kanban_home):
    """Setting an unrelated field (default_workdir) must not clobber the
    dispatch opt-in — the value round-trips through read_board_metadata."""
    kb, _home = isolated_kanban_home
    kb.create_board(slug="work", name="Worker")
    kb.write_board_metadata("work", dispatchable=True)
    kb.write_board_metadata("work", default_workdir="/tmp/x")
    assert kb.board_is_dispatchable("work") is True


def test_missing_board_metadata_is_fail_closed(isolated_kanban_home):
    kb, _home = isolated_kanban_home
    # Board that was never created / has no board.json at all.
    assert kb.board_is_dispatchable("does-not-exist") is False


# --- behavioural: the incident vector -------------------------------------

def test_incident_repro_nondispatchable_board_never_spawns(isolated_kanban_home):
    """THE regression: a ready task with no assignee + default_assignee set
    is exactly what spawned 22 workers. With respect_dispatchable=True on a
    non-dispatchable board the tick is a complete no-op: no auto-assign, no
    spawn, DB row untouched."""
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Checklist")  # dispatchable defaults False
        task_id = kb.create_task(conn, title="t1", assignee=None)
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            default_assignee="default",
            respect_dispatchable=True,
        )
    assert not res.spawned
    assert not res.auto_assigned_default
    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT assignee FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    assert row["assignee"] is None


def test_dispatchable_board_spawns_as_before(isolated_kanban_home):
    """Same task on a board explicitly opted in → dispatches normally."""
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Worker")
        task_id = kb.create_task(conn, title="t1", assignee=None)
    kb.write_board_metadata("default", dispatchable=True)
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            default_assignee="default",
            respect_dispatchable=True,
        )
    assert res.auto_assigned_default == [task_id]
    assert len(res.spawned) == 1
    assert res.spawned[0][0] == task_id


def test_respect_dispatchable_default_off_keeps_legacy_behavior(isolated_kanban_home):
    """Backward-compat guard: with respect_dispatchable omitted (default
    False) the guard is inert — every existing caller/test that expects a
    spawn on a fresh board keeps working, even without a dispatchable flag."""
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Legacy")
        task_id = kb.create_task(conn, title="t1", assignee=None)
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, dry_run=False,
            default_assignee="default",
        )
    assert res.auto_assigned_default == [task_id]
    assert len(res.spawned) == 1
