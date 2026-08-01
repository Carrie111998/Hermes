"""Tests for live auto-decompose settings resolution (issue #49638).

The gateway dispatcher used to capture ``kanban.auto_decompose`` once at boot,
so a user who flipped it to ``false`` to STOP runaway auto-decompose (which had
created and launched tasks they didn't intend) found the flag had no effect
without a full gateway restart. ``_resolve_auto_decompose_settings`` is now
called every tick, reading the current config.
"""

from __future__ import annotations

import pytest

from gateway.kanban_watchers import _resolve_auto_decompose_settings
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_decompose as decomp


def test_enabled_by_default_when_key_absent():
    enabled, per_tick = _resolve_auto_decompose_settings(lambda: {"kanban": {}})
    assert enabled is True
    assert per_tick == 3


def test_disabled_when_flag_false():
    enabled, per_tick = _resolve_auto_decompose_settings(
        lambda: {"kanban": {"auto_decompose": False}}
    )
    assert enabled is False


def test_runtime_triage_scan_excludes_block_loop_recovery(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    kb.init_db()
    with kb.connect() as conn:
        fresh = kb.create_task(conn, title="fresh rough idea", triage=True)
        recovery = kb.create_task(conn, title="already executed", assignee="mike")
        assert kb.claim_task(conn, recovery, claimer="mike") is not None
        assert kb.block_task(conn, recovery, reason="review", kind="needs_input")
        assert kb.unblock_task(conn, recovery)
        assert kb.claim_task(conn, recovery, claimer="mike") is not None
        assert kb.block_task(conn, recovery, reason="review", kind="needs_input")

    assert decomp.list_triage_ids() == [fresh]


