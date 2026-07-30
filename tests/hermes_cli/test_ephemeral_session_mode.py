"""Contract tests for profile-scoped ephemeral Hermes sessions."""

from __future__ import annotations

import inspect
from pathlib import Path

import hermes_state
from agent.agent_init import init_agent
from hermes_cli.config_defaults import DEFAULT_CONFIG
from hermes_cli.oneshot import _create_session_db_for_oneshot
from run_agent import AIAgent


def test_session_persistence_defaults_to_enabled() -> None:
    """Existing profiles must retain persistent sessions by default."""
    assert DEFAULT_CONFIG["sessions"]["persist"] is True


def test_aiagent_exposes_explicit_persist_session_argument() -> None:
    """Callers must not overload session_db=None to mean two different things."""
    parameter = inspect.signature(AIAgent.__init__).parameters["persist_session"]

    assert parameter.default is True


def test_init_agent_exposes_explicit_persist_session_argument() -> None:
    """The constructor forwarder must propagate the persistence policy."""
    parameter = inspect.signature(init_agent).parameters["persist_session"]

    assert parameter.default is True


def test_oneshot_does_not_open_session_db_when_persistence_disabled(
    monkeypatch,
) -> None:
    """Ephemeral one-shot runs must avoid touching canonical state.db."""

    def unexpected_session_db():
        raise AssertionError("SessionDB must not be constructed")

    monkeypatch.setattr(hermes_state, "SessionDB", unexpected_session_db)

    assert _create_session_db_for_oneshot(persist_session=False) is None


def test_oneshot_uses_profile_persistence_setting(monkeypatch) -> None:
    """The profile setting must control DB creation and agent persistence."""
    from hermes_cli import oneshot

    captured: dict[str, object] = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.suppress_status_output = False
            self.stream_delta_callback = object()
            self.tool_gen_callback = object()
            self._session_messages = []

        def run_conversation(self, prompt):
            return {"final_response": "ok"}

        def shutdown_memory_provider(self, *args, **kwargs):
            return None

        def close(self):
            return None

    monkeypatch.setattr(
        oneshot,
        "load_config",
        lambda: {
            "sessions": {"persist": False},
            "model": {"default": "test-model"},
        },
        raising=False,
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "sessions": {"persist": False},
            "model": {"default": "test-model"},
        },
    )
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **kwargs: {
            "api_key": "test",
            "base_url": "https://example.invalid",
            "provider": "test",
            "requested_provider": "test",
            "api_mode": None,
            "credential_pool": None,
        },
    )
    monkeypatch.setattr(
        "hermes_cli.tools_config._get_platform_tools",
        lambda cfg, platform: set(),
    )
    monkeypatch.setattr("run_agent.AIAgent", FakeAgent)
    monkeypatch.setattr(
        oneshot,
        "_create_session_db_for_oneshot",
        lambda persist_session=True: captured.setdefault(
            "db_persist_session", persist_session
        ),
    )

    response, result = oneshot._run_agent("hello")

    assert response == "ok"
    assert result["final_response"] == "ok"
    assert captured["db_persist_session"] is False
    assert captured["persist_session"] is False
    assert captured["session_db"] is False


def test_agent_persistence_disabled_blocks_lazy_db_open(monkeypatch) -> None:
    """An ephemeral agent must never lazily open canonical state.db."""
    agent = object.__new__(AIAgent)
    agent._persist_disabled = True
    agent._session_db = None

    def unexpected_session_db():
        raise AssertionError("SessionDB must not be opened")

    monkeypatch.setattr(hermes_state, "SessionDB", unexpected_session_db)

    assert agent._get_session_db_for_recall() is None


def test_agent_init_sets_persistence_disabled() -> None:
    """The public constructor policy must activate the existing hard-stop."""
    agent = object.__new__(AIAgent)

    # init_agent has many unrelated setup dependencies, so verify the public
    # AIAgent path using a minimal stub around the forwarder.
    from unittest.mock import patch

    with patch("agent.agent_init.init_agent") as mocked_init:
        AIAgent.__init__(
            agent,
            model="test-model",
            persist_session=False,
        )

    assert mocked_init.call_args.kwargs["persist_session"] is False


def test_invalid_profile_persistence_value_must_not_enable_storage() -> None:
    """A malformed security setting must fail closed, never become truthy."""
    from hermes_cli.config import resolve_session_persistence

    try:
        resolve_session_persistence({"sessions": {"persist": "false"}})
    except ValueError:
        pass
    else:
        raise AssertionError("invalid sessions.persist value must be rejected")


def test_interactive_cli_does_not_open_session_db_when_ephemeral(
    monkeypatch,
) -> None:
    """Interactive startup must not touch state.db for an ephemeral profile."""
    import copy
    import cli

    config = copy.deepcopy(cli.CLI_CONFIG)
    config.setdefault("sessions", {})["persist"] = False

    def unexpected_session_db():
        raise AssertionError("SessionDB must not be constructed")

    monkeypatch.setattr(cli, "CLI_CONFIG", config)
    monkeypatch.setattr(hermes_state, "SessionDB", unexpected_session_db)

    hermes_cli = cli.HermesCLI(model="test-model")

    assert hermes_cli.persist_session is False
    assert hermes_cli._session_db is None
    assert hermes_cli._session_db_unavailable is False


def test_interactive_cli_rejects_resume_when_ephemeral(monkeypatch) -> None:
    """Resume requires persisted state and must fail closed."""
    import copy
    import cli
    import pytest

    config = copy.deepcopy(cli.CLI_CONFIG)
    config.setdefault("sessions", {})["persist"] = False
    monkeypatch.setattr(cli, "CLI_CONFIG", config)

    with pytest.raises(
        ValueError,
        match="cannot resume.*sessions.persist is false",
    ):
        cli.HermesCLI(model="test-model", resume="existing-session")


def test_ephemeral_agent_persist_session_is_complete_noop(monkeypatch) -> None:
    """Ephemeral persistence must skip JSON, SQLite, and accounting drains."""
    agent = object.__new__(AIAgent)
    agent._persist_disabled = True
    agent._session_db = None
    agent._session_persist_lock = None

    calls: list[str] = []

    monkeypatch.setattr(
        agent,
        "_drop_trailing_empty_response_scaffolding",
        lambda messages: calls.append("drop"),
    )
    monkeypatch.setattr(
        agent,
        "_save_session_log",
        lambda messages: calls.append("json"),
    )
    monkeypatch.setattr(
        agent,
        "_flush_messages_to_session_db",
        lambda messages, history=None: calls.append("sqlite"),
    )

    agent._persist_session([{"role": "user", "content": "secret"}])

    assert calls == []
    assert not hasattr(agent, "_session_messages")


def test_ephemeral_agent_close_never_ends_database_session() -> None:
    """Close must not touch an injected database when persistence is disabled."""
    calls: list[tuple[str, str]] = []

    class RecordingDB:
        def end_session(self, session_id, reason):
            calls.append((session_id, reason))

    agent = object.__new__(AIAgent)
    agent._persist_disabled = True
    agent._end_session_on_close = True
    agent._session_db = RecordingDB()
    agent.session_id = "ephemeral-test"
    agent.client = None
    agent._active_children_lock = __import__("threading").RLock()
    agent._active_children = set()

    # Resource cleanup is independently best-effort; this test targets only
    # the final database-session write.
    agent.close()

    assert calls == []


def test_ephemeral_agent_never_writes_api_request_debug_dump(
    monkeypatch,
) -> None:
    """Debug failures must not persist prompts for ephemeral agents."""
    from agent.agent_runtime_helpers import dump_api_request_debug

    agent = object.__new__(AIAgent)
    agent._persist_disabled = True
    agent.verbose_logging = False
    agent.session_id = "ephemeral-test"
    agent.logs_dir = Path("/tmp")
    agent.base_url = "https://example.invalid"
    agent.api_mode = "chat_completions"
    agent.client = None

    writes: list[object] = []

    def record_write(*args, **kwargs):
        writes.append((args, kwargs))

    monkeypatch.setattr(
        "agent.agent_runtime_helpers.atomic_json_write",
        record_write,
    )

    result = dump_api_request_debug(
        agent,
        {
            "model": "test-model",
            "messages": [{"role": "user", "content": "secret"}],
        },
        reason="test",
    )

    assert result is None
    assert writes == []
