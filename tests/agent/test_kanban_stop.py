"""Tests for the kanban worker turn-end stop guard."""

from __future__ import annotations

import pytest

from agent.kanban_stop import (
    build_kanban_deadline_warning,
    build_kanban_stop_nudge,
    kanban_stop_nudge_enabled,
    session_called_kanban_terminal,
)


@pytest.fixture
def clear_kanban_env(monkeypatch):
    for var in (
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_STOP_NUDGE",
        "HERMES_KANBAN_RUNTIME_DEADLINE",
        "HERMES_KANBAN_RUNTIME_CAP_SECONDS",
    ):
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


def test_deadline_warning_is_disabled_by_default(clear_kanban_env, monkeypatch):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_deadline")
    clear_kanban_env.setenv("HERMES_KANBAN_RUNTIME_DEADLINE", "200")
    clear_kanban_env.setenv("HERMES_KANBAN_RUNTIME_CAP_SECONDS", "100")
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"deadline_warning_fraction": 0.0}},
    )

    assert build_kanban_deadline_warning(now=176) is None


def test_deadline_warning_requires_dispatcher_deadline(clear_kanban_env, monkeypatch):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_deadline")
    clear_kanban_env.setenv("HERMES_KANBAN_RUNTIME_CAP_SECONDS", "100")
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"deadline_warning_fraction": 0.75}},
    )

    assert build_kanban_deadline_warning(now=176) is None


def test_deadline_warning_ignores_non_dispatcher_context(clear_kanban_env, monkeypatch):
    from agent.delegation_context import non_dispatcher_owned_context

    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_deadline")
    clear_kanban_env.setenv("HERMES_KANBAN_RUNTIME_DEADLINE", "200")
    clear_kanban_env.setenv("HERMES_KANBAN_RUNTIME_CAP_SECONDS", "100")
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"deadline_warning_fraction": 0.75}},
    )

    with non_dispatcher_owned_context():
        assert build_kanban_deadline_warning(now=176) is None


def test_deadline_warning_fires_once_after_threshold_with_checkpoint(
    clear_kanban_env, monkeypatch,
):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_deadline")
    clear_kanban_env.setenv("HERMES_KANBAN_RUNTIME_DEADLINE", "200")
    clear_kanban_env.setenv("HERMES_KANBAN_RUNTIME_CAP_SECONDS", "100")
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "kanban": {
                "deadline_warning_fraction": 0.75,
                "safe_checkpoint": {"enabled": True},
            }
        },
    )

    nudge = build_kanban_deadline_warning(now=176)

    assert nudge == (
        "[System: You are past 75% of your runtime cap. Checkpoint now at a "
        "coherent boundary, or finish/block.]"
    )
    assert build_kanban_deadline_warning(issued=True, now=176) is None


def test_deadline_warning_waits_until_after_threshold_and_finishes_or_blocks(
    clear_kanban_env, monkeypatch,
):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_deadline")
    clear_kanban_env.setenv("HERMES_KANBAN_RUNTIME_DEADLINE", "200")
    clear_kanban_env.setenv("HERMES_KANBAN_RUNTIME_CAP_SECONDS", "100")
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "kanban": {
                "deadline_warning_fraction": 0.75,
                "safe_checkpoint": {"enabled": False},
            }
        },
    )

    assert build_kanban_deadline_warning(now=175) is None
    assert build_kanban_deadline_warning(now=176) == (
        "[System: You are past 75% of your runtime cap. Safe checkpointing is "
        "disabled; finish or block at a coherent boundary.]"
    )






# ── Integration: agent nudge + dispatcher bounded retry ──────────────
# These tests verify the two layers compose correctly: the agent-side
# nudge fires first (up to 2 attempts), and if the worker still exits
# without a terminal call, the dispatcher's bounded retry (streak of 3)
# handles it.  See also tests/hermes_cli/test_kanban_core_functionality.py
# for the dispatcher-side streak tests.

