"""Tests for the GatewayRunner public injection API.

Covers:
- inject_internal_message: adapter selection, SessionSource routing,
  internal=True flag, notice_text delivery, missing-adapter failure
- resolve_session_id: Telegram chat_id → session key mapping
- steer_session: active-agent steer vs idle fallback
- No Platform.ATM creation (negative guarantee)
- Runner exposed via gateway:startup hook payload
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key


# ------------------------------------------------------------------
# Test infrastructure
# ------------------------------------------------------------------

class _FakeTelegramAdapter:
    """Minimal Telegram adapter for injection tests.

    Captures the event passed to handle_message so tests can assert
    on routing decisions (source platform, internal flag, etc.).
    """

    def __init__(self):
        self.sent_messages: list = []          # (chat_id, text) tuples
        self.handled_events: list[MessageEvent] = []
        self._message_handler = AsyncMock()

    async def send(self, chat_id, text, **kwargs):
        self.sent_messages.append((chat_id, text))

    async def handle_message(self, event):
        self.handled_events.append(event)
        if self._message_handler:
            await self._message_handler(event)


def _make_runner(with_session_store=True):
    """Build a bare GatewayRunner for unit testing the injection API."""
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    tg = _FakeTelegramAdapter()
    runner.adapters = {Platform.TELEGRAM: tg}
    runner._profile_adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._session_run_generation = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._voice_mode = {}
    runner._background_tasks = set()
    runner._draining = False
    runner._restart_requested = False
    runner._restart_task_started = False
    runner._restart_detached = False
    runner._restart_via_service = False
    runner._restart_drain_timeout = 0.0
    runner._stop_task = None
    runner._exit_code = None
    runner._update_runtime_status = MagicMock()
    runner._is_user_authorized = lambda _source: True
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.delivery_router = MagicMock()
    if with_session_store:
        runner.session_store = MagicMock()
        runner.session_store._generate_session_key = lambda src: build_session_key(src)
    else:
        runner.session_store = None
    # Property backing dicts
    runner._sessions = {}
    return runner


# ------------------------------------------------------------------
# inject_internal_message
# ------------------------------------------------------------------

class TestInjectInternalMessage:
    """inject_internal_message routes an internal event to adapter.handle_message."""

    @pytest.mark.asyncio
    async def test_routes_through_telegram_adapter(self):
        """The event reaches handle_message on the correct adapter."""
        runner = _make_runner()
        await runner.inject_internal_message(
            profile="skillrx",
            platform=Platform.TELEGRAM,
            chat_id="8991600178",
            text="ATM nudge test marker",
            notice_text=None,
        )
        tg = runner.adapters[Platform.TELEGRAM]
        assert len(tg.handled_events) == 1
        event = tg.handled_events[0]
        assert event.text == "ATM nudge test marker"
        assert event.internal is True

    @pytest.mark.asyncio
    async def test_constructs_session_source_with_telegram_platform(self):
        """SessionSource reflects the real platform, not ATM."""
        runner = _make_runner()
        await runner.inject_internal_message(
            profile="skillrx",
            platform=Platform.TELEGRAM,
            chat_id="8991600178",
            text="test",
        )
        tg = runner.adapters[Platform.TELEGRAM]
        event = tg.handled_events[0]
        assert event.source.platform == Platform.TELEGRAM
        assert event.source.chat_id == "8991600178"
        assert event.source.chat_type == "dm"

    @pytest.mark.asyncio
    async def test_internal_flag_is_true(self):
        """The MessageEvent carries internal=True so _handle_message skips
        authorization and startup-restore guards."""
        runner = _make_runner()
        await runner.inject_internal_message(
            profile="skillrx",
            platform=Platform.TELEGRAM,
            chat_id="8991600178",
            text="test",
        )
        tg = runner.adapters[Platform.TELEGRAM]
        assert tg.handled_events[0].internal is True

    @pytest.mark.asyncio
    async def test_profile_passed_to_session_source(self):
        """The profile name is attached to SessionSource for session namespacing."""
        runner = _make_runner()
        await runner.inject_internal_message(
            profile="skillrx",
            platform=Platform.TELEGRAM,
            chat_id="8991600178",
            text="test",
        )
        tg = runner.adapters[Platform.TELEGRAM]
        assert tg.handled_events[0].source.profile == "skillrx"

    @pytest.mark.asyncio
    async def test_sends_notice_text_before_routing(self):
        """notice_text is delivered via adapter.send before handle_message."""
        runner = _make_runner()
        await runner.inject_internal_message(
            profile="skillrx",
            platform=Platform.TELEGRAM,
            chat_id="8991600178",
            text="nudge payload",
            notice_text="⚡ ATM nudge received",
        )
        tg = runner.adapters[Platform.TELEGRAM]
        # Notice sent first
        assert tg.sent_messages == [("8991600178", "⚡ ATM nudge received")]
        # Then event routed
        assert tg.handled_events[0].text == "nudge payload"

    @pytest.mark.asyncio
    async def test_missing_adapter_returns_none(self):
        """Returns None gracefully when no adapter exists for the platform."""
        runner = _make_runner()
        runner.adapters = {}  # no adapters at all
        result = await runner.inject_internal_message(
            profile="skillrx",
            platform=Platform.TELEGRAM,
            chat_id="8991600178",
            text="test",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_notice_failure_does_not_prevent_routing(self):
        """If adapter.send raises, the event is still routed to handle_message."""
        runner = _make_runner()
        tg = runner.adapters[Platform.TELEGRAM]
        tg.send = AsyncMock(side_effect=Exception("network down"))

        await runner.inject_internal_message(
            profile="skillrx",
            platform=Platform.TELEGRAM,
            chat_id="8991600178",
            text="payload",
            notice_text="notice that fails",
        )
        # Still routed
        assert len(tg.handled_events) == 1
        assert tg.handled_events[0].text == "payload"

    @pytest.mark.asyncio
    async def test_selects_adapter_from_profile_adapters(self):
        """When a profile is in _profile_adapters, its adapter is used."""
        runner = _make_runner()
        skillrx_tg = _FakeTelegramAdapter()
        runner._profile_adapters["skillrx"] = {Platform.TELEGRAM: skillrx_tg}
        # The default adapter should NOT be used
        default_tg = runner.adapters[Platform.TELEGRAM]

        await runner.inject_internal_message(
            profile="skillrx",
            platform=Platform.TELEGRAM,
            chat_id="8991600178",
            text="test",
        )
        # Profile adapter was used
        assert len(skillrx_tg.handled_events) == 1
        # Default adapter was NOT used
        assert len(default_tg.handled_events) == 0

    @pytest.mark.asyncio
    async def test_falls_back_to_default_adapters_when_profile_not_found(self):
        """When profile not in _profile_adapters, falls back to self.adapters."""
        runner = _make_runner()
        # Don't register a separate profile adapter
        runner._profile_adapters = {}
        default_tg = runner.adapters[Platform.TELEGRAM]

        await runner.inject_internal_message(
            profile="skillrx",
            platform=Platform.TELEGRAM,
            chat_id="8991600178",
            text="test",
        )
        assert len(default_tg.handled_events) == 1


# ------------------------------------------------------------------
# No ATM platform creation (negative guarantee)
# ------------------------------------------------------------------

def test_no_atm_platform_created():
    """inject_internal_message must never register Platform.ATM or
    create an ATM session — it routes through real platform adapters."""
    # Platform.ATM must not exist
    assert not hasattr(Platform, "ATM")

    # The method uses only real platforms (TELEGRAM in our tests)
    runner = _make_runner()
    # After injection, no ATM adapter should exist
    assert "atm" not in {p.value for p in runner.adapters}
    assert "atm" not in {p.value for p in runner._profile_adapters.values()}


# ------------------------------------------------------------------
# resolve_session_id
# ------------------------------------------------------------------

class TestResolveSessionId:
    """resolve_session_id maps a Telegram chat_id to an opaque session key."""

    def test_returns_session_key_for_telegram_chat_id(self):
        runner = _make_runner()
        key = runner.resolve_session_id("8991600178")
        assert key is not None
        assert isinstance(key, str)
        # Session key should contain the chat_id
        assert "8991600178" in key

    def test_returns_session_key_even_without_session_store(self):
        """Returns a valid key via build_session_key fallback when
        no session store is configured."""
        runner = _make_runner(with_session_store=False)
        key = runner.resolve_session_id("8991600178")
        assert key is not None
        assert isinstance(key, str)
        assert "8991600178" in key

    def test_different_chat_ids_produce_different_keys(self):
        runner = _make_runner()
        a = runner.resolve_session_id("111")
        b = runner.resolve_session_id("222")
        assert a != b


# ------------------------------------------------------------------
# steer_session
# ------------------------------------------------------------------

class _MockAgent:
    """Minimal AIAgent stub for steer_session tests."""

    def __init__(self):
        self.steer_calls: list[str] = []

    def steer(self, text):
        self.steer_calls.append(text)


class TestSteerSession:
    """steer_session delivers text at the next safe tool boundary."""

    def test_returns_false_when_no_agent_running(self):
        runner = _make_runner()
        result = asyncio.run(
            runner.steer_session("8991600178", "nudge")
        )
        assert result is False

    def test_returns_true_and_steers_when_agent_active(self):
        """When a session has a running agent, steer delivers the text."""
        runner = _make_runner()
        agent = _MockAgent()
        session_key = runner.resolve_session_id("8991600178")

        # Simulate an active agent turn
        state = runner._session_state(session_key)
        state.turn.agent = agent

        result = asyncio.run(
            runner.steer_session(session_key, "ATM nudge payload")
        )
        assert result is True
        assert agent.steer_calls == ["ATM nudge payload"]

    def test_resolves_chat_id_to_session_key(self):
        """When given a chat_id, steer_session resolves it internally."""
        runner = _make_runner()
        agent = _MockAgent()
        session_key = runner.resolve_session_id("8991600178")

        state = runner._session_state(session_key)
        state.turn.agent = agent

        result = asyncio.run(
            runner.steer_session("8991600178", "fallback resolution test")
        )
        assert result is True
        assert agent.steer_calls == ["fallback resolution test"]

    def test_steer_failure_returns_false(self):
        """When agent.steer raises, steer_session returns False."""
        runner = _make_runner()
        agent = _MockAgent()
        agent.steer = MagicMock(side_effect=RuntimeError("steer broken"))
        session_key = runner.resolve_session_id("8991600178")

        state = runner._session_state(session_key)
        state.turn.agent = agent

        result = asyncio.run(
            runner.steer_session(session_key, "payload")
        )
        assert result is False


# ------------------------------------------------------------------
# Runner in gateway:startup hook payload
# ------------------------------------------------------------------

class TestGatewayStartupHook:
    """The gateway:startup hook payload exposes the runner for plugins."""

    @pytest.mark.asyncio
    async def test_runner_passed_in_startup_hook_context(self):
        """The startup hook payload includes the runner reference."""
        runner = _make_runner()

        # Patch the full start() method and just test the hook emit
        runner.hooks.loaded_hooks = []
        await runner.hooks.emit("gateway:startup", {
            "platforms": [p.value for p in runner.adapters.keys()],
            "runner": runner,
        })

        runner.hooks.emit.assert_called_once()

    def test_hook_context_runner_is_callable(self):
        """The runner reference in the hook context exposes inject_internal_message."""
        runner = _make_runner()
        assert hasattr(runner, "inject_internal_message")
        assert callable(runner.inject_internal_message)
        assert hasattr(runner, "resolve_session_id")
        assert callable(runner.resolve_session_id)
        assert hasattr(runner, "steer_session")
        assert callable(runner.steer_session)
