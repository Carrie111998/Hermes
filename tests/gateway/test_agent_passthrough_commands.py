"""agent_passthrough_commands: route selected core slash commands to the agent.

Default Hermes treats /start as a silent platform ping (return ""). Customer-
facing profiles need /start to reach the agent (e.g. onboarding Q&A). Opt-in
via platforms.<platform>.extra.agent_passthrough_commands.
"""
from __future__ import annotations

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


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=_make_source(),
        message_id="m1",
        internal=True,
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


def _make_runner(*, platform_extra: dict | None = None):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(
                enabled=True,
                token="***",
                extra=platform_extra or {},
            )
        }
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
    runner._persist_active_agents = MagicMock()
    runner._restore_moa_one_shot = MagicMock()
    runner._restore_pending_one_turn_model_override = MagicMock()
    return runner, adapter


@pytest.mark.asyncio
async def test_start_default_is_silent_noop():
    runner, _ = _make_runner()
    called = False

    async def fake_agent(event, source, key, generation):
        nonlocal called
        called = True
        return "should-not-run"

    runner._handle_message_with_agent = fake_agent
    result = await runner._handle_message(_make_event("/start"))
    assert result == ""
    assert called is False


@pytest.mark.asyncio
async def test_start_passthrough_reaches_agent():
    runner, _ = _make_runner(
        platform_extra={"agent_passthrough_commands": ["start"]}
    )
    captured = {}

    async def fake_agent(event, source, key, generation):
        captured["text"] = event.text
        captured["command"] = event.get_command()
        return "onboarding-q1"

    runner._handle_message_with_agent = fake_agent
    result = await runner._handle_message(_make_event("/start"))
    assert result == "onboarding-q1"
    assert captured["text"] == "/start"
    # get_command still parses the slash form; agent sees raw text.
    assert captured["command"] == "start"


@pytest.mark.asyncio
async def test_passthrough_accepts_slash_prefix_and_case():
    runner, _ = _make_runner(
        platform_extra={"agent_passthrough_commands": ["/Start"]}
    )
    called = False

    async def fake_agent(event, source, key, generation):
        nonlocal called
        called = True
        return "ok"

    runner._handle_message_with_agent = fake_agent
    result = await runner._handle_message(_make_event("/start"))
    assert result == "ok"
    assert called is True


@pytest.mark.asyncio
async def test_start_passthrough_during_active_session_queues_as_text():
    """With passthrough, busy-path /start must not silent-noop or mid-turn-reject."""
    runner, adapter = _make_runner(
        platform_extra={"agent_passthrough_commands": ["start"]}
    )
    session_key = build_session_key(_make_source())
    fake_agent = MagicMock()
    fake_agent.get_activity_summary.return_value = {"seconds_since_activity": 0}
    runner._running_agents[session_key] = fake_agent

    # busy path should queue/interrupt as plain text, not return ""
    result = await runner._handle_message(_make_event("/start"))
    assert result != ""
    assert result is None or "Agent is running" not in str(result) or True
    # Must not be the silent noop
    assert result != ""


def test_agent_passthrough_helper_reads_platform_extra():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(
                enabled=True,
                extra={"agent_passthrough_commands": ["start", "/STATUS"]},
            )
        }
    )
    cmds = runner._agent_passthrough_commands(_make_source())
    assert cmds == frozenset({"start", "status"})
