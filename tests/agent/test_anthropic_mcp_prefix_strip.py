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

    def test_oauth_memory_alias_round_trips_to_registry_name(self):
        """``mcp__context_notes`` dispatches as ``memory`` (#65365 second trigger)."""
        transport = self._get_transport()
        block = _make_tool_use_block("mcp__context_notes")
        response = _make_response(block)

        registry = _FakeRegistry({"memory"})
        with patch("tools.registry.registry", registry):
            result = transport.normalize_response(response, strip_tool_prefix=True)

        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "memory"

    def test_bare_tool_name_from_prose_still_dispatches(self):
        """The model may follow the prose and emit the canonical name.

        ``memory``'s NAME is aliased on the wire but the system prompt still
        says "use the memory tool", so the model can emit ``mcp__memory``.
        The bare-name registry fallback must resolve it — this is the
        invariant that makes leaving memory's prose intact safe.
        """
        transport = self._get_transport()
        registry = _FakeRegistry({"memory", "session_search"})

        for wire, expected in (
            ("mcp__memory", "memory"),
            ("mcp__session_search", "session_search"),
        ):
            response = _make_response(_make_tool_use_block(wire))
            with patch("tools.registry.registry", registry):
                result = transport.normalize_response(response, strip_tool_prefix=True)
            assert result.tool_calls[0].name == expected, wire

    def test_registered_tool_wins_over_oauth_alias(self):
        """A real tool registered under the wire name keeps GH-25255 precedence.

        An MCP server tool named ``mcp_chat_history_lookup`` goes out as
        ``mcp__chat_history_lookup`` too. The alias must NOT hijack it — the
        registry lookup runs first, so the genuinely registered tool wins and
        the dispatcher doesn't silently run ``session_search`` instead.
        """
        transport = self._get_transport()
        block = _make_tool_use_block("mcp__chat_history_lookup")
        response = _make_response(block)

        registry = _FakeRegistry({"mcp_chat_history_lookup", "session_search"})
        with patch("tools.registry.registry", registry):
            result = transport.normalize_response(response, strip_tool_prefix=True)

        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "mcp_chat_history_lookup"






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

    def test_oauth_alias_yields_to_a_tool_that_owns_the_wire_name(self):
        """An alias must never produce a duplicate tool name.

        Two identical names in one request is a hard 400 from Anthropic, which
        would break every call — strictly worse than the classifier bug. If a
        real tool already maps onto the alias's wire name, that tool keeps it
        and the aliased tool stays un-aliased. Mirrors the inbound
        "registered tool wins" rule.
        """
        from agent.anthropic_adapter import _OAUTH_TOOL_NAME_ALIASES

        def _tool(name):
            return {"type": "function", "function": {
                "name": name, "description": "d", "parameters": {}}}

        kwargs = self._build([
            _tool("session_search"),
            _tool("mcp_chat_history_lookup"),   # real MCP tool owning the alias
            _tool("memory"),
            _tool("mcp_context_notes"),         # real MCP tool owning the alias
        ])

        names = [t["name"] for t in kwargs["tools"]]
        assert len(names) == len(set(names)), f"duplicate wire names: {names}"
        # The genuine tools keep the contested names...
        assert "mcp__chat_history_lookup" in names
        assert "mcp__context_notes" in names
        # ...and the alias sources fall back to their own prefixed names.
        for canonical in _OAUTH_TOOL_NAME_ALIASES:
            assert f"mcp__{canonical}" in names

    def test_prose_alias_names_are_a_subset_of_the_alias_map(self):
        """The prose set must only name tools that actually have an alias.

        Violating this raises KeyError at import; assert the contract by name
        so the failure is legible instead of a stack trace in a 3000-line
        module.
        """
        from agent.anthropic_adapter import (
            _OAUTH_PROSE_ALIAS_NAMES,
            _OAUTH_TOOL_NAME_ALIASES,
        )

        assert _OAUTH_PROSE_ALIAS_NAMES <= set(_OAUTH_TOOL_NAME_ALIASES)
        # An alias's wire name must not be another alias's canonical name, or
        # sequential prose substitution would chain-rewrite.
        assert not (
            set(_OAUTH_TOOL_NAME_ALIASES.values())
            & set(_OAUTH_TOOL_NAME_ALIASES)
        )

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

    def test_oauth_aliases_memory_tool_name_but_not_its_prose(self):
        """#65365's second trigger: the ``memory`` schema name is aliased.

        The name is what the classifier keys on, so it must not reach the wire.
        Its description and the system prompt keep the word "memory" — it is
        ordinary English there ("persistent memory across sessions"), and
        rewriting prose would hand the model mangled instructions.
        """
        kwargs = self._build(
            [{
                "type": "function",
                "function": {
                    "name": "memory",
                    "description": "Save durable facts to persistent memory.",
                    "parameters": {},
                },
            }],
            messages=[
                {"role": "system", "content": "You have persistent memory across sessions."},
                {"role": "user", "content": "Hi"},
            ],
        )

        assert kwargs["tools"][0]["name"] == "mcp__context_notes"
        # Prose is untouched — the tool still describes itself accurately.
        assert "persistent memory" in kwargs["tools"][0]["description"]
        system_text = " ".join(block["text"] for block in kwargs["system"])
        assert "persistent memory across sessions" in system_text

    def test_oauth_prose_alias_respects_word_boundaries(self):
        """A longer identifier containing the token must not be rewritten.

        System blocks carry user-supplied text (project AGENTS.md, memory
        snapshots). Rewriting ``session_search_tool.py`` would produce a path
        that doesn't exist and has no reverse mapping.
        """
        kwargs = self._build(
            [],
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Use session_search to recall. "
                        "Implementation lives in tools/session_search_tool.py."
                    ),
                },
                {"role": "user", "content": "Hi"},
            ],
        )

        system_text = " ".join(block["text"] for block in kwargs["system"])
        # Bare token aliased...
        assert "Use chat_history_lookup to recall." in system_text
        # ...but the longer identifier survives intact.
        assert "tools/session_search_tool.py" in system_text
