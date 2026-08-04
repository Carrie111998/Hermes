"""
Regression tests for the shared-container task_id mapping.

The top-level agent and all delegate_task subagents normally share a
terminal sandbox keyed by ``"default"``. Kanban workers instead share a
board-and-card-scoped sandbox with only their own delegates.
``_resolve_container_task_id`` is the sole gatekeeper for which tool-call
task_ids go to a shared container vs. get their own isolated sandbox.
RL / benchmark environments opt in to isolation by calling
``register_task_env_overrides(task_id, {...})`` before the agent loop;
other task_ids collapse back to the applicable shared key.

If you change the collapse logic, update both the helper and these
tests -- see `hermes-agent-dev` skill, "Why do subagents get their own
containers?" section, and the Container lifecycle paragraph under
Docker Backend in ``website/docs/user-guide/configuration.md``.
"""

import pytest

from tools import terminal_tool


@pytest.fixture(autouse=True)
def _clean_overrides():
    """Ensure no stray overrides from other tests leak in."""
    before = dict(terminal_tool._task_env_overrides)
    terminal_tool._task_env_overrides.clear()
    yield
    terminal_tool._task_env_overrides.clear()
    terminal_tool._task_env_overrides.update(before)


def test_none_task_id_maps_to_default():
    assert terminal_tool._resolve_container_task_id(None) == "default"


def test_empty_task_id_maps_to_default():
    assert terminal_tool._resolve_container_task_id("") == "default"


def test_kanban_worker_uses_board_and_card_scoped_container(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_a082045e")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")

    top_level_key = terminal_tool._resolve_container_task_id(None)

    assert top_level_key.startswith("kanban-")
    assert terminal_tool._resolve_container_task_id("delegate-1") == top_level_key

    monkeypatch.setenv("HERMES_KANBAN_BOARD", "other-board")
    assert terminal_tool._resolve_container_task_id(None) != top_level_key


def test_cwd_only_override_collapses_to_default():
    """CWD-only overrides (ACP adapter workspace tracking) must NOT trigger
    container isolation — they should collapse to the shared 'default'
    container so all surfaces (TUI, gateway, dashboard) share one sandbox.
    Regression for #37361."""
    terminal_tool.register_task_env_overrides(
        "acp-session-abc", {"cwd": "/home/user/project"}
    )
    try:
        assert (
            terminal_tool._resolve_container_task_id("acp-session-abc")
            == "default"
        )
    finally:
        terminal_tool.clear_task_env_overrides("acp-session-abc")


def test_env_type_override_keeps_own_id():
    """env_type is an isolation key — must trigger per-task container."""
    terminal_tool.register_task_env_overrides(
        "bench-env", {"env_type": "sandbox", "cwd": "/work"}
    )
    try:
        assert (
            terminal_tool._resolve_container_task_id("bench-env")
            == "bench-env"
        )
    finally:
        terminal_tool.clear_task_env_overrides("bench-env")
