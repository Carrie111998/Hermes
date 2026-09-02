"""Authoritative, typed Kanban completion-veto tests.

The hook is deliberately exercised through ``kanban_db.complete_task`` rather
than a shell command.  A caller that can reach the core transition must not be
able to bypass an enrolled completion policy by choosing a different surface.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.plugins import get_plugin_manager


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_before_kanban_task_complete_veto_blocks_core_api(kanban_home):
    """A typed hook veto must preserve the task's pre-completion state."""
    manager = get_plugin_manager()
    saved = {key: list(value) for key, value in manager._hooks.items()}
    observed = []

    def veto(**payload):
        observed.append(payload)
        return {"allow": False, "reason": "production receipt missing"}

    manager._hooks.setdefault("before_kanban_task_complete", []).append(veto)
    try:
        conn = kb.connect()
        try:
            task_id = kb.create_task(conn, title="delivery-v2 canary", assignee="implementer")
            assert kb.complete_task(
                conn,
                task_id,
                summary="attempt without receipt",
                metadata={"delivery_v2": {"task_class": "PRODUCTION_CHANGE"}},
                actor="implementer",
            ) is False
            assert kb.get_task(conn, task_id).status == "ready"
        finally:
            conn.close()
    finally:
        manager._hooks = saved

    assert len(observed) == 1
    payload = observed[0]
    assert payload["task_id"] == task_id
    assert payload["actor"] == "implementer"
    assert payload["task"].id == task_id
    assert payload["metadata"] == {"delivery_v2": {"task_class": "PRODUCTION_CHANGE"}}


def test_before_kanban_task_complete_veto_blocks_cli(kanban_home, monkeypatch, capsys):
    """The direct CLI reaches the same typed policy boundary as the API."""
    monkeypatch.setenv("HERMES_PROFILE", "  CLI-VERIFIER ")
    manager = get_plugin_manager()
    saved = {key: list(value) for key, value in manager._hooks.items()}
    observed = []

    def veto(**payload):
        observed.append(payload)
        return "independent receipt required"

    manager._hooks.setdefault("before_kanban_task_complete", []).append(veto)
    try:
        conn = kb.connect()
        try:
            task_id = kb.create_task(conn, title="cli canary", assignee="implementer")
        finally:
            conn.close()

        from hermes_cli.kanban import _cmd_complete

        args = argparse.Namespace(
            task_ids=[task_id],
            result=None,
            summary="CLI bypass attempt",
            metadata=json.dumps({"delivery_v2": {"task_class": "PRODUCTION_CHANGE"}}),
        )
        assert _cmd_complete(args) == 1
        assert "cannot complete" in capsys.readouterr().err

        conn = kb.connect()
        try:
            assert kb.get_task(conn, task_id).status == "ready"
        finally:
            conn.close()
    finally:
        manager._hooks = saved

    assert observed[0]["actor"] == "cli-verifier"


def test_malformed_pre_completion_decision_fails_closed(kanban_home):
    """A plugin cannot accidentally allow completion with an ambiguous reply."""
    manager = get_plugin_manager()
    saved = {key: list(value) for key, value in manager._hooks.items()}
    manager._hooks.setdefault("before_kanban_task_complete", []).append(
        lambda **_: {"allow": "yes"}
    )
    try:
        conn = kb.connect()
        try:
            task_id = kb.create_task(conn, title="malformed policy", assignee="implementer")
            assert kb.complete_task(conn, task_id, summary="attempt") is False
            assert kb.get_task(conn, task_id).status == "ready"
            events = conn.execute(
                "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
                (task_id,),
            ).fetchall()
        finally:
            conn.close()
    finally:
        manager._hooks = saved

    assert events[-1]["kind"] == "completion_blocked_policy"
    assert "malformed" in json.loads(events[-1]["payload"])["reason"]
