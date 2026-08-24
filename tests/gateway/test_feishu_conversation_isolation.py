"""Behavior contracts for Feishu conversation-isolated thread turns."""

from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    _thread_metadata_for_source,
)
from gateway.session import SessionSource, build_session_key


def _source(*, thread_id: str | None = None, message_id: str | None = None):
    return SessionSource(
        platform=Platform.FEISHU,
        chat_id="oc_chat",
        chat_name="Feishu Chat",
        chat_type="group",
        user_id="ou_user",
        user_name="Tester",
        thread_id=thread_id,
        message_id=message_id,
    )


def _event(
    text: str = "/thread summarize this",
    *,
    thread_id: str | None = None,
    message_id: str | None = "om_command",
):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=_source(thread_id=thread_id, message_id=message_id),
        message_id=message_id,
    )


def _runner(adapter, *, agent_result="assistant answer"):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._adapter_for_source = MagicMock(return_value=adapter)
    runner._session_key_for_source = MagicMock(
        side_effect=lambda source: build_session_key(source)
    )
    runner._is_session_running = MagicMock(return_value=False)
    runner._interrupt_and_clear_session = AsyncMock()
    runner._handle_reset_command = AsyncMock()
    runner._dispatch_event_to_agent = AsyncMock(return_value=agent_result)
    return runner


def test_thread_command_is_gateway_dispatchable():
    from hermes_cli.commands import (
        ACTIVE_SESSION_BYPASS_COMMANDS,
        resolve_command,
        should_bypass_active_session,
    )

    command = resolve_command("thread")
    assert command is not None
    assert command.gateway_only is True
    assert command.busy_policy == "dispatch"
    assert "thread" in ACTIVE_SESSION_BYPASS_COMMANDS
    assert should_bypass_active_session("thread") is True


def test_feishu_thread_source_has_deterministic_shared_session_identity():
    alice = _source(thread_id="omt_topic")
    bob = dataclasses.replace(alice, user_id="ou_bob")
    other_topic = dataclasses.replace(alice, thread_id="omt_other")

    assert build_session_key(alice) == build_session_key(bob)
    assert build_session_key(alice) != build_session_key(other_topic)


def test_feishu_thread_metadata_carries_durable_reply_anchor():
    source = _source(thread_id="omt_topic", message_id="om_origin")

    assert _thread_metadata_for_source(source) == {
        "thread_id": "omt_topic",
        "reply_to_message_id": "om_origin",
    }
    assert _thread_metadata_for_source(source, "om_latest") == {
        "thread_id": "omt_topic",
        "reply_to_message_id": "om_latest",
    }


@pytest.mark.asyncio
async def test_parent_thread_launch_retargets_agent_and_retracts_seed():
    adapter = MagicMock()
    adapter.create_thread = AsyncMock(
        return_value=SendResult(
            success=True,
            message_id="om_seed",
            thread_id="omt_created",
        )
    )
    adapter.delete_message = AsyncMock(return_value=True)
    adapter.release_retargeted_session_guard = MagicMock(return_value=True)
    runner = _runner(adapter)
    event = _event()
    parent_key = build_session_key(event.source)

    result = await runner._handle_thread_command(event)

    assert result == "assistant answer"
    adapter.create_thread.assert_awaited_once_with(
        "oc_chat",
        "⏳",
        reply_to="om_command",
    )
    adapter.release_retargeted_session_guard.assert_called_once_with(parent_key)
    adapter.delete_message.assert_awaited_once_with("oc_chat", "om_seed")
    assert event.text == "summarize this"
    assert event.message_type == MessageType.TEXT
    assert event.source.thread_id == "omt_created"
    assert event.source.parent_chat_id == "oc_chat"
    assert event.source.message_id == "om_command"
    assert event.reply_to_message_id == "om_command"
    assert event.reply_to_text is None
    dispatched_event, dispatched_source, dispatched_key = (
        runner._dispatch_event_to_agent.await_args.args
    )
    assert dispatched_event is event
    assert dispatched_source is event.source
    assert dispatched_key == build_session_key(event.source)


@pytest.mark.asyncio
async def test_seed_cleanup_runs_when_agent_dispatch_raises():
    adapter = MagicMock()
    adapter.create_thread = AsyncMock(
        return_value=SendResult(
            success=True,
            message_id="om_seed",
            thread_id="omt_created",
        )
    )
    adapter.delete_message = AsyncMock(return_value=True)
    adapter.release_retargeted_session_guard = MagicMock(return_value=True)
    runner = _runner(adapter)
    runner._dispatch_event_to_agent.side_effect = RuntimeError("agent failed")

    with pytest.raises(RuntimeError, match="agent failed"):
        await runner._handle_thread_command(_event())

    adapter.delete_message.assert_awaited_once_with("oc_chat", "om_seed")


@pytest.mark.asyncio
async def test_missing_seed_id_never_deletes_the_user_command():
    adapter = MagicMock()
    adapter.create_thread = AsyncMock(
        return_value=SendResult(
            success=True,
            message_id=None,
            thread_id="omt_created",
        )
    )
    adapter.delete_message = AsyncMock(return_value=True)
    adapter.release_retargeted_session_guard = MagicMock(return_value=True)
    runner = _runner(adapter)

    result = await runner._handle_thread_command(_event())

    assert result == "assistant answer"
    adapter.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_thread_resets_only_that_session_before_dispatch():
    adapter = MagicMock()
    runner = _runner(adapter)
    runner._is_session_running.return_value = True
    event = _event(
        "/thread start over",
        thread_id="omt_existing",
        message_id="om_restart",
    )
    thread_key = build_session_key(event.source)

    result = await runner._handle_thread_command(event)

    assert result == "assistant answer"
    runner._interrupt_and_clear_session.assert_awaited_once()
    assert runner._interrupt_and_clear_session.await_args.args[:2] == (
        thread_key,
        event.source,
    )
    runner._handle_reset_command.assert_awaited_once()
    reset_event = runner._handle_reset_command.await_args.args[0]
    assert reset_event.text == "/new"
    assert reset_event.source.thread_id == "omt_existing"
    adapter.create_thread.assert_not_called()
    runner._dispatch_event_to_agent.assert_awaited_once_with(
        event,
        event.source,
        thread_key,
    )


@pytest.mark.asyncio
async def test_thread_command_fails_closed_without_safe_target():
    adapter = MagicMock()
    runner = _runner(adapter)

    assert await runner._handle_thread_command(_event("/thread")) == (
        "Usage: /thread <prompt>"
    )
    assert "requires a message id" in await runner._handle_thread_command(
        _event(message_id=None)
    )

    adapter.create_thread = AsyncMock(
        return_value=SendResult(success=False, error="permission denied")
    )
    assert await runner._handle_thread_command(_event()) == (
        "Failed to create Feishu thread: permission denied"
    )

    telegram_event = MessageEvent(
        text="/thread prompt",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="123",
            chat_type="dm",
        ),
        message_id="1",
    )
    assert "only on Feishu" in await runner._handle_thread_command(telegram_event)


class _RecordingFeishuAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(
            PlatformConfig(enabled=True, token="fake"),
            Platform.FEISHU,
        )
        self.config.typing_indicator = False
        self.sent = []
        self.inline_images = []

    async def connect(self):
        return True

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(
            {
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata,
            }
        )
        return SendResult(success=True, message_id="om_final")

    async def send_image_file(
        self,
        chat_id,
        image_path,
        caption=None,
        reply_to=None,
        metadata=None,
        **kwargs,
    ):
        self.inline_images.append(
            {
                "chat_id": chat_id,
                "image_path": image_path,
                "caption": caption,
                "reply_to": reply_to,
                "metadata": metadata,
            }
        )
        return SendResult(success=True, message_id="om_image")

    async def send_typing(self, chat_id, metadata=None):
        return None

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


@pytest.mark.asyncio
async def test_final_delivery_recomputes_metadata_after_thread_retarget():
    adapter = _RecordingFeishuAdapter()
    event = _event()
    parent_key = build_session_key(event.source)

    async def handler(mutated_event):
        mutated_event.source = dataclasses.replace(
            mutated_event.source,
            thread_id="omt_created",
            message_id="om_command",
        )
        mutated_event.reply_to_message_id = "om_command"
        return "final answer"

    adapter.set_message_handler(handler)
    await adapter._process_message_background(event, parent_key)

    assert adapter.sent == [
        {
            "chat_id": "oc_chat",
            "content": "final answer",
            "reply_to": "om_command",
            "metadata": {
                "thread_id": "omt_created",
                "reply_to_message_id": "om_command",
                "notify": True,
            },
        }
    ]


@pytest.mark.asyncio
async def test_inline_media_uses_same_retargeted_conversation(tmp_path):
    image = tmp_path / "chart.png"
    image.write_bytes(b"image")
    adapter = _RecordingFeishuAdapter()
    event = _event()
    parent_key = build_session_key(event.source)

    async def handler(mutated_event):
        mutated_event.source = dataclasses.replace(
            mutated_event.source,
            thread_id="omt_created",
            message_id="om_command",
        )
        mutated_event.reply_to_message_id = "om_command"
        return f"Chart summary\nMEDIA:{image}"

    adapter.set_message_handler(handler)
    await adapter._process_message_background(event, parent_key)

    assert adapter.sent == []
    assert adapter.inline_images == [
        {
            "chat_id": "oc_chat",
            "image_path": str(image),
            "caption": "Chart summary",
            "reply_to": "om_command",
            "metadata": {
                "thread_id": "omt_created",
                "reply_to_message_id": "om_command",
                "notify": True,
            },
        }
    ]


@pytest.mark.asyncio
async def test_busy_bypass_recomputes_metadata_after_thread_retarget():
    adapter = _RecordingFeishuAdapter()
    event = _event()
    parent_key = build_session_key(event.source)
    adapter._active_sessions[parent_key] = asyncio.Event()

    async def handler(mutated_event):
        mutated_event.source = dataclasses.replace(
            mutated_event.source,
            thread_id="omt_created",
            message_id="om_command",
        )
        mutated_event.reply_to_message_id = "om_command"
        return "busy-path final"

    adapter.set_message_handler(handler)
    await adapter.handle_message(event)

    assert adapter.sent == [
        {
            "chat_id": "oc_chat",
            "content": "busy-path final",
            "reply_to": "om_command",
            "metadata": {
                "thread_id": "omt_created",
                "reply_to_message_id": "om_command",
                "notify": True,
            },
        }
    ]


@pytest.mark.asyncio
async def test_retarget_release_unblocks_queued_parent_turn():
    adapter = _RecordingFeishuAdapter()
    parent_event = _event()
    parent_key = build_session_key(parent_event.source)
    owner_task = asyncio.current_task()
    assert owner_task is not None

    adapter._active_sessions[parent_key] = asyncio.Event()
    adapter._session_tasks[parent_key] = owner_task
    adapter._pending_messages[parent_key] = _event(
        "queued parent follow-up",
        message_id="om_followup",
    )
    started = []

    def start_processing(event, session_key, *, interrupt_event=None):
        started.append((event, session_key, interrupt_event))
        return True

    adapter._start_session_processing = start_processing

    assert adapter.release_retargeted_session_guard(parent_key) is True
    assert parent_key not in adapter._active_sessions
    assert parent_key not in adapter._session_tasks
    assert parent_key not in adapter._pending_messages
    assert [(event.text, key) for event, key, _guard in started] == [
        ("queued parent follow-up", parent_key)
    ]


@pytest.mark.asyncio
async def test_adapter_serializes_thread_creation_per_parent_chat():
    from plugins.platforms.feishu.adapter import FeishuAdapter

    adapter = FeishuAdapter(PlatformConfig())
    adapter._client = SimpleNamespace()
    active_sends = 0
    max_active_sends = 0

    async def fake_send(*, reply_to, **kwargs):
        nonlocal active_sends, max_active_sends
        active_sends += 1
        max_active_sends = max(max_active_sends, active_sends)
        try:
            await asyncio.sleep(0)
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(
                    message_id=f"seed-{reply_to}",
                    thread_id=f"thread-{reply_to}",
                    root_id=None,
                ),
            )
        finally:
            active_sends -= 1

    adapter._feishu_send_with_retry = fake_send
    first_result, second_result = await asyncio.gather(
        adapter.create_thread("oc_chat", "one", reply_to="om_one"),
        adapter.create_thread("oc_chat", "two", reply_to="om_two"),
    )

    assert max_active_sends == 1
    assert first_result.thread_id == "thread-om_one"
    assert second_result.thread_id == "thread-om_two"


@pytest.mark.asyncio
async def test_adapter_creates_thread_reply_and_uses_message_id_fallback():
    from plugins.platforms.feishu.adapter import FeishuAdapter

    captured = {}

    def reply(request):
        captured["request"] = request
        return SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(
                message_id="om_seed",
                thread_id=None,
                root_id=None,
            ),
        )

    async def run_direct(function, *args, **kwargs):
        return function(*args, **kwargs)

    adapter = FeishuAdapter(PlatformConfig())
    adapter._client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(reply=reply)))
    )
    adapter._run_blocking = AsyncMock(side_effect=run_direct)

    result = await adapter.create_thread(
        "oc_chat",
        "Creating isolated conversation",
        reply_to="om_command",
    )

    request = captured["request"]
    assert request.message_id == "om_command"
    assert request.request_body.reply_in_thread is True
    assert result.success is True
    assert result.message_id == "om_seed"
    assert result.thread_id == "om_seed"


@pytest.mark.asyncio
async def test_adapter_deletes_seed_through_sdk_executor():
    from plugins.platforms.feishu.adapter import FeishuAdapter

    adapter = FeishuAdapter(PlatformConfig())
    delete_call = MagicMock()
    adapter._client = SimpleNamespace(
        im=SimpleNamespace(
            v1=SimpleNamespace(
                message=SimpleNamespace(delete=delete_call),
            )
        )
    )
    response = SimpleNamespace(success=lambda: True)
    adapter._run_blocking = AsyncMock(return_value=response)

    assert await adapter.delete_message("oc_chat", "om_seed") is True
    function, request = adapter._run_blocking.await_args.args
    assert function is delete_call
    assert request.message_id == "om_seed"


@pytest.mark.asyncio
async def test_card_callback_resolves_only_its_recorded_thread_session(monkeypatch):
    from plugins.platforms.feishu.adapter import FeishuAdapter
    from tools import approval as approval_module

    adapter = FeishuAdapter(PlatformConfig())
    first_key = build_session_key(_source(thread_id="omt_first"))
    second_key = build_session_key(_source(thread_id="omt_second"))
    adapter._approval_state = {
        1: {
            "session_key": first_key,
            "message_id": "om_first_card",
            "chat_id": "oc_chat",
        },
        2: {
            "session_key": second_key,
            "message_id": "om_second_card",
            "chat_id": "oc_chat",
        },
    }
    resolved = []
    monkeypatch.setattr(
        approval_module,
        "resolve_gateway_approval",
        lambda session_key, choice: resolved.append((session_key, choice)) or 1,
    )

    await adapter._resolve_approval(
        2,
        "once",
        "Tester",
        open_id="ou_user",
        chat_id="oc_chat",
    )

    assert resolved == [(second_key, "once")]
    assert 1 in adapter._approval_state
    assert 2 not in adapter._approval_state


@pytest.mark.asyncio
async def test_identical_streamed_final_is_resent_below_progress():
    from gateway.stream_consumer import (
        GatewayStreamConsumer,
        StreamConsumerConfig,
    )
    from plugins.platforms.feishu.adapter import FeishuAdapter

    adapter = FeishuAdapter(PlatformConfig())
    preview_sent = asyncio.Event()

    async def send(*args, **kwargs):
        if not preview_sent.is_set():
            preview_sent.set()
            return SendResult(success=True, message_id="om_preview")
        return SendResult(success=True, message_id="om_final")

    adapter.send = AsyncMock(side_effect=send)
    adapter.edit_message = AsyncMock(return_value=SendResult(success=True))
    adapter.delete_message = AsyncMock(return_value=True)
    consumer = GatewayStreamConsumer(
        adapter,
        "oc_chat",
        StreamConsumerConfig(
            transport="auto",
            chat_type="group",
            edit_interval=0.01,
            buffer_threshold=5,
            cursor="",
            fresh_final_after_seconds=0.0,
        ),
        metadata={
            "thread_id": "omt_created",
            "reply_to_message_id": "om_command",
        },
    )

    consumer.on_delta("Full answer")
    task = asyncio.create_task(consumer.run())
    await asyncio.wait_for(preview_sent.wait(), timeout=1.0)
    consumer.finish()
    await task

    assert adapter.send.await_count == 2
    assert [
        call.kwargs["content"] for call in adapter.send.await_args_list
    ] == ["Full answer", "Full answer"]
    adapter.edit_message.assert_not_awaited()
    adapter.delete_message.assert_awaited_once_with("oc_chat", "om_preview")
    assert consumer.final_response_sent is True


def test_feishu_finals_use_fresh_message_below_progress():
    from plugins.platforms.feishu.adapter import FeishuAdapter

    adapter = FeishuAdapter(PlatformConfig())
    assert adapter.prefers_fresh_final_streaming(
        "answer",
        {"thread_id": "omt_created"},
    ) is True
