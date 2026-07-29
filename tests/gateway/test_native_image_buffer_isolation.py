import asyncio
import threading

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key


def _make_runner() -> GatewayRunner:
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake")},
    )
    runner.adapters = {}
    runner._model = "openai/gpt-4.1-mini"
    runner._base_url = None
    runner._decide_image_input_mode = lambda **_: "native"
    return runner


def _source(chat_id: str) -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        chat_type="private",
        user_name=f"user-{chat_id}",
    )


def _image_event(source: SessionSource, path: str) -> MessageEvent:
    return MessageEvent(
        text="see image",
        message_type=MessageType.PHOTO,
        source=source,
        media_urls=[path],
        media_types=["image/png"],
    )


@pytest.mark.asyncio
async def test_native_image_buffer_isolated_per_session():
    runner = _make_runner()
    source_a = _source("chat-a")
    source_b = _source("chat-b")

    await runner._prepare_inbound_message_text(
        event=_image_event(source_a, "/tmp/a.png"),
        source=source_a,
        history=[],
    )
    await runner._prepare_inbound_message_text(
        event=_image_event(source_b, "/tmp/b.png"),
        source=source_b,
        history=[],
    )

    assert runner._consume_pending_native_image_paths(build_session_key(source_a)) == ["/tmp/a.png"]
    assert runner._consume_pending_native_image_paths(build_session_key(source_b)) == ["/tmp/b.png"]


@pytest.mark.asyncio
async def test_native_image_buffer_not_cleared_by_other_sessions_without_images():
    runner = _make_runner()
    source_a = _source("chat-a")
    source_b = _source("chat-b")

    await runner._prepare_inbound_message_text(
        event=_image_event(source_a, "/tmp/a.png"),
        source=source_a,
        history=[],
    )
    await runner._prepare_inbound_message_text(
        event=MessageEvent(text="plain text", source=source_b),
        source=source_b,
        history=[],
    )

    assert runner._consume_pending_native_image_paths(build_session_key(source_a)) == ["/tmp/a.png"]
    assert runner._consume_pending_native_image_paths(build_session_key(source_b)) == []


@pytest.mark.asyncio
async def test_native_image_buffer_uses_resolved_session_key_when_provided():
    runner = _make_runner()
    source = _source("chat-a")
    runner._session_key_for_source = lambda _source: "source-derived-key"

    await runner._prepare_inbound_message_text(
        event=_image_event(source, "/tmp/a.png"),
        source=source,
        history=[],
        session_key="canonical-session-key",
    )

    assert runner._consume_pending_native_image_paths("source-derived-key") == []
    assert runner._consume_pending_native_image_paths("canonical-session-key") == ["/tmp/a.png"]


@pytest.mark.asyncio
async def test_cancelled_image_routing_worker_stays_tracked_until_exit():
    """Pre-session image resolution can touch state.db through runtime lookup."""
    runner = _make_runner()
    entered = threading.Event()
    release = threading.Event()

    def blocking_decision(**_kwargs):
        entered.set()
        if not release.wait(timeout=5):
            raise RuntimeError("image routing worker release timed out")
        return "native"

    runner._decide_image_input_mode = blocking_decision
    task = asyncio.create_task(
        runner._prepare_inbound_message_text(
            event=_image_event(_source("chat-a"), "/tmp/a.png"),
            source=_source("chat-a"),
            history=[],
        )
    )
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert any(
            not future.done() for future in runner._gateway_executor_futures
        )
        release.set()
        for _ in range(100):
            if not any(
                not future.done()
                for future in runner._gateway_executor_futures
            ):
                break
            await asyncio.sleep(0.01)
        assert not any(
            not future.done() for future in runner._gateway_executor_futures
        )
    finally:
        release.set()
        runner._shutdown_executor()
