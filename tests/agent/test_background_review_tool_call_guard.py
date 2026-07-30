"""The background-review fork must not spawn when it could only no-op.

The review fork's whole job is to emit ``memory`` / ``skill_manage`` tool calls,
and by default it inherits the parent's live runtime. When the parent provider IS
an autonomous agent reached through a client shim that cannot carry Hermes tool
calls back, that fork is a guaranteed no-op — one that still pays for a full
agent spawn (a JVM, for Junie) on every review cadence.

So: a client declaring ``SUPPORTS_HERMES_TOOL_CALLS = False`` skips the fork with
a log line pointing at the ``auxiliary.background_review`` override; a client that
can emit tool calls (including junie-acp via the ACP text bridge) is unaffected,
as are ordinary providers whose clients say nothing at all.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import agent.background_review as bg  # noqa: E402


def _fake_parent(client) -> SimpleNamespace:
    """The minimal parent-agent surface _run_review_in_thread touches pre-fork."""
    agent = SimpleNamespace(
        provider="junie-acp",
        model="junie-acp",
        client=client,
        session_id="s1",
        platform="cli",
        request_overrides={},
        max_tokens=None,
        acp_command="junie",
        acp_args=["--acp=true"],
        enabled_toolsets=None,
        disabled_toolsets=None,
        reasoning_config=None,
        _credential_pool=None,
        _current_main_runtime=lambda: {"api_key": "k", "base_url": "acp://junie", "api_mode": "chat_completions"},
        _emit_auxiliary_failure=lambda *_a, **_k: None,
        _safe_print=lambda *_a, **_k: None,
        background_review_callback=None,
    )
    return agent


def _run(agent):
    """Run the worker with AIAgent patched; return the AIAgent mock."""
    with (
        patch("hermes_cli.config.load_config", return_value={}),
        patch("run_agent.AIAgent") as mock_aiagent,
        patch("tools.terminal_tool.set_approval_callback"),
    ):
        bg._run_review_in_thread(agent, [{"role": "user", "content": "hi"}], "review please")
    return mock_aiagent


def test_fork_is_skipped_when_the_provider_cannot_emit_tool_calls():
    client = MagicMock()
    client.SUPPORTS_HERMES_TOOL_CALLS = False
    mock_aiagent = _run(_fake_parent(client))
    mock_aiagent.assert_not_called()


def test_fork_is_spawned_when_the_provider_can_emit_tool_calls():
    client = MagicMock()
    client.SUPPORTS_HERMES_TOOL_CALLS = True
    mock_aiagent = _run(_fake_parent(client))
    assert mock_aiagent.called


def test_ordinary_providers_are_unaffected():
    # A plain OpenAI-style client says nothing about the capability.
    class _PlainClient:
        pass

    mock_aiagent = _run(_fake_parent(_PlainClient()))
    assert mock_aiagent.called


def test_forwarded_tools_cover_the_review_forks_whitelist():
    """The review fork can only act with tools that reach the provider.

    ``_run_review_in_thread`` whitelists the memory + skills toolsets. Every one
    of those names must be in the junie-acp forwarded set — a review that can
    call ``skill_manage`` but not ``skills_list``/``skill_view`` can only create
    skills blindly instead of updating the right existing one.
    """
    pytest.importorskip("acp")
    from model_tools import get_tool_definitions

    from agent.junie_acp_client import _resolve_forwarded_tools

    whitelist = {
        t["function"]["name"]
        for t in get_tool_definitions(enabled_toolsets=["memory", "skills"], quiet_mode=True)
    }
    forwarded = _resolve_forwarded_tools()
    assert forwarded is not None
    missing = whitelist - forwarded
    assert not missing, (
        f"review-fork tools not forwarded to junie-acp: {sorted(missing)} — "
        "add them to _DEFAULT_FORWARDED_TOOLS"
    )


def test_junie_client_declares_support():
    pytest.importorskip("acp")
    from agent.junie_acp_client import JunieACPClient

    # The text bridge is what makes this True — see the client's docstring.
    assert JunieACPClient.SUPPORTS_HERMES_TOOL_CALLS is True
    assert bg._parent_can_emit_tool_calls(_fake_parent(JunieACPClient.__new__(JunieACPClient))) is True
