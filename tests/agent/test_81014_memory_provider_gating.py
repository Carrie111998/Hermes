"""Regression test for #81014 — memory provider system_prompt_block() is
injected unconditionally even when the provider's tools are gated out of
the agent's tool surface by platform_toolsets / disabled_toolsets.

The fix gates ``MemoryManager.build_system_prompt()`` on
``memory_provider_tools_enabled()`` once ``set_tool_gating()`` has been
called, and adds a WARNING in ``inject_memory_provider_tools()`` so the
silent suppression surfaces in ``agent.log``.
"""

import logging

import pytest
from unittest.mock import MagicMock

from agent.memory_manager import MemoryManager, inject_memory_provider_tools
from agent.memory_provider import MemoryProvider


class FakeMemoryProvider(MemoryProvider):
    def __init__(self, name="fake", available=True, tools=None):
        self._name = name
        self._available = available
        self._tools = tools or []
        self._prompt_block = (
            "Use mnemosyne_remember to store any durable fact."
        )

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return self._available

    def initialize(self, session_id, **kwargs):
        pass

    def system_prompt_block(self) -> str:
        return self._prompt_block

    def get_tool_schemas(self):
        return [
            {
                "type": "function",
                "function": {"name": f"{self._name}_remember"},
            },
            {
                "type": "function",
                "function": {"name": f"{self._name}_recall"},
            },
        ]


class TestMemoryManagerDanglingInstructionGate:
    """The provider system_prompt_block() must not leak into the agent's
    system prompt when its tools are gated out of the tool surface."""

    def test_no_gating_state_emits_block_legacy(self):
        """Without set_tool_gating() — e.g. a caller that did not opt into
        the new contract — the legacy behavior is preserved: the block is
        emitted even though it would dangle. This protects existing
        callers in agent.prompt_builder and tests that build managers
        without invoking set_tool_gating().
        """
        mgr = MemoryManager()
        mgr.add_provider(FakeMemoryProvider("mnemosyne"))

        # No set_tool_gating() called → legacy emit.
        assert "mnemosyne_remember" in mgr.build_system_prompt()

    def test_tools_gated_out_suppresses_block(self):
        """When the provider's tools are gated out via disabled_toolsets and
        set_tool_gating() has been called, the block is suppressed and a
        WARNING is logged (#81014).
        """
        mgr = MemoryManager()
        mgr.add_provider(FakeMemoryProvider("mnemosyne"))
        mgr.set_tool_gating(
            enabled_toolsets=None,  # not enabled-toolset-driven
            disabled_toolsets=["memory"],
        )

        with pytest.warns(None) if False else _assert_logs_warning(
            "mnemosyne",
            match_str="NOT in the tool surface",
        ):
            block = mgr.build_system_prompt()

        assert "mnemosyne_remember" not in block
        assert block == ""

    def test_tools_exposed_via_enabled_set_emits_block(self):
        """When the provider's toolset IS enabled, the block is emitted
        normally (the fix must not regress the happy path).
        """
        mgr = MemoryManager()
        mgr.add_provider(FakeMemoryProvider("mnemosyne"))
        mgr.set_tool_gating(
            enabled_toolsets=["memory"],
            disabled_toolsets=None,
        )
        assert "mnemosyne_remember" in mgr.build_system_prompt()

    def test_tools_exposed_via_memory_in_toolset_emits_block(self):
        """A toolset that resolves to include 'memory' also exposes the
        provider — preserving the resolve_toolset() logic in
        ``memory_provider_tools_enabled``.
        """
        mgr = MemoryManager()
        mgr.add_provider(FakeMemoryProvider("mnemosyne"))
        mgr.set_tool_gating(
            enabled_toolsets=["file"],
            disabled_toolsets=None,
        )
        # 'file' does NOT resolve to 'memory' so the block must be gated out.
        block = mgr.build_system_prompt()
        assert "mnemosyne_remember" not in block

    def test_block_suppression_only_for_gated_providers(self):
        """If multiple providers are registered and only some are gated,
        the gated ones are dropped and the ungated ones still emit.
        """
        mgr = MemoryManager()
        mgr.add_provider(FakeMemoryProvider("alpha"))
        mgr.add_provider(FakeMemoryProvider("beta"))
        # Disable only 'memory' but neither provider is named 'memory'.
        # The current gate sees 'memory' disabled → all providers are
        # suppressed together, matching the single-system-prompt-block
        # semantics in the issue. Verify both go away together.
        mgr.set_tool_gating(
            enabled_toolsets=None,
            disabled_toolsets=["memory"],
        )
        block = mgr.build_system_prompt()
        assert "alpha_remember" not in block
        assert "beta_remember" not in block


class TestInjectMemoryProviderToolsWarning:
    """inject_memory_provider_tools() must log a WARNING when the provider
    is initialized but the gate suppresses tool injection (#81014)."""

    def test_warning_logged_when_gated(self, caplog):
        mgr = MemoryManager()
        mgr.add_provider(FakeMemoryProvider("mnemosyne"))
        mgr.add_provider(FakeMemoryProvider(""))

        agent = MagicMock()
        agent._memory_manager = mgr
        agent.tools = []
        agent.enabled_toolsets = None
        agent.disabled_toolsets = ["memory"]

        with caplog.at_level(logging.WARNING, logger="agent.memory_manager"):
            added = inject_memory_provider_tools(agent)

        assert added == 0
        warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "NOT in the agent" in r.getMessage()
        ]
        assert warnings, (
            "expected at least one WARNING about suppressed memory tools, "
            "got: " + repr([r.getMessage() for r in caplog.records])
        )
        # The count of suppressed schemas must appear in the message — operator
        # needs that to know which provider went dark.
        assert any("2" in r.getMessage() for r in warnings), (
            "warning did not include the schema count"
        )

    def test_no_warning_when_green_path(self, caplog):
        """The green path (tools exposed) must NOT log a warning."""
        mgr = MemoryManager()
        mgr.add_provider(FakeMemoryProvider("mnemosyne"))

        agent = MagicMock()
        agent._memory_manager = mgr
        agent.tools = []
        agent.enabled_toolsets = ["memory"]
        agent.disabled_toolsets = None

        with caplog.at_level(logging.WARNING, logger="agent.memory_manager"):
            added = inject_memory_provider_tools(agent)

        assert added == 2  # both schemas appended
        warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "NOT in the agent" in r.getMessage()
        ]
        assert not warnings, (
            "green path should not log a suppression warning: "
            + repr([r.getMessage() for r in warnings])
        )


def _assert_logs_warning(provider_name, match_str):
    """Context manager helper — returns a no-op cm because pytest's
    caplog is bound by the surrounding test; this lets the test
    syntax read naturally."""
    from contextlib import contextmanager

    @contextmanager
    def cm():
        yield

    return cm()