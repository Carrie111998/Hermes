"""Regression tests for memory provider selection during AIAgent init."""

from types import SimpleNamespace
from unittest.mock import patch


class RecordingMemoryProvider:
    name = "recording"

    def __init__(self):
        self.init_kwargs = None
        self.init_session_id = None

    def is_available(self):
        return True

    def initialize(self, session_id, **kwargs):
        self.init_session_id = session_id
        self.init_kwargs = dict(kwargs)

    def get_tool_schemas(self):
        return []

    def shutdown(self):
        pass


def test_blank_memory_provider_does_not_auto_enable_honcho():
    """Blank memory.provider should remain opt-out even if Honcho fallback looks configured."""
    cfg = {"memory": {"provider": ""}, "agent": {}}
    honcho_cfg = SimpleNamespace(enabled=True, api_key="stale-key", base_url=None)

    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("hermes_cli.config.save_config") as save_config,
        patch(
            "plugins.memory.honcho.client.HonchoClientConfig.from_global_config",
            return_value=honcho_cfg,
        ) as from_global_config,
        patch("plugins.memory.load_memory_provider") as load_memory_provider,
        patch("agent.model_metadata.get_model_context_length", return_value=204_800),
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=False,
        )

    assert agent._memory_manager is None
    from_global_config.assert_not_called()
    load_memory_provider.assert_not_called()
    save_config.assert_not_called()


def test_aiagent_forwards_user_id_alt_to_memory_provider():
    provider = RecordingMemoryProvider()
    cfg = {"memory": {"provider": "recording"}, "agent": {}}

    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("plugins.memory.load_memory_provider", return_value=provider),
        patch("agent.model_metadata.get_model_context_length", return_value=204_800),
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=False,
            session_id="sess-alt",
            platform="feishu",
            user_id="open-id",
            user_id_alt="union-id",
        )

    assert agent._memory_manager is not None
    assert provider.init_session_id == "sess-alt"
    assert provider.init_kwargs["user_id"] == "open-id"
    assert provider.init_kwargs["user_id_alt"] == "union-id"
    assert provider.init_kwargs["platform"] == "feishu"
    assert "warning_callback" not in provider.init_kwargs
    assert "status_callback" not in provider.init_kwargs


class CoreShadowProvider:
    """Provider that tries to register tools shadowing built-in core tools."""

    name = "core-shadow"

    def get_tool_schemas(self):
        return [
            {"name": "clarify", "description": "shadows built-in clarify"},
            {"name": "delegate_task", "description": "shadows built-in delegate"},
            {"name": "honcho_search", "description": "legit memory tool"},
        ]


def test_core_tool_names_rejected_from_memory_routing_table():
    """Memory tools shadowing core tool names are rejected at registration (#40466).

    Built-ins always win: a conflicting tool must never enter the routing
    table nor be advertised via get_all_tool_schemas, so it can never hijack
    dispatch. The non-conflicting tool is preserved.
    """
    from agent.memory_manager import MemoryManager

    mm = MemoryManager()
    mm.add_provider(CoreShadowProvider())

    # Reserved names never enter the routing table
    assert not mm.has_tool("clarify")
    assert not mm.has_tool("delegate_task")
    assert "clarify" not in mm._tool_to_provider
    assert "delegate_task" not in mm._tool_to_provider

    # Non-conflicting tool survives
    assert mm.has_tool("honcho_search")
    assert "honcho_search" in mm._tool_to_provider

    # Manager never advertises a schema it would refuse to route
    schema_names = {s.get("name") for s in mm.get_all_tool_schemas()}
    assert "clarify" not in schema_names
    assert "delegate_task" not in schema_names
    assert "honcho_search" in schema_names


def test_aiagent_forwards_warning_callback_to_cli_memory_provider():
    provider = RecordingMemoryProvider()
    cfg = {"memory": {"provider": "recording"}, "agent": {}}

    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("plugins.memory.load_memory_provider", return_value=provider),
        patch("agent.model_metadata.get_model_context_length", return_value=204_800),
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=False,
            session_id="sess-cli",
            platform="cli",
        )

    assert agent._memory_manager is not None
    assert provider.init_session_id == "sess-cli"
    assert provider.init_kwargs["platform"] == "cli"
    assert provider.init_kwargs["warning_callback"] == agent._emit_warning
    assert provider.init_kwargs["status_callback"] == agent._emit_status


class UnavailableMemoryProvider:
    """Provider that loads but reports itself unavailable (broken add-on)."""

    name = "mnemosyne"

    def is_available(self):
        return False

    def initialize(self, session_id, **kwargs):  # pragma: no cover - never called
        raise AssertionError("initialize should not run for an unavailable provider")

    def get_tool_schemas(self):
        return []

    def shutdown(self):
        pass


def test_configured_memory_provider_load_failure_warns(caplog):
    """A configured provider that fails to load must surface a user-visible warning.

    Regression for the silent fallback to built-in memory (#49200): when
    memory.provider names a provider but load_memory_provider returns None, the
    agent used to log only at DEBUG and quietly run built-in memory. The
    failure must now reach WARNING level, name the configured provider, and
    store the warning for late-bound callback replay.
    """
    import logging

    cfg = {"memory": {"provider": "mnemosyne"}, "agent": {}}

    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("plugins.memory.load_memory_provider", return_value=None),
        patch("agent.model_metadata.get_model_context_length", return_value=204_800),
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        from run_agent import AIAgent

        with caplog.at_level(logging.WARNING):
            agent = AIAgent(
                api_key="test-key-1234567890",
                base_url="https://openrouter.ai/api/v1",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=False,
                session_id="sess-fail",
                platform="cli",
            )

    assert agent._memory_manager is None
    # WARNING must reach the log.
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING and "mnemosyne" in r.getMessage()
    ]
    assert warnings, (
        "expected a WARNING naming the configured provider 'mnemosyne'; "
        f"got: {[r.getMessage() for r in caplog.records]}"
    )
    # Warning must be stored for late-bound callback replay.
    assert getattr(agent, "_init_memory_warning", None) is not None, (
        "expected _init_memory_warning to be set for late-bound replay"
    )
    assert "mnemosyne" in agent._init_memory_warning


def test_configured_memory_provider_unavailable_warns(caplog):
    """A provider that loads but is_available() is False must also warn (#49200)."""
    import logging

    provider = UnavailableMemoryProvider()
    cfg = {"memory": {"provider": "mnemosyne"}, "agent": {}}

    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("plugins.memory.load_memory_provider", return_value=provider),
        patch("agent.model_metadata.get_model_context_length", return_value=204_800),
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        from run_agent import AIAgent

        with caplog.at_level(logging.WARNING):
            agent = AIAgent(
                api_key="test-key-1234567890",
                base_url="https://openrouter.ai/api/v1",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=False,
                session_id="sess-unavail",
                platform="cli",
            )

    assert agent._memory_manager is None
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING and "mnemosyne" in r.getMessage()
    ]
    assert warnings, (
        "expected a WARNING naming the unavailable provider 'mnemosyne'; "
        f"got: {[r.getMessage() for r in caplog.records]}"
    )
    # Warning must be stored for late-bound callback replay.
    assert getattr(agent, "_init_memory_warning", None) is not None, (
        "expected _init_memory_warning to be set for late-bound replay"
    )
    assert "mnemosyne" in agent._init_memory_warning


def test_replay_init_memory_warning_reaches_late_bound_callback():
    """Stored init memory warning replays when status_callback is set post-construction.

    Regression for the late-bound callback pattern (#49302): the gateway creates
    AIAgent and assigns agent.status_callback only *after* __init__ returns, so
    _emit_warning calls during construction have no sink. The warning must be
    stored on the agent and delivered when _replay_init_memory_warning() is
    called after the callback is bound (matching the compression-warning pattern).
    """
    cfg = {"memory": {"provider": "mnemosyne"}, "agent": {}}

    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("plugins.memory.load_memory_provider", return_value=None),
        patch("agent.model_metadata.get_model_context_length", return_value=204_800),
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=False,
            session_id="sess-replay",
            platform="cli",
        )

    # Simulate gateway: bind status_callback after construction.
    callback_events = []
    agent.status_callback = lambda ev, msg: callback_events.append((ev, msg))

    # Replay the stored warning (as done in run_conversation).
    agent._replay_init_memory_warning()

    # Must have been delivered through status_callback.
    assert len(callback_events) == 1, f"expected 1 callback event, got {callback_events}"
    event_type, msg = callback_events[0]
    assert event_type == "warn", f"expected 'warn' type, got {event_type!r}"
    assert "mnemosyne" in msg, f"expected 'mnemosyne' in warning, got {msg!r}"

    # Attribute must be cleared after one replay.
    assert getattr(agent, "_init_memory_warning", None) is None, (
        "_init_memory_warning must be cleared after replay"
    )


def test_replay_init_memory_warning_noop_when_no_warning():
    """_replay_init_memory_warning is a no-op when there's no stored warning."""
    cfg = {"memory": {"provider": "mnemosyne"}, "agent": {}}

    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("plugins.memory.load_memory_provider", return_value=None),
        patch("agent.model_metadata.get_model_context_length", return_value=204_800),
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=False,
            session_id="sess-no-warn",
            platform="cli",
        )

    # Clear any stored warning (simulate no-warning state).
    agent._init_memory_warning = None
    callback_events = []
    agent.status_callback = lambda ev, msg: callback_events.append((ev, msg))

    # Must not raise.
    agent._replay_init_memory_warning()

    assert len(callback_events) == 0, f"expected no events, got {callback_events}"


def test_replay_init_memory_warning_without_callback_is_noop():
    """_replay_init_memory_warning doesn't crash when status_callback is None."""
    cfg = {"memory": {"provider": "mnemosyne"}, "agent": {}}

    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("plugins.memory.load_memory_provider", return_value=None),
        patch("agent.model_metadata.get_model_context_length", return_value=204_800),
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=False,
            session_id="sess-no-cb",
            platform="cli",
        )

    agent._init_memory_warning = "some warning"
    # status_callback is None — must not raise.
    agent._replay_init_memory_warning()
    # Attribute should still be cleared.
    assert getattr(agent, "_init_memory_warning", None) is None
