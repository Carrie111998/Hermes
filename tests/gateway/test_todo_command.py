"""Tests for the /todo gateway slash command.

Covers the registry contract (the command dispatches mid-run rather than
hitting the busy-reject catch-all) and the handler's agent-resolution
cascade: running agent → cached agent → no agent.
"""

import threading
from types import SimpleNamespace

import pytest

from hermes_cli.commands import (
    ACTIVE_SESSION_BYPASS_COMMANDS,
    COMMAND_REGISTRY,
    GATEWAY_KNOWN_COMMANDS,
)
from tools.todo_tool import TodoStore


def _todo_def():
    return next(c for c in COMMAND_REGISTRY if c.name == "todo")


def test_todo_is_gateway_known_with_aliases():
    for name in ("todo", "todos", "plan"):
        assert name in GATEWAY_KNOWN_COMMANDS


def test_todo_dispatches_while_agent_is_busy():
    # The list is most useful mid-turn, so /todo must not fall through to the
    # "Agent is running" busy-reject catch-all.
    assert _todo_def().busy_policy == "dispatch"
    assert "todo" in ACTIVE_SESSION_BYPASS_COMMANDS


def test_todo_has_no_busy_handler():
    # busy_policy="dispatch" with no busy_handler routes to the plain-handler
    # table in _dispatch_busy_slash_command, which is where "todo" is wired.
    assert _todo_def().busy_handler is None


# --- TodoStore.format_for_display -----------------------------------------


def test_format_for_display_empty_returns_none():
    assert TodoStore().format_for_display() is None


def test_format_for_display_keeps_completed_and_cancelled():
    # Unlike format_for_injection (which drops finished work so the model does
    # not redo it), the user-facing render must show progress.
    store = TodoStore()
    store.write(
        [
            {"id": "1", "content": "read source", "status": "completed"},
            {"id": "2", "content": "write test", "status": "in_progress"},
            {"id": "3", "content": "open PR", "status": "pending"},
            {"id": "4", "content": "abandoned idea", "status": "cancelled"},
        ]
    )
    out = store.format_for_display()
    assert "[x] read source" in out
    assert "[>] write test" in out
    assert "[ ] open PR" in out
    assert "[~] abandoned idea" in out
    assert "1/4 done" in out


def test_format_for_display_omits_injection_header():
    from tools.todo_tool import TODO_INJECTION_HEADER

    store = TodoStore()
    store.write([{"id": "1", "content": "task", "status": "pending"}])
    assert TODO_INJECTION_HEADER not in store.format_for_display()


# --- handler --------------------------------------------------------------


class _Runner:
    """Minimal stand-in exposing only what _handle_todo_command reads."""

    def __init__(self, running=None, cached=None):
        self._running_agents = running or {}
        self._agent_cache = cached
        self._agent_cache_lock = threading.Lock()

    def _session_key_for_source(self, source):
        return "sess"

    async def handle(self, event):
        from gateway.slash_commands import GatewaySlashCommandsMixin

        return await GatewaySlashCommandsMixin._handle_todo_command(self, event)


def _event():
    return SimpleNamespace(source=SimpleNamespace(platform=None))


def _agent_with(items):
    store = TodoStore()
    if items:
        store.write(items)
    return SimpleNamespace(_todo_store=store)


@pytest.mark.asyncio
async def test_no_resident_agent():
    out = await _Runner().handle(_event())
    assert "No task list yet" in out


@pytest.mark.asyncio
async def test_pending_sentinel_is_not_an_agent():
    from gateway.run import _AGENT_PENDING_SENTINEL

    runner = _Runner(running={"sess": _AGENT_PENDING_SENTINEL})
    out = await runner.handle(_event())
    assert "No task list yet" in out


@pytest.mark.asyncio
async def test_reads_the_running_agent():
    runner = _Runner(
        running={"sess": _agent_with([{"id": "1", "content": "ship it", "status": "in_progress"}])}
    )
    out = await runner.handle(_event())
    assert "[>] ship it" in out


@pytest.mark.asyncio
async def test_falls_back_to_cached_agent_between_turns():
    agent = _agent_with([{"id": "1", "content": "cached task", "status": "pending"}])
    runner = _Runner(cached={"sess": (agent, object())})
    out = await runner.handle(_event())
    assert "[ ] cached task" in out


@pytest.mark.asyncio
async def test_running_agent_wins_over_cache():
    runner = _Runner(
        running={"sess": _agent_with([{"id": "1", "content": "live", "status": "pending"}])},
        cached={"sess": (_agent_with([{"id": "1", "content": "stale", "status": "pending"}]), object())},
    )
    out = await runner.handle(_event())
    assert "live" in out
    assert "stale" not in out


@pytest.mark.asyncio
async def test_agent_without_todo_store():
    runner = _Runner(running={"sess": SimpleNamespace()})
    out = await runner.handle(_event())
    assert "No task list yet" in out


@pytest.mark.asyncio
async def test_empty_list_on_a_resident_agent():
    runner = _Runner(running={"sess": _agent_with([])})
    out = await runner.handle(_event())
    assert "No task list yet" in out
