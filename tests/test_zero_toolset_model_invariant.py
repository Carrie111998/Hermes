"""Zero-tool sessions must own no tool at the model layer either.

``enabled_toolsets=[]`` is an explicit barrier: a client that opened a session
without tools must never see a tool schema reach the model. The kanban
lifecycle injection (``HERMES_KANBAN_TASK``) deliberately re-adds its toolset
to a restricted selection, but a selection of NOTHING is a barrier, not a cost
optimization, so the barrier wins.

These tests run the real toolset pipeline (registry + toolsets + model_tools)
and a real ``AIAgent``: mocking the tool definitions would prove nothing about
what actually reaches the model.
"""

from __future__ import annotations

import os
from unittest.mock import patch


def _tool_names(enabled):
    from model_tools import get_tool_definitions

    definitions = get_tool_definitions(enabled_toolsets=enabled, quiet_mode=True)
    return [tool["function"]["name"] for tool in definitions]


class TestKanbanInjectionRespectsTheBarrier:
    def test_empty_selection_stays_empty_under_kanban_task(self) -> None:
        with patch.dict(os.environ, {"HERMES_KANBAN_TASK": "task-42"}):
            assert _tool_names([]) == []

    def test_empty_selection_is_empty_without_kanban_task(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "HERMES_KANBAN_TASK"}
        with patch.dict(os.environ, env, clear=True):
            assert _tool_names([]) == []

    def test_restricted_selection_still_gets_kanban_tools(self) -> None:
        with patch.dict(os.environ, {"HERMES_KANBAN_TASK": "task-42"}):
            names = _tool_names(["todo"])
        assert any(name.startswith("kanban_") for name in names), names

    def test_absent_selection_is_unaffected(self) -> None:
        with patch.dict(os.environ, {"HERMES_KANBAN_TASK": "task-42"}):
            assert len(_tool_names(None)) > 0


class TestAgentBuildsWithNoTool:
    def _agent(self, enabled_toolsets):
        from run_agent import AIAgent

        return AIAgent(
            model="test/model",
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            provider="openrouter",
            enabled_toolsets=enabled_toolsets,
            max_iterations=2,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            # Keep the build side-effect free: trajectory dumps write outside
            # the test's temp home.
            save_trajectories=False,
        )

    def test_agent_has_no_tool_under_kanban_task(self) -> None:
        with patch.dict(
            os.environ,
            {"HERMES_KANBAN_TASK": "task-42", "OPENROUTER_API_KEY": "test-key"},
        ):
            agent = self._agent([])
        assert agent.tools == []
        assert agent.valid_tool_names == set()

    def test_agent_without_selection_has_tools(self) -> None:
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            agent = self._agent(None)
        assert len(agent.tools) > 0
