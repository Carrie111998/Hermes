"""Tests for gateway /fast support and Priority Processing routing."""

import sys
import threading
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


class _CapturingAgent:
    last_init = None
    last_run = None

    def __init__(self, *args, **kwargs):
        type(self).last_init = dict(kwargs)
        self.tools = []

    def run_conversation(
        self,
        user_message,
        conversation_history=None,
        task_id=None,
        persist_user_message=None,
        persist_user_timestamp=None,
    ):
        type(self).last_run = {
            "user_message": user_message,
            "conversation_history": conversation_history,
            "task_id": task_id,
            "persist_user_message": persist_user_message,
            "persist_user_timestamp": persist_user_timestamp,
        }
        return {
            "final_response": "ok",
            "messages": [],
            "api_calls": 1,
            "completed": True,
        }


def _install_fake_agent(monkeypatch):
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)


def _make_runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._service_tier = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner._pending_model_notes = {}
    runner._session_db = None
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._session_model_overrides = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(streaming=None)
    runner.session_store = SimpleNamespace(
        get_or_create_session=lambda source: SimpleNamespace(session_id="session-1"),
        load_transcript=lambda session_id: [],
    )
    runner._get_or_create_gateway_honcho = lambda session_key: (None, None)
    runner._enrich_message_with_vision = AsyncMock(return_value="ENRICHED")
    return runner


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
        user_id="user-1",
    )


def _make_discord_auto_thread_source() -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="999",
        chat_type="thread",
        user_id="user-1",
        thread_id="999",
        parent_chat_id="100",
        auto_thread_created=True,
        auto_thread_initial_name="raw user prompt",
    )


def _make_slack_assistant_thread_source() -> SessionSource:
    source = SessionSource(
        platform=Platform.SLACK,
        chat_id="D123",
        chat_type="dm",
        user_id="U_USER",
        thread_id="171.111",
        scope_id="T_TEAM",
    )
    setattr(source, "_slack_assistant_lifecycle_provenance", True)
    return source


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def test_turn_route_injects_priority_processing_without_changing_runtime():
    runner = _make_runner()
    runner._service_tier = "priority"
    runtime_kwargs = {
        "api_key": "***",
        "base_url": "https://openrouter.ai/api/v1",
        "provider": "openrouter",
        "api_mode": "chat_completions",
        "command": None,
        "args": [],
        "credential_pool": None,
    }

    route = gateway_run.GatewayRunner._resolve_turn_agent_config(runner, "hi", "gpt-5.4", runtime_kwargs)

    assert route["runtime"]["provider"] == "openrouter"
    assert route["runtime"]["api_mode"] == "chat_completions"
    assert route["request_overrides"] == {"service_tier": "priority"}


@pytest.mark.asyncio
async def test_handle_fast_command_global_flag_persists_config(monkeypatch, tmp_path):
    runner = _make_runner()

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")

    response = await runner._handle_fast_command(_make_event("/fast fast --global"))

    assert "FAST" in response
    assert runner._service_tier == "priority"

    saved = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert saved["agent"]["service_tier"] == "fast"
    # Global write supersedes the session override.
    assert not runner._session_service_tier_overrides


@pytest.mark.asyncio
async def test_session_fast_override_beats_config_default(monkeypatch, tmp_path):
    """A session /fast normal wins over agent.service_tier: fast in config."""
    runner = _make_runner()

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_runtime_config",
        lambda: {"agent": {"service_tier": "fast"}},
    )
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")

    event = _make_event("/fast normal")
    session_key = runner._session_key_for_source(event.source)

    response = await runner._handle_fast_command(event)

    assert "NORMAL" in response
    # Override stores explicit None (normal) and wins over config "fast".
    assert session_key in runner._session_service_tier_overrides
    assert runner._resolve_session_service_tier(session_key=session_key) is None
    # A different session still gets the config default.
    assert runner._resolve_session_service_tier(session_key="other-session") == "priority"


@pytest.mark.asyncio
async def test_run_agent_passes_slack_generated_thread_title_callback(monkeypatch, tmp_path):
    _install_fake_agent(monkeypatch)
    runner = _make_runner()
    runner._session_db = SimpleNamespace(_db=MagicMock())  # type: ignore[assignment]

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_env_path", tmp_path / ".env")
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_load_gateway_runtime_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "***",
        },
    )

    import hermes_cli.tools_config as tools_config

    monkeypatch.setattr(
        tools_config, "_get_platform_tools", lambda user_config, platform_key: {"core"}
    )
    source = _make_slack_assistant_thread_source()
    session_key = "agent:main:slack:dm:D123:thread:171.111"

    with patch("agent.title_generator.maybe_auto_title") as mock_title:
        await runner._run_agent(
            message="raw user prompt",
            context_prompt="",
            history=[],
            source=source,
            session_id="session-1",
            session_key=session_key,
            run_generation=7,
        )

    callback = mock_title.call_args.kwargs["title_callback"]
    with patch.object(runner, "_schedule_slack_generated_thread_title") as mock_schedule:
        callback("Semantic Session Title")

    scheduled_source, session_id, title, scheduled_key, run_generation = (
        mock_schedule.call_args.args
    )
    assert scheduled_source == source
    assert scheduled_source is not source
    assert getattr(scheduled_source, "_slack_assistant_lifecycle_provenance") is True
    assert session_id == "session-1"
    assert title == "Semantic Session Title"
    assert scheduled_key == session_key
    assert run_generation == 7
    runner._telegram_topic_mode_enabled = lambda candidate: True
    telegram_topic = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
        user_id="user-1",
        thread_id="42",
    )
    discord_thread = _make_discord_auto_thread_source()
    assert runner._is_telegram_topic_lane(telegram_topic) is True
    assert runner._is_discord_auto_thread_lane(discord_thread) is True
    assert runner._is_slack_assistant_thread_lane(telegram_topic) is False
    assert runner._is_slack_assistant_thread_lane(discord_thread) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chat_id", "chat_type"),
    [("D_CLASSIC", "dm"), ("G_MPIM", "dm")],
)
async def test_run_agent_omits_slack_title_callback_without_lifecycle_provenance(
    monkeypatch, tmp_path, chat_id, chat_type
):
    _install_fake_agent(monkeypatch)
    runner = _make_runner()
    runner._session_db = SimpleNamespace(_db=MagicMock())  # type: ignore[assignment]
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_env_path", tmp_path / ".env")
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_load_gateway_runtime_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "***",
        },
    )

    import hermes_cli.tools_config as tools_config

    monkeypatch.setattr(
        tools_config, "_get_platform_tools", lambda user_config, platform_key: {"core"}
    )
    source = SessionSource(
        platform=Platform.SLACK,
        chat_id=chat_id,
        chat_type=chat_type,
        user_id="U_USER",
        thread_id="171.111",
        scope_id="T_TEAM",
    )

    with patch("agent.title_generator.maybe_auto_title") as mock_title:
        await runner._run_agent(
            message="ordinary threaded DM",
            context_prompt="",
            history=[],
            source=source,
            session_id="session-1",
            session_key=f"agent:main:slack:dm:{chat_id}:thread:171.111",
        )

    assert "title_callback" not in mock_title.call_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("peek", [lambda key: "session-2", lambda key: (_ for _ in ()).throw(RuntimeError())])
async def test_slack_generated_thread_title_uses_fail_closed_final_session_validator(peek):
    runner = _make_runner()
    source = _make_slack_assistant_thread_source()
    session_key = "agent:main:slack:dm:D123:thread:171.111"
    runner._session_state(session_key)
    runner.session_store.peek_session_id = MagicMock(side_effect=peek)
    validation_results = []

    async def set_title(*args, **kwargs):
        validation_results.append(kwargs["is_current_session"]())

    runner._adapter_for_source = lambda candidate: SimpleNamespace(
        set_generated_assistant_thread_title=set_title
    )
    runner.adapters = {Platform.SLACK: object()}

    await runner._set_slack_generated_thread_title(
        source,
        "session-1",
        "Semantic Session Title",
        session_key,
    )

    assert validation_results == [False]
    runner.session_store.peek_session_id.assert_called_once_with(session_key)


@pytest.mark.asyncio
async def test_slack_title_validator_allows_later_turn_but_rejects_explicit_invalidation():
    runner = _make_runner()
    source = _make_slack_assistant_thread_source()
    session_key = "agent:main:slack:dm:D123:thread:171.111"
    runner.session_store.peek_session_id = MagicMock(return_value="session-1")
    captured_validators = []

    async def set_title(*args, **kwargs):
        captured_validators.append(kwargs["is_current_session"])

    runner._adapter_for_source = lambda candidate: SimpleNamespace(
        set_generated_assistant_thread_title=set_title
    )
    runner.adapters = {Platform.SLACK: object()}
    title_run_generation = runner._begin_session_run_generation(session_key)

    await runner._set_slack_generated_thread_title(
        source,
        "session-1",
        "Semantic Session Title",
        session_key,
        title_run_generation,
    )

    runner._begin_session_run_generation(session_key)
    assert captured_validators[0]() is True

    runner._invalidate_session_run_generation(session_key, reason="test_reset")
    assert captured_validators[0]() is False


