import threading
from unittest.mock import MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource, build_session_key
from gateway.run import GatewayRunner


class _PendingAdapter:
    def __init__(self):
        self._pending_messages = {}


def _make_runner(profile_home):
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")})
    runner.adapters = {Platform.TELEGRAM: _PendingAdapter()}
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._queued_events = {}
    runner._busy_queue_lock = threading.RLock()
    runner._busy_queue_uncertain_sessions = set()
    runner._busy_queue_uncertain_digests = set()
    runner._busy_queue_persist_ready = MagicMock(return_value=None)
    runner._busy_queue_profile_home = lambda source: profile_home
    runner._pending_approvals = {}
    runner._voice_mode = {}
    runner._is_user_authorized = lambda _source: True
    runner.session_store = MagicMock()
    return runner


@pytest.mark.asyncio
async def test_handle_message_does_not_priority_interrupt_photo_followup(tmp_path):
    runner = _make_runner(tmp_path)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm", user_id="u1")
    session_key = build_session_key(source)
    running_agent = MagicMock()
    runner._running_agents[session_key] = running_agent

    event = MessageEvent(
        text="caption",
        message_type=MessageType.PHOTO,
        source=source,
        media_urls=["/tmp/photo-a.jpg"],
        media_types=["image/jpeg"],
    )

    result = await runner._handle_message(event)

    assert result is None
    running_agent.interrupt.assert_not_called()
    assert runner.adapters[Platform.TELEGRAM]._pending_messages[session_key] is event


@pytest.mark.asyncio
async def test_priority_photo_over_cap_returns_rejection_without_partial_merge(tmp_path):
    runner = _make_runner(tmp_path)
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
        user_id="u1",
    )
    session_key = build_session_key(source)
    running_agent = MagicMock()
    runner._running_agents[session_key] = running_agent
    cap = runner._BUSY_QUEUE_MAX_PENDING

    for i in range(cap):
        result = await runner._handle_message(
            MessageEvent(
                text=f"caption-{i}",
                message_type=MessageType.PHOTO,
                source=source,
                message_id=f"p-{i}",
                media_urls=[f"/tmp/photo-{i}.jpg"],
                media_types=["image/jpeg"],
            )
        )
        assert result is None

    head = runner.adapters[Platform.TELEGRAM]._pending_messages[session_key]
    before = (head.text, list(head.media_urls), list(head.media_types))
    rejected = await runner._handle_message(
        MessageEvent(
            text="caption-rejected",
            message_type=MessageType.PHOTO,
            source=source,
            message_id="p-rejected",
            media_urls=["/tmp/photo-rejected.jpg"],
            media_types=["image/jpeg"],
        )
    )

    assert rejected is not None
    assert "not accepted" in str(rejected).lower()
    assert (head.text, head.media_urls, head.media_types) == before
    running_agent.interrupt.assert_not_called()
