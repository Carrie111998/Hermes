"""Regression test for /refine gateway event-loop safety."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.slash_commands import GatewaySlashCommandsMixin


@pytest.mark.asyncio
async def test_refine_offloads_snapshot_and_thread_spawn_from_event_loop():
    runner = object.__new__(GatewaySlashCommandsMixin)
    runner._running_agents = {}
    runner._agent_cache_lock = MagicMock()
    runner._agent_cache_lock.__enter__.return_value = None
    runner._agent_cache_lock.__exit__.return_value = None

    agent = MagicMock()
    agent.valid_tool_names = {"skill_manage"}
    agent._session_messages = [{"role": "user", "content": "refine this"}]
    runner._agent_cache = {"session-key": agent}
    runner._session_key_for_source = MagicMock(return_value="session-key")

    event = MagicMock()
    event.source = SimpleNamespace()
    event.get_command_args.return_value = "capture the workflow"

    with patch.object(
        asyncio,
        "to_thread",
        new=AsyncMock(return_value="2026-08-11T12-00-00Z"),
    ) as to_thread:
        result = await runner._handle_refine_command(event)

    to_thread.assert_awaited_once_with(
        agent._spawn_background_review,
        messages_snapshot=agent._session_messages,
        review_memory=True,
        review_skills=True,
        focus="capture the workflow",
        snapshot_before_writes=True,
    )
    assert "Rollback snapshot" in result
