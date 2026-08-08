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
from hermes_cli.kanban_decompose import list_triage_ids


def test_enabled_by_default_when_key_absent():
    enabled, per_tick = _resolve_auto_decompose_settings(lambda: {"kanban": {}})
    assert enabled is True
    assert per_tick == 3


def test_disabled_when_flag_false():
    enabled, per_tick = _resolve_auto_decompose_settings(
        lambda: {"kanban": {"auto_decompose": False}}
    )
    assert enabled is False


def test_gateway_auto_decompose_list_skips_github_draft_but_keeps_ordinary_triage(tmp_path, monkeypatch):
    """Draft PR ingestion stays parked while ordinary triage remains eligible."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    kb.init_db()
    with kb.connect() as conn:
        draft_id = kb.create_task(
            conn,
            title="Review PR #112: draft",
            initial_status="triage",
            metadata={
                "source": "github_pull_request",
                "draft": True,
                "repository": "solovisionllc/solofamilyplan",
                "number": 112,
            },
        )
        ordinary_id = kb.create_task(
            conn,
            title="Clarify the implementation request",
            initial_status="triage",
        )

    triage_ids = list_triage_ids()
    assert draft_id not in triage_ids
    assert ordinary_id in triage_ids


