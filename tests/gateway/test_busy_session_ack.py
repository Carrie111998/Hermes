"""Tests for busy-session acknowledgment when user sends messages during active agent runs.

Verifies that users get an immediate status response instead of total silence
when the agent is working on a task. See PR fix for the @Lonely__MH report.
"""
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Minimal stubs so we can import gateway code without heavy deps
# ---------------------------------------------------------------------------
import sys, types

_tg = types.ModuleType("telegram")
_tg.constants = types.ModuleType("telegram.constants")
_ct = MagicMock()
_ct.SUPERGROUP = "supergroup"
_ct.GROUP = "group"
_ct.PRIVATE = "private"
_tg.constants.ChatType = _ct
sys.modules.setdefault("telegram", _tg)
sys.modules.setdefault("telegram.constants", _tg.constants)
sys.modules.setdefault("telegram.ext", types.ModuleType("telegram.ext"))

from gateway.platforms.base import (
    MessageEvent,
    MessageType,
    Platform,
    SessionSource,
    build_session_key,
)
import sys, threading, types


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(text="hello", chat_id="123", platform_val="telegram"):
    """Build a minimal MessageEvent."""
    source = SessionSource(
        platform=MagicMock(value=platform_val),
        chat_id=chat_id,
        chat_type="private",
        user_id="user1",
    )
    evt = MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source,
        message_id="msg1",
    )
    return evt


def _make_runner():
    """Build a minimal GatewayRunner-like object for testing."""
    from gateway.run import GatewayRunner, _AGENT_PENDING_SENTINEL

    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._busy_ack_ts = {}
    runner._draining = False
    runner._external_drain_active = False
    runner._busy_text_mode = "interrupt"
    runner._queued_events = {}
    runner._busy_queue_lock = threading.RLock()
    runner._busy_queue_claimed_events = {}
    runner._busy_queue_uncertain_sessions = set()
    runner._busy_queue_uncertain_digests = set()
    # These unit tests exercise routing/ACK behavior. The dedicated durability
    # suite covers the real atomic file store; this witness keeps the admission
    # boundary present instead of reviving the legacy volatile fallback.
    runner._busy_queue_persist_ready = MagicMock(return_value=None)
    runner._busy_queue_max_bytes = lambda: 1024 * 1024
    runner.adapters = {}
    runner.config = MagicMock()
    runner.config.group_sessions_per_user = True
    runner.config.thread_sessions_per_user = False
    runner.session_store = None
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = True
    runner._is_user_authorized = lambda _source: True
    return runner, _AGENT_PENDING_SENTINEL


def _make_adapter(platform_val="telegram"):
    """Build a minimal adapter mock."""
    adapter = MagicMock()
    adapter._pending_messages = {}
    adapter._send_with_retry = AsyncMock()
    adapter.config = MagicMock()
    adapter.config.extra = {}
    adapter.platform = MagicMock(value=platform_val)
    adapter._text_debounce = {}
    adapter._busy_text_debounce_seconds = 0.6
    return adapter


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBusySessionAck:
    """User sends a message while agent is running — should get acknowledgment."""


    @pytest.mark.asyncio
    async def test_telegram_grace_followups_respect_queue_fifo(self, monkeypatch):
        """Rapid Telegram text follow-ups in queue mode must not merge."""
        from gateway.run import GatewayRunner

        monkeypatch.setenv("HERMES_TELEGRAM_FOLLOWUP_GRACE_SECONDS", "3.0")

        runner, _sentinel = _make_runner()
        runner._busy_input_mode = "queue"
        runner._queued_events = {}
        adapter = _make_adapter()

        source = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="123",
            chat_type="dm",
            user_id="user1",
        )
        sk = build_session_key(source)
        runner.adapters[source.platform] = adapter

        agent = MagicMock()
        agent.get_activity_summary.return_value = {
            "seconds_since_activity": 0.0,
        }
        runner._running_agents[sk] = agent
        runner._running_agents_ts[sk] = time.time()

        events = [
            MessageEvent(
                text=text,
                message_type=MessageType.TEXT,
                source=source,
                message_id=f"m-{idx}",
            )
            for idx, text in enumerate(("first", "second", "third"), start=1)
        ]

        for event in events:
            result = await GatewayRunner._handle_message(runner, event)
            assert result is None

        assert adapter._pending_messages[sk].text == "first"
        assert [event.text for event in runner._queued_events[sk]] == [
            "second",
            "third",
        ]
        agent.interrupt.assert_not_called()

    @pytest.mark.asyncio
    async def test_sends_ack_when_agent_running(self):
        """First message during busy session should get a status ack."""
        runner, sentinel = _make_runner()
        runner._busy_input_mode = "interrupt"
        adapter = _make_adapter()

        event = _make_event(text="Are you working?")
        sk = build_session_key(event.source)

        # Simulate running agent
        agent = MagicMock()
        agent.get_activity_summary.return_value = {
            "api_call_count": 21,
            "max_iterations": 60,
            "current_tool": "terminal",
            "last_activity_ts": time.time(),
            "last_activity_desc": "terminal",
            "seconds_since_activity": 1.0,
        }
        runner._running_agents[sk] = agent
        runner._running_agents_ts[sk] = time.time() - 600  # 10 min ago
        interrupt_signal = MagicMock()
        adapter._active_sessions = {sk: interrupt_signal}
        runner.adapters[event.source.platform] = adapter

        result = await runner._handle_active_session_busy_message(event, sk)

        assert result is True  # handled
        # Verify ack was sent
        adapter._send_with_retry.assert_called_once()
        call_kwargs = adapter._send_with_retry.call_args
        content = call_kwargs.kwargs.get("content") or call_kwargs[1].get("content", "")
        if not content and call_kwargs.args:
            # positional args
            content = str(call_kwargs)
        assert "Interrupting" in content or "respond" in content
        assert "/stop" not in content  # no need — we ARE interrupting

        # The adapter signal is the sole post-persistence interrupt boundary;
        # a second direct agent.interrupt(text) would be an unreceipted path.
        interrupt_signal.set.assert_called_once_with()
        agent.interrupt.assert_not_called()


    @pytest.mark.asyncio
    async def test_steer_mode_calls_agent_steer_no_interrupt_no_queue(self, monkeypatch):
        """busy_input_mode='steer' injects via agent.steer() and skips queueing."""
        import gateway.run as _gr

        monkeypatch.delenv("HERMES_GATEWAY_BUSY_STEER_ACK_ENABLED", raising=False)
        monkeypatch.setattr(_gr, "_load_gateway_config", lambda: {})
        runner, sentinel = _make_runner()
        runner._busy_input_mode = "steer"
        _install_durable_steer_handoff(runner)
        adapter = _make_adapter()

        event = _make_event(text="also check the tests")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter

        agent = MagicMock()
        agent.steer = MagicMock(return_value=True)
        runner._running_agents[sk] = agent

        with patch("gateway.run.merge_pending_message_event") as mock_merge:
            await runner._handle_active_session_busy_message(event, sk)

        # VERIFY: Agent was steered, NOT interrupted
        agent.steer.assert_called_once_with("also check the tests")
        agent.interrupt.assert_not_called()

        # VERIFY: No queueing — successful steer must NOT replay as next turn
        mock_merge.assert_not_called()

        # VERIFY: Ack mentions steer wording
        adapter._send_with_retry.assert_called_once()
        call_kwargs = adapter._send_with_retry.call_args
        content = call_kwargs.kwargs.get("content") or call_kwargs[1].get("content", "")
        assert "Steered" in content or "steer" in content.lower()
        assert "Interrupting" not in content

    @pytest.mark.asyncio
    async def test_steer_mode_transcribes_voice_before_injection(self, monkeypatch):
        """A busy voice follow-up is transcribed and steered, never queued."""
        import gateway.run as _gr

        monkeypatch.delenv("HERMES_GATEWAY_BUSY_STEER_ACK_ENABLED", raising=False)
        monkeypatch.setattr(_gr, "_load_gateway_config", lambda: {})
        runner, _sentinel = _make_runner()
        runner._busy_input_mode = "steer"
        runner._should_echo_stt_transcripts = MagicMock(return_value=False)
        runner._enrich_message_with_transcription = AsyncMock(
            return_value=('"yönü teknik mimariye çevir"', ["yönü teknik mimariye çevir"])
        )
        adapter = _make_adapter()

        event = _make_event(text="")
        event.message_type = MessageType.VOICE
        event.media_urls = ["/tmp/follow-up.ogg"]
        event.media_types = ["audio/ogg"]
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter

        agent = MagicMock()
        agent.steer = MagicMock(return_value=True)
        runner._running_agents[sk] = agent

        await runner._handle_active_session_busy_message(event, sk)

        runner._enrich_message_with_transcription.assert_awaited_once_with(
            "", ["/tmp/follow-up.ogg"]
        )
        agent.steer.assert_not_called()
        agent.interrupt.assert_not_called()
        assert adapter._pending_messages[sk].text == '"yönü teknik mimariye çevir"'
        content = adapter._send_with_retry.call_args.kwargs["content"]
        assert "Queued" in content
        assert "Steered" not in content


    @pytest.mark.asyncio
    async def test_steer_mode_falls_back_to_queue_when_agent_rejects(self):
        """If agent.steer() returns False, fall back to queue behavior."""
        runner, sentinel = _make_runner()
        runner._busy_input_mode = "steer"
        _install_durable_steer_handoff(runner)
        adapter = _make_adapter()

        event = _make_event(text="empty or rejected")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter

        agent = MagicMock()
        agent.steer = MagicMock(return_value=False)  # rejected
        runner._running_agents[sk] = agent

        await runner._handle_active_session_busy_message(event, sk)

        agent.steer.assert_called_once()
        agent.interrupt.assert_not_called()
        # Fell back to queue semantics: event was stored for the next turn
        # via the FIFO path (each follow-up its own turn — no newline-merge
        # that would mash separate messages together, #43066).
        assert adapter._pending_messages.get(sk) is event

        # Ack uses queue-mode wording (not steer, not interrupt)
        call_kwargs = adapter._send_with_retry.call_args
        content = call_kwargs.kwargs.get("content") or call_kwargs[1].get("content", "")
        assert "Queued for the next turn" in content
        assert "Steered" not in content

    @pytest.mark.asyncio
    async def test_steer_mode_falls_back_to_queue_when_agent_pending(self):
        """If agent is still starting (sentinel), steer mode falls back to queue."""
        runner, sentinel = _make_runner()
        runner._busy_input_mode = "steer"
        _install_durable_steer_handoff(runner)
        adapter = _make_adapter()

        event = _make_event(text="arrived too early")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter

        # Agent is still being set up — sentinel in place
        runner._running_agents[sk] = sentinel

        await runner._handle_active_session_busy_message(event, sk)

        # Event was queued instead of steered (FIFO path, #43066)
        assert adapter._pending_messages.get(sk) is event

        call_kwargs = adapter._send_with_retry.call_args
        content = call_kwargs.kwargs.get("content") or call_kwargs[1].get("content", "")
        assert "Queued for the next turn" in content




    @pytest.mark.asyncio
    async def test_includes_status_detail_when_opted_in(self, monkeypatch):
        """Ack message should include iteration and tool info when available."""
        import gateway.run as _gr

        monkeypatch.setattr(
            _gr,
            "_load_gateway_config",
            lambda: {"display": {"platforms": {"telegram": {"busy_ack_detail": True}}}},
        )
        runner, sentinel = _make_runner()
        runner._busy_input_mode = "interrupt"
        adapter = _make_adapter()

        event = _make_event(text="yo")
        sk = build_session_key(event.source)

        agent = MagicMock()
        agent.get_activity_summary.return_value = {
            "api_call_count": 21,
            "max_iterations": 60,
            "current_tool": "terminal",
            "last_activity_ts": time.time(),
            "last_activity_desc": "terminal",
            "seconds_since_activity": 0.5,
        }
        runner._running_agents[sk] = agent
        runner._running_agents_ts[sk] = time.time() - 600  # 10 min
        runner.adapters[event.source.platform] = adapter

        await runner._handle_active_session_busy_message(event, sk)

        call_kwargs = adapter._send_with_retry.call_args
        content = call_kwargs.kwargs.get("content", "")
        assert "21/60" in content  # iteration
        assert "terminal" in content  # current tool
        assert "10 min" in content  # elapsed

    @pytest.mark.asyncio
    async def test_steer_mode_can_suppress_visible_ack_without_disabling_steer(self, monkeypatch):
        """busy_steer_ack_enabled=false keeps steering but drops the echo bubble."""
        import gateway.run as _gr

        monkeypatch.delenv("HERMES_GATEWAY_BUSY_STEER_ACK_ENABLED", raising=False)
        monkeypatch.setattr(
            _gr,
            "_load_gateway_config",
            lambda: {"display": {"platforms": {"telegram": {"busy_steer_ack_enabled": False}}}},
        )

        runner, sentinel = _make_runner()
        runner._busy_input_mode = "steer"
        _install_durable_steer_handoff(runner)
        adapter = _make_adapter()

        event = _make_event(text="also check the tests")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter

        agent = MagicMock()
        agent.steer = MagicMock(return_value=True)
        runner._running_agents[sk] = agent

        await runner._handle_active_session_busy_message(event, sk)

        agent.steer.assert_called_once_with("also check the tests")
        agent.interrupt.assert_not_called()
        adapter._send_with_retry.assert_not_called()
        assert sk not in adapter._pending_messages

    @pytest.mark.asyncio
    async def test_debounce_suppresses_rapid_acks(self):
        """Second message within 30s should NOT send another ack."""
        runner, sentinel = _make_runner()
        runner._busy_input_mode = "interrupt"
        adapter = _make_adapter()

        event1 = _make_event(text="hello?")
        # Reuse the same source so platform mock matches
        event2 = MessageEvent(
            text="still there?",
            message_type=MessageType.TEXT,
            source=event1.source,
            message_id="msg2",
        )
        sk = build_session_key(event1.source)

        agent = MagicMock()
        agent.get_activity_summary.return_value = {
            "api_call_count": 5,
            "max_iterations": 60,
            "current_tool": None,
            "last_activity_ts": time.time(),
            "last_activity_desc": "api_call",
            "seconds_since_activity": 0.5,
        }
        runner._running_agents[sk] = agent
        runner._running_agents_ts[sk] = time.time() - 60
        interrupt_signal = MagicMock()
        adapter._active_sessions = {sk: interrupt_signal}
        runner.adapters[event1.source.platform] = adapter

        # First message — should get ack
        result1 = await runner._handle_active_session_busy_message(event1, sk)
        assert result1 is True
        assert adapter._send_with_retry.call_count == 1

        # Second message within cooldown — should be queued but no ack
        result2 = await runner._handle_active_session_busy_message(event2, sk)
        assert result2 is True
        assert adapter._send_with_retry.call_count == 1  # still 1, no new ack

        # Both accepted obligations signal the adapter only after persistence;
        # ACK debounce does not suppress delivery or add a direct agent path.
        assert interrupt_signal.set.call_count == 2
        agent.interrupt.assert_not_called()

    @pytest.mark.asyncio
    async def test_steer_handoff_exception_acknowledges_uncertainty(self):
        runner, _sentinel = _make_runner()
        runner._busy_input_mode = "steer"
        adapter = _make_adapter()
        event = _make_event(text="also inspect the ledger")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter
        runner._running_agents[sk] = MagicMock()

        def uncertain_handoff(*_args, **_kwargs):
            runner._busy_queue_uncertain_sessions.add(sk)
            return False, False

        runner._admit_and_maybe_steer_event = MagicMock(
            side_effect=uncertain_handoff
        )

        handled = await runner._handle_active_session_busy_message(event, sk)

        assert handled is True
        adapter._send_with_retry.assert_awaited_once()
        content = adapter._send_with_retry.await_args.kwargs["content"]
        assert "uncertain" in content.lower()
        assert "do not resend" in content.lower()
        assert "queue is full" not in content.lower()

    @pytest.mark.asyncio
    async def test_telegram_omits_status_detail_by_default(self, monkeypatch):
        """Telegram busy acks stay concise unless busy_ack_detail is enabled."""
        import gateway.run as gateway_run

        monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
        runner, sentinel = _make_runner()
        runner._busy_input_mode = "interrupt"
        adapter = _make_adapter()

        event = _make_event(text="yo")
        sk = build_session_key(event.source)

        agent = MagicMock()
        agent.get_activity_summary.return_value = {
            "api_call_count": 21,
            "max_iterations": 60,
            "current_tool": "terminal",
            "last_activity_ts": time.time(),
            "last_activity_desc": "terminal",
            "seconds_since_activity": 0.5,
        }
        runner._running_agents[sk] = agent
        runner._running_agents_ts[sk] = time.time() - 600
        runner.adapters[event.source.platform] = adapter

        await runner._handle_active_session_busy_message(event, sk)

        content = adapter._send_with_retry.call_args.kwargs.get("content", "")
        assert "Interrupting current task" in content
        assert "21/60" not in content
        assert "terminal" not in content
        assert "10 min" not in content

    @pytest.mark.asyncio
    async def test_busy_ack_debounce_skips_steer_ack_config_load(self, monkeypatch):
        """Rapid follow-ups should not reload display config when ack is debounced."""
        import gateway.run as _gr

        def _boom():
            raise AssertionError("config should not be loaded inside ack cooldown")

        monkeypatch.delenv("HERMES_GATEWAY_BUSY_STEER_ACK_ENABLED", raising=False)
        monkeypatch.setattr(_gr, "_load_gateway_config", _boom)

        runner, sentinel = _make_runner()
        runner._busy_input_mode = "steer"
        _install_durable_steer_handoff(runner)
        adapter = _make_adapter()

        event = _make_event(text="rapid steer")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter

        agent = MagicMock()
        agent.steer = MagicMock(return_value=True)
        runner._running_agents[sk] = agent
        runner._busy_ack_ts[sk] = time.time()

        result = await runner._handle_active_session_busy_message(event, sk)

        assert result is True
        agent.steer.assert_called_once_with("rapid steer")
        adapter._send_with_retry.assert_not_called()

    @pytest.mark.asyncio
    async def test_busy_text_mode_queue_uses_durable_gateway_ledger(self):
        """Legacy text queue mode cannot bypass persist-before-ACK admission."""
        runner, sentinel = _make_runner()
        runner._busy_input_mode = "interrupt"
        runner._busy_text_mode = "queue"
        adapter = _make_adapter()

        first = _make_event(text="part one")
        second = _make_event(text="part two")
        sk = build_session_key(first.source)

        agent = MagicMock()
        runner._running_agents[sk] = agent
        runner.adapters[first.source.platform] = adapter
        runner.adapters[second.source.platform] = adapter

        result1 = await runner._handle_active_session_busy_message(first, sk)
        result2 = await runner._handle_active_session_busy_message(second, sk)

        assert result1 is True
        assert result2 is True
        assert adapter._pending_messages[sk] is first
        assert runner._queued_events[sk] == [second]
        assert getattr(first, "_busy_queue_receipt_ids", None)
        assert getattr(second, "_busy_queue_receipt_ids", None)
        agent.interrupt.assert_not_called()
        # Busy ACKs are intentionally debounced after durable admission.
        assert adapter._send_with_retry.await_count == 1

    @pytest.mark.asyncio
    async def test_interrupt_mode_followups_remain_distinct_and_latest_is_head(self):
        """Interrupt follow-ups stay separate and the newest durable interrupt
        becomes the head obligation; older accepted work remains next, never
        newline-merged into the new interrupt."""
        runner, _sentinel = _make_runner()
        runner._busy_input_mode = "interrupt"
        runner._queued_events = {}
        adapter = _make_adapter()

        # Both events must share the SAME platform object so they resolve to
        # the same adapter (a fresh MagicMock per event would not).
        shared_platform = Platform.TELEGRAM

        def _evt(text):
            src = SessionSource(
                platform=shared_platform, chat_id="123",
                chat_type="dm", user_id="user1",
            )
            return MessageEvent(text=text, message_type=MessageType.TEXT,
                                source=src, message_id=f"m-{text[:5]}")

        first = _evt("first message")
        second = _evt("second message")
        sk = build_session_key(first.source)
        runner.adapters[shared_platform] = adapter

        agent = MagicMock()
        agent._active_children = []  # real list → not demoted to queue
        runner._running_agents[sk] = agent

        await runner._handle_active_session_busy_message(first, sk)
        runner._busy_ack_ts = {}  # avoid the 30s ack-debounce early return
        await runner._handle_active_session_busy_message(second, sk)

        # Each interrupt has its own durable receipt. The latest instruction is
        # selected first; the earlier accepted instruction remains distinct.
        head = adapter._pending_messages.get(sk)
        assert head is second
        assert head.text == "second message"
        overflow = runner._queued_events.get(sk, [])
        assert [e.text for e in overflow] == ["first message"]

    @pytest.mark.asyncio
    async def test_steer_ack_env_override_can_suppress_visible_ack(self, monkeypatch):
        """Env override supports process-level suppression for gateway services."""
        import gateway.run as _gr

        monkeypatch.setenv("HERMES_GATEWAY_BUSY_STEER_ACK_ENABLED", "false")
        monkeypatch.setattr(
            _gr,
            "_load_gateway_config",
            lambda: {"display": {"platforms": {"telegram": {"busy_steer_ack_enabled": True}}}},
        )

        runner, sentinel = _make_runner()
        runner._busy_input_mode = "steer"
        _install_durable_steer_handoff(runner)
        adapter = _make_adapter()

        event = _make_event(text="steer silently")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter

        agent = MagicMock()
        agent.steer = MagicMock(return_value=True)
        runner._running_agents[sk] = agent

        await runner._handle_active_session_busy_message(event, sk)

        agent.steer.assert_called_once_with("steer silently")
        adapter._send_with_retry.assert_not_called()
        assert sk not in adapter._pending_messages


class TestBusySessionOnboardingHint:
    """First-touch hint appended to the busy-ack the first time it fires."""

    @pytest.mark.asyncio
    async def test_first_busy_ack_appends_gateway_owner_hint(self, tmp_path, monkeypatch):
        """Gateway onboarding names configuration, not the CLI-only /busy command."""
        import gateway.run as _gr

        monkeypatch.setattr(_gr, "_hermes_home", tmp_path)
        # mark_seen imports utils.atomic_yaml_write; make sure it resolves
        # against a writable dir by pointing _hermes_home at tmp_path.
        monkeypatch.setattr(_gr, "_load_gateway_config", lambda: {})

        runner, _sentinel = _make_runner()
        runner._busy_input_mode = "interrupt"
        adapter = _make_adapter()

        event = _make_event(text="ping")
        sk = build_session_key(event.source)

        agent = MagicMock()
        agent.get_activity_summary.return_value = {
            "api_call_count": 3, "max_iterations": 60,
            "current_tool": None, "last_activity_ts": time.time(),
            "last_activity_desc": "api", "seconds_since_activity": 0.1,
        }
        runner._running_agents[sk] = agent
        runner._running_agents_ts[sk] = time.time() - 5
        runner.adapters[event.source.platform] = adapter

        await runner._handle_active_session_busy_message(event, sk)

        call_kwargs = adapter._send_with_retry.call_args
        content = call_kwargs.kwargs.get("content", "")

        # Normal ack body
        assert "Interrupting" in content
        # First-touch hint appended
        assert "First-time tip" in content
        assert "display.busy_input_mode" in content
        assert "/busy" not in content

        # The flag is now persisted to tmp_path/config.yaml
        import yaml
        cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert cfg["onboarding"]["seen"]["busy_input_prompt"] is True

    @pytest.mark.asyncio
    async def test_queue_mode_hint_points_to_gateway_configuration(self, tmp_path, monkeypatch):
        """Queue-mode gateway hints do not advertise an unavailable command."""
        import gateway.run as _gr

        monkeypatch.setattr(_gr, "_hermes_home", tmp_path)
        monkeypatch.setattr(_gr, "_load_gateway_config", lambda: {})

        runner, _sentinel = _make_runner()
        runner._busy_input_mode = "queue"
        adapter = _make_adapter()

        event = _make_event(text="queue me")
        sk = build_session_key(event.source)
        runner.adapters[event.source.platform] = adapter

        agent = MagicMock()
        runner._running_agents[sk] = agent

        with patch("gateway.run.merge_pending_message_event"):
            await runner._handle_active_session_busy_message(event, sk)

        content = adapter._send_with_retry.call_args.kwargs.get("content", "")
        assert "Queued for the next turn" in content
        assert "First-time tip" in content
        assert "display.busy_input_mode" in content
        assert "/busy" not in content
        assert "/busy queue" not in content




class TestLongRunningNotificationOwnership:
    """The long-running heartbeat must stop once its run no longer owns the
    session slot or the executor finished — otherwise a stale
    'running: delegate_task' bubble outlives the run that spawned it (#12029).
    """

    def test_notification_stops_after_session_ownership_moves(self):
        from gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        runner._running_agents = {}

        original_agent = MagicMock()
        replacement_agent = MagicMock()
        runner._running_agents["sess"] = replacement_agent

        assert runner._should_emit_long_running_notification(
            "sess", original_agent, executor_task=None
        ) is False


@pytest.mark.asyncio
async def test_bounded_queue_rejection_never_interrupts_or_claims_acceptance():
    """A failed persistence receipt is visible and cannot trigger interrupt."""

    runner, _sentinel = _make_runner()
    runner._busy_input_mode = "queue"
    adapter = _make_adapter()
    event = _make_event(text="must not disappear")
    session_key = build_session_key(event.source)
    agent = MagicMock()
    runner._running_agents[session_key] = agent
    runner.adapters[event.source.platform] = adapter

    with patch.object(
        runner,
        "_queue_or_replace_pending_event",
        return_value=False,
    ):
        assert await runner._handle_active_session_busy_message(event, session_key)

    agent.interrupt.assert_not_called()
    content = adapter._send_with_retry.call_args.kwargs.get("content", "")
    assert "not accepted" in content
    assert "queued" not in content.lower()


def _install_durable_steer_handoff(runner):
    """Stub the already-covered durable handoff while testing ACK presentation."""

    def _handoff(session_key, event, running_agent, adapter, steer_text):
        steered = bool(running_agent.steer(steer_text))
        if not steered:
            assert runner._queue_or_replace_pending_event(session_key, event)
        return True, steered

    witness = MagicMock(side_effect=_handoff)
    runner._admit_and_maybe_steer_event = witness
    return witness
