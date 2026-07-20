"""Regression: one-shot (``hermes -z``) must wait for background MCP discovery
before building the agent, so slow stdio MCP servers aren't silently dropped.

See #68137: CLI startup kicks MCP discovery onto the ``cli-mcp-discovery``
background thread for the one-shot command, but ``_run_agent`` used to build
``AIAgent`` (which snapshots the tool registry) without joining that thread —
so tools from slow servers never reached the model. The fix mirrors the
interactive path by calling ``wait_for_mcp_discovery()`` first, guarded so a
discovery failure can't crash the one-shot run itself.
"""

from unittest.mock import MagicMock, patch

from hermes_cli import oneshot


def _run_agent_with_stubs(calls, wait_side_effect=None):
    """Invoke ``oneshot._run_agent`` with every collaborator stubbed, recording
    the order in which the MCP-discovery wait and agent construction happen in
    ``calls``.

    ``_run_agent`` imports its collaborators locally (to keep top-level CLI
    startup cheap), so we patch them at their *source* modules rather than as
    attributes of ``hermes_cli.oneshot``.
    """

    def _record_wait():
        calls.append("wait")

    def _make_agent(*_args, **_kwargs):
        calls.append("agent")
        agent = MagicMock()
        agent.run_conversation.return_value = {"final_response": "ok"}
        return agent

    runtime = {
        "api_key": "k",
        "base_url": "https://example.test",
        "provider": "openrouter",
        "api_mode": "openai",
        "credential_pool": None,
    }

    wait_kwargs = (
        {"side_effect": _record_wait} if wait_side_effect is None else {"side_effect": wait_side_effect}
    )

    with patch("hermes_cli.config.load_config", return_value={}), \
        patch("hermes_cli.models.detect_provider_for_model", return_value=None), \
        patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=runtime), \
        patch("hermes_cli.tools_config._get_platform_tools", return_value=set()), \
        patch("hermes_cli.oneshot.get_fallback_chain", return_value=[]), \
        patch("hermes_cli.oneshot._create_session_db_for_oneshot", return_value=None), \
        patch("hermes_cli.mcp_startup.wait_for_mcp_discovery", **wait_kwargs), \
        patch("run_agent.AIAgent", side_effect=_make_agent):
        return oneshot._run_agent("say hi", model="openrouter/some-model")


class TestOneshotWaitsForMcpDiscovery:
    def test_waits_before_building_agent(self):
        """``wait_for_mcp_discovery`` must be called *before* ``AIAgent`` is
        constructed, otherwise the tool registry is snapshotted without the
        slow MCP servers' tools."""
        calls = []
        _run_agent_with_stubs(calls)

        assert calls == ["wait", "agent"], (
            f"expected discovery wait before agent construction, got {calls!r}"
        )

    def test_proceeds_when_discovery_wait_raises(self):
        """A failure inside ``wait_for_mcp_discovery`` must not crash the
        one-shot run — the agent should still be built and the conversation
        should still proceed (mirroring the interactive path's resilience)."""
        calls = []

        def _boom_then_record_agent(*_a, **_k):
            calls.append("agent")
            agent = MagicMock()
            agent.run_conversation.return_value = {"final_response": "ok"}
            return agent

        runtime = {
            "api_key": "k",
            "base_url": "https://example.test",
            "provider": "openrouter",
            "api_mode": "openai",
            "credential_pool": None,
        }

        with patch("hermes_cli.config.load_config", return_value={}), \
            patch("hermes_cli.models.detect_provider_for_model", return_value=None), \
            patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=runtime), \
            patch("hermes_cli.tools_config._get_platform_tools", return_value=set()), \
            patch("hermes_cli.oneshot.get_fallback_chain", return_value=[]), \
            patch("hermes_cli.oneshot._create_session_db_for_oneshot", return_value=None), \
            patch("hermes_cli.mcp_startup.wait_for_mcp_discovery", side_effect=RuntimeError("boom")), \
            patch("run_agent.AIAgent", side_effect=_boom_then_record_agent):
            result = oneshot._run_agent("say hi", model="openrouter/some-model")

        # The agent was still built and a result returned despite the wait error.
        assert calls == ["agent"], f"expected agent construction despite wait failure, got {calls!r}"
        assert result is not None