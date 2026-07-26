from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str, *, internal: bool = True) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=_make_source(),
        message_id="m1",
        internal=internal,
    )


def _session_entry() -> SessionEntry:
    return SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=0,
    )


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    adapter._pending_messages = {}
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(
        emit=AsyncMock(),
        emit_collect=AsyncMock(return_value=[]),
        loaded_hooks=False,
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = _session_entry()
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._queued_events = {}
    runner._session_db = MagicMock()
    runner._session_db.get_session_title.return_value = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    runner._update_prompt_pending = {}
    runner._busy_input_mode = "interrupt"
    runner._draining = False
    runner._session_run_generation = {}
    runner._session_sources = {}
    runner._pending_native_image_paths_by_session = {}
    runner._background_tasks = {}
    runner._background_task_counter = 0
    runner._session_model_overrides = {}
    runner._pending_model_notes = {}
    runner._service_tier = None
    runner._fast_mode_by_session = {}
    runner._goal_state_by_session = {}
    runner._goal_runs_in_progress = set()
    runner._goal_queued_by_session = set()
    runner._is_telegram_topic_root_lobby = lambda _source: False
    runner._should_send_telegram_lobby_reminder = lambda _source: False
    runner._check_slash_access = lambda _source, _command: None
    runner._begin_session_run_generation = lambda _key: 1
    runner._release_running_agent_state = lambda key: runner._running_agents.pop(key, None)
    return runner, adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("command_text", ["/queue do this next", "/q do this next"])
async def test_idle_queue_sends_payload_as_next_turn(command_text):
    runner, _adapter = _make_runner()
    captured = {}

    async def fake_handle_message_with_agent(event, source, key, generation):
        captured["text"] = event.text
        captured["command"] = event.get_command()
        captured["source"] = source
        captured["key"] = key
        captured["generation"] = generation
        return {"final_response": "", "messages": []}

    runner._handle_message_with_agent = fake_handle_message_with_agent

    result = await runner._handle_message(_make_event(command_text))

    assert result == {"final_response": "", "messages": []}
    assert captured["text"] == "do this next"
    assert captured["command"] is None
    assert captured["source"] == _make_source()
    assert captured["key"] == build_session_key(_make_source())
    assert captured["generation"] == 1
    assert runner._running_agents == {}


@pytest.mark.asyncio
async def test_idle_queue_without_payload_returns_usage():
    runner, _adapter = _make_runner()
    called = False

    async def fake_handle_message_with_agent(event, source, key, generation):
        nonlocal called
        called = True
        return {"final_response": "", "messages": []}

    runner._handle_message_with_agent = fake_handle_message_with_agent

    result = await runner._handle_message(_make_event("/queue"))

    assert result == "Usage: /queue <prompt>"
    assert called is False
    assert runner._running_agents == {}


@pytest.mark.asyncio
async def test_protected_plugin_command_receives_live_context_and_revokes(monkeypatch):
    import hermes_cli.plugins as plugins
    from gateway.authenticated_dispatch import validate_authenticated_gateway_dispatch

    runner, _adapter = _make_runner()
    captured = {}

    monkeypatch.setattr(
        plugins,
        "get_plugin_commands",
        lambda: {"secure-test": {"requires_authenticated_gateway": True}},
    )

    def invoke(name, args, *, authenticated_gateway_context=None):
        captured["name"] = name
        captured["args"] = args
        captured["context"] = authenticated_gateway_context
        assert validate_authenticated_gateway_dispatch(authenticated_gateway_context)
        return "accepted"

    monkeypatch.setattr(plugins, "invoke_plugin_command", invoke)

    result = await runner._handle_message(
        _make_event("/secure_test payload", internal=False)
    )

    assert result == "accepted"
    assert captured["name"] == "secure-test"
    assert captured["args"] == "payload"
    assert captured["context"].platform == "telegram"
    assert captured["context"].user_id == "u1"
    assert captured["context"].chat_id == "c1"
    assert captured["context"].message_id == "m1"
    assert not validate_authenticated_gateway_dispatch(captured["context"])


@pytest.mark.asyncio
async def test_protected_plugin_command_internal_event_denies_without_entry(monkeypatch):
    import hermes_cli.plugins as plugins

    runner, _adapter = _make_runner()
    setattr(runner.hooks, "loaded_hooks", True)
    entered = False

    monkeypatch.setattr(
        plugins,
        "get_plugin_commands",
        lambda: {"secure-test": {"requires_authenticated_gateway": True}},
    )

    def invoke(*args, **kwargs):
        nonlocal entered
        entered = True
        return "unexpected"

    monkeypatch.setattr(plugins, "invoke_plugin_command", invoke)
    event = _make_event("/secure-test payload")
    event.internal = True

    result = await runner._handle_message(event)

    assert result == "Authenticated gateway command dispatch denied."
    assert entered is False
    assert getattr(runner.hooks.emit_collect, "call_count") == 0


@pytest.mark.asyncio
async def test_protected_command_discovery_failure_denies_before_hooks_or_fallthrough(
    monkeypatch,
):
    import hermes_cli.plugins as plugins

    runner, _adapter = _make_runner()
    setattr(runner.hooks, "loaded_hooks", True)
    lookups = 0
    entered = False
    fell_through = False

    def get_commands():
        nonlocal lookups
        lookups += 1
        if lookups == 1:
            # The slash-access lookup runs before the security classification.
            return {}
        if lookups == 2:
            raise RuntimeError("transient security-classification failure")
        return {"secure-test": {"requires_authenticated_gateway": True}}

    def invoke(*args, **kwargs):
        nonlocal entered
        entered = True
        return "unexpected"

    async def fallback(*args, **kwargs):
        nonlocal fell_through
        fell_through = True
        return {"final_response": "unexpected"}

    monkeypatch.setattr(plugins, "get_plugin_commands", get_commands)
    monkeypatch.setattr(plugins, "invoke_plugin_command", invoke)
    runner._handle_message_with_agent = fallback

    result = await runner._handle_message(_make_event("/secure-test payload"))

    assert result == "Authenticated gateway command dispatch denied."
    assert entered is False
    assert fell_through is False
    assert getattr(runner.hooks.emit_collect, "call_count") == 0


@pytest.mark.asyncio
async def test_protected_plugin_command_missing_identity_denies_without_entry_or_fallthrough(
    monkeypatch,
):
    import hermes_cli.plugins as plugins

    runner, _adapter = _make_runner()
    entered = False
    fell_through = False

    monkeypatch.setattr(
        plugins,
        "get_plugin_commands",
        lambda: {"secure-test": {"requires_authenticated_gateway": True}},
    )

    def invoke(*args, **kwargs):
        nonlocal entered
        entered = True
        return "unexpected"

    async def fallback(*args, **kwargs):
        nonlocal fell_through
        fell_through = True
        return {"final_response": "unexpected"}

    monkeypatch.setattr(plugins, "invoke_plugin_command", invoke)
    runner._handle_message_with_agent = fallback

    source = _make_source()
    source.user_id = None
    event = MessageEvent(
        text="/secure_test payload",
        message_type=MessageType.TEXT,
        source=source,
        message_id="m1",
        internal=False,
    )
    result = await runner._handle_message(event)

    assert result == "Authenticated gateway command dispatch denied."
    assert entered is False
    assert fell_through is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_message_id", "source_message_id"),
    [(None, None), ("event-message", "different-source-message")],
)
async def test_protected_plugin_command_requires_matching_trigger_message_id(
    monkeypatch,
    event_message_id,
    source_message_id,
):
    import hermes_cli.plugins as plugins

    runner, _adapter = _make_runner()
    entered = False
    monkeypatch.setattr(
        plugins,
        "get_plugin_commands",
        lambda: {"secure-test": {"requires_authenticated_gateway": True}},
    )

    def invoke(*args, **kwargs):
        nonlocal entered
        entered = True
        return "unexpected"

    monkeypatch.setattr(plugins, "invoke_plugin_command", invoke)
    source = _make_source()
    source.message_id = source_message_id
    event = MessageEvent(
        text="/secure_test payload",
        message_type=MessageType.TEXT,
        source=source,
        message_id=event_message_id,
        internal=False,
    )

    result = await runner._handle_message(event)

    assert result == "Authenticated gateway command dispatch denied."
    assert entered is False


@pytest.mark.asyncio
async def test_protected_plugin_command_handler_failure_does_not_fall_through(monkeypatch):
    import hermes_cli.plugins as plugins

    runner, _adapter = _make_runner()
    fell_through = False

    monkeypatch.setattr(
        plugins,
        "get_plugin_commands",
        lambda: {"secure-test": {"requires_authenticated_gateway": True}},
    )

    def invoke(*args, **kwargs):
        raise RuntimeError("private handler detail")

    async def fallback(*args, **kwargs):
        nonlocal fell_through
        fell_through = True
        return {"final_response": "unexpected"}

    monkeypatch.setattr(plugins, "invoke_plugin_command", invoke)
    runner._handle_message_with_agent = fallback

    result = await runner._handle_message(
        _make_event("/secure_test payload", internal=False)
    )

    assert result == "Authenticated gateway command dispatch denied."
    assert fell_through is False
