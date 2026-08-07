"""Tests for the kanban worker turn-end stop guard."""

from __future__ import annotations

import pytest

from agent.kanban_stop import (
    build_kanban_stop_nudge,
    kanban_stop_nudge_enabled,
    session_called_kanban_terminal,
)


@pytest.fixture
def clear_kanban_env(monkeypatch):
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_STOP_NUDGE"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch






def test_env_can_disable(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    clear_kanban_env.setenv("HERMES_KANBAN_STOP_NUDGE", "0")
    assert kanban_stop_nudge_enabled() is False
    assert build_kanban_stop_nudge(messages=[]) is None


def test_nudge_when_no_terminal_tool(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_46be8aa5")
    messages = [
        {"role": "user", "content": "work kanban task"},
        {
            "role": "assistant",
            "content": "Let me write the comprehensive recipe.",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_heartbeat", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_heartbeat", "tool_call_id": "1", "content": "ok"},
    ]
    nudge = build_kanban_stop_nudge(messages=messages, attempts=0)
    assert nudge is not None
    assert "kanban_complete" in nudge
    assert "kanban_block" in nudge
    assert "t_46be8aa5" in nudge
    assert "protocol violation" in nudge.lower() or "protocol" in nudge.lower()


def test_no_nudge_after_kanban_complete(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_complete", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_complete", "tool_call_id": "1", "content": "done"},
    ]
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None






def test_no_nudge_in_delegated_child_context(clear_kanban_env):
    """#80507 — a delegate_task child inherits the worker's HERMES_KANBAN_TASK
    but cannot call kanban_complete/kanban_block (toolstrip + DB denial from
    #69837); the stop-guard must not nudge it into an unsatisfiable loop."""
    from agent.delegation_context import delegated_child_context

    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    with delegated_child_context():
        assert kanban_stop_nudge_enabled() is False
        assert (
            build_kanban_stop_nudge(
                messages=[{"role": "assistant", "content": "review complete"}]
            )
            is None
        )


def test_no_nudge_in_inprocess_cron_context(clear_kanban_env):
    """Sibling of #80507 — a cron job fired in-process from a worker is not
    the lifecycle owner either, so the guard must not nudge it."""
    from agent.delegation_context import non_dispatcher_owned_context

    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    with non_dispatcher_owned_context():
        assert kanban_stop_nudge_enabled() is False


def test_no_nudge_with_child_lineage_env_marker(clear_kanban_env):
    """Subprocesses spawned by a delegated child carry the
    HERMES_DELEGATED_CHILD_CONTEXT lineage marker; the guard must stay off."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    clear_kanban_env.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    assert kanban_stop_nudge_enabled() is False


def test_falsy_child_lineage_marker_keeps_guard_on(clear_kanban_env):
    """A lifecycle-owning worker that merely carries a falsy spelling of the
    lineage marker (e.g. HERMES_DELEGATED_CHILD_CONTEXT=0) must still be
    nudged — the #80507 exemption only applies to a set marker."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    clear_kanban_env.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "0")
    assert kanban_stop_nudge_enabled() is True


def test_worker_still_nudged_outside_child_context(clear_kanban_env):
    """Regression guard: the real dispatcher worker (the lifecycle owner)
    must still be nudged — the #80507 exemption must not weaken the guard."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    assert kanban_stop_nudge_enabled() is True
    assert (
        build_kanban_stop_nudge(messages=[{"role": "assistant", "content": "done"}])
        is not None
    )


# ── Integration: agent nudge + dispatcher bounded retry ──────────────
# These tests verify the two layers compose correctly: the agent-side
# nudge fires first (up to 2 attempts), and if the worker still exits
# without a terminal call, the dispatcher's bounded retry (streak of 3)
# handles it.  See also tests/hermes_cli/test_kanban_core_functionality.py
# for the dispatcher-side streak tests.




