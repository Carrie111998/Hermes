import asyncio
from unittest.mock import AsyncMock

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from gateway.wake import deliver_wake


def test_control_plane_wake_marks_event_to_suppress_public_delivery():
    """A caller can run a wake while explicitly forbidding platform output."""
    adapter = type("Adapter", (), {"supports_async_delivery": True})()
    adapter.handle_message = AsyncMock()
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="channel-1",
        chat_type="group",
        user_id="internal-agent-mail:default:SilverHarbor",
        profile="default",
        internal_session_id="agent-mail:default:SilverHarbor",
    )

    asyncio.run(
        deliver_wake(
            adapter,
            text="internal mail",
            source=source,
            message_id="agent-mail:42",
            suppress_public_delivery=True,
        )
    )

    event = adapter.handle_message.await_args.args[0]
    assert isinstance(event, MessageEvent)
    assert event.internal is True
    assert event.metadata["suppress_public_delivery"] is True
