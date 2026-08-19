from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from gateway.slash_commands import GatewaySlashCommandsMixin


class _Event:
    source = object()

    def get_command_args(self) -> str:
        return ""


def test_refine_reports_busy_instead_of_false_started_message() -> None:
    agent = SimpleNamespace(
        _session_messages=[{"role": "user", "content": "hello"}],
        valid_tool_names=set(),
        _spawn_background_review=MagicMock(return_value=False),
    )
    runner = object.__new__(GatewaySlashCommandsMixin)
    runner._session_key_for_source = lambda _source: "session"
    runner._running_agents = {}
    runner._agent_cache_lock = threading.Lock()
    runner._agent_cache = {"session": (agent, None)}

    result = asyncio.run(runner._handle_refine_command(_Event()))

    assert result == "A background review is already running — retry /refine shortly."
    agent._spawn_background_review.assert_called_once_with(
        messages_snapshot=agent._session_messages,
        review_memory=True,
        review_skills=False,
        focus=None,
        explicit_request=True,
    )
