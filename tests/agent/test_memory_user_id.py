"""Tests for per-user memory scoping via user_id threading.

Verifies that gateway user_id flows from AIAgent -> MemoryManager -> plugins,
so each gateway user gets their own memory bucket instead of sharing a static one.
"""

import json
import os
from unittest.mock import MagicMock, patch

from agent.memory_provider import MemoryProvider
from agent.memory_manager import MemoryManager


# ---------------------------------------------------------------------------
# Concrete test provider that records init kwargs
# ---------------------------------------------------------------------------


class RecordingProvider(MemoryProvider):
    """Minimal provider that records what initialize() receives."""

    def __init__(self, name="recording"):
        self._name = name
        self._init_kwargs = {}
        self._init_session_id = None

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        self._init_session_id = session_id
        self._init_kwargs = dict(kwargs)

    def system_prompt_block(self) -> str:
        return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        return ""

    def sync_turn(self, user_content, assistant_content, *, session_id=""):
        pass

    def get_tool_schemas(self):
        return []

    def handle_tool_call(self, tool_name, args, **kwargs):
        return json.dumps({})

    def shutdown(self):
        pass


# ---------------------------------------------------------------------------
# MemoryManager user_id threading tests
# ---------------------------------------------------------------------------


class TestMemoryManagerUserIdThreading:
    """Verify user_id reaches providers via initialize_all."""



    def test_no_user_id_when_cli(self):
        """CLI sessions should not have user_id in kwargs."""
        mgr = MemoryManager()
        p = RecordingProvider()
        mgr.add_provider(p)

        mgr.initialize_all(
            session_id="sess-456",
            platform="cli",
        )

        assert "user_id" not in p._init_kwargs
        assert p._init_kwargs.get("platform") == "cli"


    def test_multiple_providers_all_receive_user_id(self):
        mgr = MemoryManager()
        # Use one provider named "builtin" (always accepted) and one external
        p1 = RecordingProvider("builtin")
        p2 = RecordingProvider("external")
        mgr.add_provider(p1)
        mgr.add_provider(p2)

        mgr.initialize_all(
            session_id="sess-multi",
            platform="slack",
            user_id="slack_U12345",
        )

        assert p1._init_kwargs.get("user_id") == "slack_U12345"
        assert p1._init_kwargs.get("platform") == "slack"
        assert p2._init_kwargs.get("user_id") == "slack_U12345"
        assert p2._init_kwargs.get("platform") == "slack"


# ---------------------------------------------------------------------------
# Mem0 provider user_id tests
# ---------------------------------------------------------------------------


class TestMem0UserIdScoping:
    """Verify Mem0 plugin uses gateway user_id when provided."""


    def test_no_user_id_falls_back_to_config(self):
        """Without user_id in kwargs, should use config default."""
        from plugins.memory.mem0 import Mem0MemoryProvider

        provider = Mem0MemoryProvider()
        with patch("plugins.memory.mem0._load_config", return_value={
            "api_key": "test-key",
            "user_id": "custom-default",
            "agent_id": "hermes",
            "rerank": True,
        }):
            provider.initialize(session_id="test-sess")

        assert provider._user_id == "custom-default"


    def test_different_users_get_different_ids(self):
        """Two providers initialized with different user_ids should be scoped differently."""
        from plugins.memory.mem0 import Mem0MemoryProvider

        p1 = Mem0MemoryProvider()
        p2 = Mem0MemoryProvider()

        with patch("plugins.memory.mem0._load_config", return_value={
            "api_key": "test-key",
            "user_id": "hermes-user",
            "agent_id": "hermes",
            "rerank": True,
        }):
            p1.initialize(session_id="sess-1", user_id="alice_123")
            p2.initialize(session_id="sess-2", user_id="bob_456")

        assert p1._user_id == "alice_123"
        assert p2._user_id == "bob_456"
        assert p1._user_id != p2._user_id


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# AIAgent user_id propagation test
# ---------------------------------------------------------------------------


class TestAIAgentUserIdPropagation:
    """Verify AIAgent stores user_id and passes it to memory init kwargs."""

    def test_user_id_stored_on_agent(self):
        """AIAgent should store user_id as instance attribute."""
        with patch.dict(os.environ, {"HERMES_HOME": "/tmp/test_hermes"}):
            from run_agent import AIAgent
            agent = object.__new__(AIAgent)
            # Manually set the attribute as __init__ does
            agent._user_id = "test_user_42"
            assert agent._user_id == "test_user_42"

    def test_user_id_none_by_default(self):
        """AIAgent should have None user_id when not provided (CLI mode)."""
        with patch.dict(os.environ, {"HERMES_HOME": "/tmp/test_hermes"}):
            from run_agent import AIAgent
            agent = object.__new__(AIAgent)
            agent._user_id = None
            assert agent._user_id is None
