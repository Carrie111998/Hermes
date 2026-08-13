"""CLI dispatch honours the global emergency stop (``hermes pause``).

The ESTOP sentinel gates the gateway kanban watcher, but a manual
``hermes kanban dispatch`` from a shell goes through
``kanban_db.dispatch_once`` directly. These tests pin the contract that
the CLI path is bound by the estop too: housekeeping still runs, but no
task is claimed or spawned while the sentinel is engaged, and dispatch
resumes normally once it is lifted.
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest


@pytest.fixture()
def isolated_kanban_home(monkeypatch):
    """Fresh HERMES_HOME with a kanban DB and a default profile."""
    test_home = tempfile.mkdtemp(prefix="kanban_estop_test_")
    os.makedirs(os.path.join(test_home, "profiles", "default"), exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if (mod.startswith("hermes_cli") or mod.startswith("hermes_state")
                or mod.startswith("agent.estop") or mod == "hermes_constants"):
            del sys.modules[mod]
    from hermes_cli import kanban_db
    from agent import estop
    estop._reset_log_state_for_tests()
    yield kanban_db, estop
    estop.disengage()


def _fake_spawn(*args, **kwargs):
    return 12345


def test_engaged_estop_blocks_cli_dispatch_spawns(isolated_kanban_home):
    kb, estop = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(conn, title="t0", assignee="default")
    estop.engage("test: estop must bind the CLI dispatch path")
    assert estop.is_engaged()
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert res.spawned == [], (
        "CLI dispatch spawned a worker while the estop was engaged"
    )


def test_disengaged_estop_restores_cli_dispatch(isolated_kanban_home):
    kb, estop = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(conn, title="t0", assignee="default")
    estop.engage("engage then lift")
    estop.disengage()
    assert not estop.is_engaged()
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    assert len(res.spawned) == 1, "dispatch did not resume after estop lift"
