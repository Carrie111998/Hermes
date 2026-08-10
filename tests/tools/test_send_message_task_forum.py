"""Behavior tests for Discord task-forum routing in send_message."""

from types import SimpleNamespace

import pytest

from gateway.config import Platform
from tools.send_message_tool import _send_to_platform


@pytest.mark.asyncio
async def test_static_delivery_to_task_forum_is_blocked():
    """Cron/lifecycle callers cannot create inert task-forum posts."""
    pconfig = SimpleNamespace(
        enabled=True,
        token="***",
        extra={"agent_task_forum_channels": ["tasks-parent"]},
    )

    result = await _send_to_platform(
        Platform.DISCORD,
        pconfig,
        "tasks-parent",
        "inert task",
    )

    assert "error" in result
    assert "task forum" in result["error"].lower()


@pytest.mark.asyncio
async def test_explicit_agent_task_uses_live_adapter(monkeypatch):
    """Agent-initiated work is routed to the live session-start path."""
    recorded = {}

    class Adapter:
        async def send(self, *, chat_id, content, metadata=None):
            recorded.update(chat_id=chat_id, content=content, metadata=metadata)
            return SimpleNamespace(
                success=True,
                message_id="starter",
                raw_response={"thread_id": "task-thread"},
            )

    runner = SimpleNamespace(adapters={Platform.DISCORD: Adapter()})
    monkeypatch.setattr("gateway.run._gateway_runner_ref", lambda: runner)
    pconfig = SimpleNamespace(
        enabled=True,
        token="***",
        extra={"agent_task_forum_channels": ["tasks-parent"]},
    )

    result = await _send_to_platform(
        Platform.DISCORD,
        pconfig,
        "tasks-parent",
        "real task",
        start_agent_task=True,
    )

    assert result["success"] is True
    assert result["thread_id"] == "task-thread"
    assert recorded["metadata"] == {"start_agent_task": True}
