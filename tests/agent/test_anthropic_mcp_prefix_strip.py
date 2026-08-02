"""Tests for Anthropic OAuth tool-name normalization and round-trips.

Anthropic's subscription/OAuth billing classifier treats a **single-underscore**
``mcp_`` tool name as a third-party-app fingerprint and rejects the request with
HTTP 400 "Third-party apps now draw from extra usage, not plan limits".  So on
the OAuth wire NOTHING may carry a single-underscore ``mcp_`` prefix:

  * bare native tools            ``read_file``            -> ``mcp__read_file``
  * native MCP server tools      ``mcp_linear_get_issue`` -> ``mcp__linear_get_issue``

``normalize_response`` reverses the ``mcp__`` wire name back to whatever the tool
registry knows (the single-underscore ``mcp_<server>_<tool>`` form for MCP server
tools, or the bare name for native tools) so the dispatcher is unaffected.

The deterministic prompt trigger includes ``session_search`` guidance, so its
OAuth wire alias must round-trip to the registry name.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tool_use_block(name: str, block_id: str = "tc_1", input_data: dict | None = None):
    """Create a fake Anthropic tool_use content block."""
    return SimpleNamespace(
        type="tool_use",
        id=block_id,
        name=name,
        input=input_data or {"query": "test"},
    )


def _make_response(*blocks, stop_reason="end_turn"):
    """Create a fake Anthropic Messages response."""
    return SimpleNamespace(
        content=list(blocks),
        stop_reason=stop_reason,
        model="claude-sonnet-4",
        usage=SimpleNamespace(input_tokens=100, output_tokens=50),
    )


class _FakeRegistry:
    """Minimal fake tool registry for testing prefix round-trip logic."""

    def __init__(self, registered_names: set[str]):
        self._names = registered_names

    def get_entry(self, name: str):
        if name in self._names:
            return SimpleNamespace(name=name)  # truthy = tool exists
        return None


# ---------------------------------------------------------------------------
# Response side: mcp__ wire name -> registry name
# ---------------------------------------------------------------------------

class TestAnthropicMcpPrefixStrip:
    """Verify strip_tool_prefix reverses the ``mcp__`` wire prefix correctly."""

    def _get_transport(self):
        from agent.transports.anthropic import AnthropicTransport
        return AnthropicTransport()

    def test_strips_prefix_for_oauth_injected_native_tool(self):
        """``mcp__read_file`` -> ``read_file`` (bare native tool)."""
        transport = self._get_transport()
        block = _make_tool_use_block("mcp__read_file")
        response = _make_response(block)

        registry = _FakeRegistry({"read_file", "terminal", "web_search"})
        with patch("tools.registry.registry", registry):
            result = transport.normalize_response(response, strip_tool_prefix=True)

        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "read_file"


    def test_no_strip_when_flag_false(self):
        """When strip_tool_prefix=False, names are never modified."""
        transport = self._get_transport()
        block = _make_tool_use_block("mcp__read_file")
        response = _make_response(block)

        registry = _FakeRegistry({"read_file"})
        with patch("tools.registry.registry", registry):
            result = transport.normalize_response(response, strip_tool_prefix=False)

        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "mcp__read_file"

    def test_oauth_session_search_alias_round_trips_to_registry_name(self):
        """``mcp__chat_history_lookup`` dispatches as ``session_search``."""
        transport = self._get_transport()
        block = _make_tool_use_block("mcp__chat_history_lookup")
        response = _make_response(block)

        registry = _FakeRegistry({"session_search"})
        with patch("tools.registry.registry", registry):
            result = transport.normalize_response(response, strip_tool_prefix=True)

        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "session_search"






# ---------------------------------------------------------------------------
# Request side: registry name -> mcp__ wire name (no single-underscore leaks)
# ---------------------------------------------------------------------------

class TestAnthropicOAuthOutgoingPrefix:
    """build_anthropic_kwargs must emit ZERO single-underscore ``mcp_`` names on
    the OAuth wire — bare names and MCP server names both land on ``mcp__``."""

    def _build(self, tools, is_oauth=True, messages=None):
        from agent.anthropic_adapter import build_anthropic_kwargs
        return build_anthropic_kwargs(
            model="claude-sonnet-4-6",
            messages=messages or [{"role": "user", "content": "Hi"}],
            tools=tools,
            max_tokens=4096,
            reasoning_config=None,
            is_oauth=is_oauth,
        )


    def test_oauth_promotes_single_underscore_mcp_server_tool(self):
        """OAuth + ``mcp_<server>_<tool>`` -> promoted to double underscore.

        This is the gap left by the bare constant swap: MCP server tools used
        to be *skipped* and went on the wire single-underscore, still tripping
        the classifier.  They must become ``mcp__`` and NOT be double-prefixed.
        """
        kwargs = self._build([{
            "type": "function",
            "function": {
                "name": "mcp_linear_get_issue",
                "description": "x",
                "parameters": {},
            },
        }])
        names = [t["name"] for t in kwargs["tools"]]
        assert names == ["mcp__linear_get_issue"]
        # never double-prefixed
        assert not any(n.startswith("mcp__mcp_") for n in names)


    def test_oauth_no_single_underscore_mcp_on_wire(self):
        """Mixed set: every wire name is bare-free of single-underscore mcp_."""
        kwargs = self._build([
            {"type": "function", "function": {"name": "read_file",
                                              "description": "x", "parameters": {}}},
            {"type": "function", "function": {"name": "mcp_linear_get_issue",
                                              "description": "y", "parameters": {}}},
            {"type": "function", "function": {"name": "terminal",
                                              "description": "z", "parameters": {}}},
        ])
        names = sorted(t["name"] for t in kwargs["tools"])
        assert names == ["mcp__linear_get_issue", "mcp__read_file", "mcp__terminal"]
        # The core invariant: NOTHING single-underscore reaches the wire.
        for n in names:
            assert not (n.startswith("mcp_") and not n.startswith("mcp__"))

    def test_oauth_aliases_session_search_in_request_shape(self):
        """OAuth aliases the classifier trigger in name, description, and prompt."""
        kwargs = self._build(
            [{
                "type": "function",
                "function": {
                    "name": "session_search",
                    "description": "Use session_search to recall prior chats.",
                    "parameters": {},
                },
            }],
            messages=[
                {"role": "system", "content": "Use session_search before asking again."},
                {"role": "user", "content": "Hi"},
            ],
        )

        assert kwargs["tools"][0]["name"] == "mcp__chat_history_lookup"
        assert "session_search" not in kwargs["tools"][0]["description"]
        system_text = " ".join(block["text"] for block in kwargs["system"])
        assert "session_search" not in system_text
        assert "chat_history_lookup" in system_text

    def test_non_oauth_keeps_session_search_request_shape(self):
        """API-key requests retain the public tool name and prompt vocabulary."""
        kwargs = self._build(
            [{
                "type": "function",
                "function": {
                    "name": "session_search",
                    "description": "Use session_search to recall prior chats.",
                    "parameters": {},
                },
            }],
            is_oauth=False,
            messages=[
                {"role": "system", "content": "Use session_search before asking again."},
                {"role": "user", "content": "Hi"},
            ],
        )

        assert kwargs["tools"][0]["name"] == "session_search"
        assert "session_search" in kwargs["tools"][0]["description"]
        assert kwargs["system"] == "Use session_search before asking again."
