import asyncio
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageDispatchDisposition,
    MessageEvent,
    SendResult,
)
from gateway.session import SessionSource, build_session_key


class LifecycleAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(
            PlatformConfig(enabled=True, token="test", typing_indicator=False),
            Platform.TELEGRAM,
        )
        self.processing_hooks = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="sent-1")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}

    async def on_processing_start(self, event):
        self.processing_hooks.append(("start", event.message_id, None))

    async def on_processing_complete(self, event, outcome):
        self.processing_hooks.append(("complete", event.message_id, outcome))


def _priority_runner(adapter, session_key):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner.config = SimpleNamespace(
        group_sessions_per_user=True,
        thread_sessions_per_user=False,
    )
    runner._startup_restore_in_progress = False
    runner._update_prompt_pending = {}
    runner._running_agents = {session_key: SimpleNamespace(interrupt=lambda _text: None)}
    runner._running_agents_ts = {}
    runner._queued_events = {}
    runner._draining = False
    runner._busy_input_mode = "queue"
    runner._restart_requested = False
    runner._session_db = None
    runner.session_store = None
    return runner


@pytest.mark.asyncio
async def test_base_priority_queue_transfers_lifecycle_without_early_complete():
    adapter = LifecycleAdapter()
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        chat_type="dm",
        user_id="user-1",
    )
    session_key = build_session_key(source)
    runner = _priority_runner(adapter, session_key)
    adapter.set_message_handler(runner._handle_message)

    event = MessageEvent(
        text="second request",
        source=source,
        message_id="request-2",
        internal=True,
    )

    disposition = await adapter.handle_message(event)
    task = adapter._session_tasks[session_key]
    await asyncio.wait_for(task, timeout=1)

    assert disposition is MessageDispatchDisposition.BACKGROUND_STARTED
    assert event.dispatch_disposition is MessageDispatchDisposition.PENDING_QUEUED
    assert getattr(event, "_processing_lifecycle_transferred", False) is True
    assert adapter._pending_messages[session_key] is event
    assert adapter.processing_hooks == [("start", "request-2", None)]
    assert session_key not in adapter._active_sessions
