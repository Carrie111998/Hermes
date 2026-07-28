"""Test that pre_tool_call plugin hooks can actually block tool execution.

The existing ``test_plugin_pre_tool_block_wins_without_counting_as_toolguard_block``
in ``test_tool_call_guardrail_runtime.py`` MOCKS ``resolve_pre_tool_block`` — it
never exercises the real ``invoke_hook()`` → callbacks → return-values path.

This test loads the ``test_pre_tool_call_block`` plugin, registers its hooks,
calls ``invoke_hook("pre_tool_call", ...)``, and asserts that a block directive
is returned when a terminal tool call is attempted.

Related:
  - https://github.com/NousResearch/hermes-agent/issues/73338
  - https://github.com/NousResearch/hermes-agent/issues/41045
"""

import json
import sys
from pathlib import Path

import pytest

# Skip if we can't import hermes_cli (CI may not have it)
hermes_cli = pytest.importorskip("hermes_cli.plugins")

from hermes_cli.plugins import (
    PluginManager,
    get_plugin_manager,
    invoke_hook,
    get_pre_tool_call_block_message,
)


@pytest.fixture(scope="module")
def _manager_with_test_plugin():
    """Discover and load plugins, ensuring test_pre_tool_call_block is loaded."""
    manager = get_plugin_manager()
    # Force re-discovery to pick up the test plugin
    manager.discover_and_load(force=True)
    return manager


def test_pre_tool_call_block_directive_collected_from_real_callback(
    _manager_with_test_plugin,
):
    """invoke_hook('pre_tool_call', ...) collects block directive from plugin callback.

    The test_pre_tool_call_block plugin's pre_block callback returns
    {"action": "block", "message": "BLOCKED by test-block plugin"} for ALL
    terminal tool calls. This test verifies that invoke_hook() correctly
    collects that return value.
    """
    # Call invoke_hook for a terminal tool
    results = invoke_hook(
        "pre_tool_call",
        tool_name="terminal",
        args={"command": "echo test"},
        task_id="test-task-1",
        session_id="test-session-1",
        tool_call_id="test-call-1",
    )

    # At least one result should be the block directive
    block_found = False
    for result in results:
        if isinstance(result, dict) and result.get("action") == "block":
            block_found = True
            assert "BLOCKED" in result.get("message", ""), (
                f"Expected block message, got: {result}"
            )
            break

    assert block_found, (
        f"invoke_hook('pre_tool_call') returned {len(results)} results, "
        f"none with action='block'. Results: {results}"
    )


def test_get_pre_tool_call_block_message_returns_block_for_terminal(
    _manager_with_test_plugin,
):
    """get_pre_tool_call_block_message() returns block message for terminal calls.

    This is the higher-level API that the agent runtime calls. It should
    aggregate invoke_hook results and return the first block message.
    """
    message = get_pre_tool_call_block_message(
        "terminal",
        {"command": "echo blocked"},
        task_id="test-task-2",
        session_id="test-session-2",
        tool_call_id="test-call-2",
    )

    assert message is not None, (
        "get_pre_tool_call_block_message() returned None for terminal call — "
        "block directive was not collected"
    )
    assert "BLOCKED" in message, f"Unexpected block message: {message}"


def test_safe_tool_not_blocked(_manager_with_test_plugin):
    """The test plugin only blocks 'terminal' — other tools pass through."""
    message = get_pre_tool_call_block_message(
        "web_search",
        {"query": "test"},
        task_id="test-task-3",
    )

    assert message is None, (
        f"Expected no block for web_search, got: {message}"
    )
