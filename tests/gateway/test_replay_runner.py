import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.replay import ReplayPlan, current_replay_context
from gateway.run import GatewayRunner
from gateway.session import SessionSource


class FakeReplayAdapter(BasePlatformAdapter):
    def __init__(self, config=None):
        super().__init__(
            config or PlatformConfig(enabled=True, extra={"group_sessions_per_user": False, "thread_sessions_per_user": False}),
            Platform.WHATSAPP,
        )
        self.connect_called = False

    async def connect(self) -> bool:
        self.connect_called = True
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id: str, content: str, reply_to=None, metadata=None) -> SendResult:
        return SendResult(success=False, error="live send should be guarded in replay")

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id, "name": chat_id}

    async def replay_bridge_messages(self, messages, *, bypass_require_mention=True) -> int:
        for message in messages:
            event = MessageEvent(
                text=message.get("body", ""),
                message_type=MessageType.TEXT,
                source=SessionSource(
                    platform=Platform.WHATSAPP,
                    chat_id=message.get("chatId", "120363@g.us"),
                    chat_name=message.get("chatName", "Ops"),
                    chat_type="group" if message.get("isGroup", True) else "dm",
                    user_id=message.get("senderId", "user@s.whatsapp.net"),
                    user_name=message.get("senderName", "Sky"),
                ),
                raw_message=dict(message),
                message_id=message.get("messageId"),
            )
            await self.handle_message(event)
        return len(messages)


@pytest.mark.asyncio
async def test_gateway_runner_replay_uses_no_connect_build_and_captures_outbound(monkeypatch):
    runner = GatewayRunner(GatewayConfig(platforms={Platform.WHATSAPP: PlatformConfig(enabled=True, extra={})}))
    adapter = FakeReplayAdapter()
    build_calls = []

    async def fake_build(platform, platform_config, *, connect=True):
        build_calls.append((platform, connect))
        runner._wire_adapter(adapter)
        return adapter, None

    async def fake_handle(event):
        ctx = current_replay_context()
        assert ctx is not None
        assert ctx.execution_mode == "replay"
        assert ctx.run_id == "run-1"
        return "captured reply"

    monkeypatch.setattr(runner, "_build_adapter", fake_build)
    monkeypatch.setattr(runner, "_handle_message", AsyncMock(side_effect=fake_handle))

    result = await runner.replay(ReplayPlan(
        platform="whatsapp",
        run_id="run-1",
        attempt_id="attempt-1",
        messages=({"messageId": "m1", "body": "hello", "timestamp": 100},),
    ))

    assert build_calls == [(Platform.WHATSAPP, False)]
    assert adapter.connect_called is False
    assert runner._handle_message.await_count == 1
    assert result.processed == 1
    assert result.outbound[0]["kind"] == "send"
    assert result.outbound[0]["kwargs"]["content"] == "captured reply"
    assert result.outbound[0]["message_id"] == "replay-1"


@pytest.mark.asyncio
async def test_replay_blocks_slash_command_side_effects(monkeypatch):
    runner = GatewayRunner(GatewayConfig(platforms={Platform.WHATSAPP: PlatformConfig(enabled=True, extra={})}))
    adapter = FakeReplayAdapter()

    async def fake_build(platform, platform_config, *, connect=True):
        runner._wire_adapter(adapter)
        return adapter, None

    monkeypatch.setattr(runner, "_build_adapter", fake_build)

    result = await runner.replay(ReplayPlan(
        platform="whatsapp",
        run_id="run-cmd",
        attempt_id="attempt-cmd",
        messages=({"messageId": "m1", "body": "/reset", "timestamp": 100},),
    ))

    assert result.processed == 1
    assert result.blocked_commands == [{
        "command": "new",
        "platform": "whatsapp",
        "chat_id": "120363@g.us",
        "reason": "replay_command_side_effect_blocked",
    }]
    assert result.outbound == []


def _wa_adapter(tmp_path):
    from gateway.platforms.whatsapp import WhatsAppAdapter

    return WhatsAppAdapter(PlatformConfig(
        enabled=True,
        extra={
            "session_path": str(tmp_path / "wa-session"),
            "turn_debounce_ms": 1500,
            "group_policy": "open",
            "dm_policy": "open",
            "group_sessions_per_user": False,
            "thread_sessions_per_user": False,
        },
    ))


def _small_tgg_bridge_corpus():
    return [
        {
            "messageId": "m1",
            "chatId": "120363111@g.us",
            "chatName": "TGG Ops",
            "isGroup": True,
            "senderId": "60120000000@s.whatsapp.net",
            "senderName": "Sky",
            "body": "first update",
            "timestamp": 100,
        },
        {
            "messageId": "m2",
            "chatId": "120363111@g.us",
            "chatName": "TGG Ops",
            "isGroup": True,
            "senderId": "60120000000@s.whatsapp.net",
            "senderName": "Sky",
            "body": "second update",
            "timestamp": 101,
        },
        {
            "messageId": "m3",
            "chatId": "120363111@g.us",
            "chatName": "TGG Ops",
            "isGroup": True,
            "senderId": "60120000000@s.whatsapp.net",
            "senderName": "Sky",
            "body": "later update",
            "timestamp": 110,
        },
    ]


@pytest.mark.asyncio
async def test_native_replay_matches_current_whatsapp_harness_turn_grouping(tmp_path, monkeypatch):
    corpus = _small_tgg_bridge_corpus()
    golden_source_ids = [["m1", "m2"], ["m3"]]

    harness_adapter = _wa_adapter(tmp_path / "harness")
    harness_events = []

    async def capture_harness(event):
        harness_events.append(event)

    harness_adapter.handle_message = capture_harness
    await harness_adapter.replay_bridge_messages(corpus)
    harness_ids = [event.raw_message.get("sourceMessageIds", [event.message_id]) for event in harness_events]
    assert harness_ids == golden_source_ids

    runner = GatewayRunner(GatewayConfig(platforms={Platform.WHATSAPP: PlatformConfig(enabled=True, extra={})}))
    native_adapter = _wa_adapter(tmp_path / "native")
    native_events = []

    async def fake_build(platform, platform_config, *, connect=True):
        runner._wire_adapter(native_adapter)
        return native_adapter, None

    async def capture_native(event):
        native_events.append(event)
        return None

    monkeypatch.setattr(runner, "_build_adapter", fake_build)
    monkeypatch.setattr(runner, "_handle_message", AsyncMock(side_effect=capture_native))

    await runner.replay(ReplayPlan(platform="whatsapp", messages=tuple(corpus)))
    native_ids = [event.raw_message.get("sourceMessageIds", [event.message_id]) for event in native_events]

    assert native_ids == harness_ids == golden_source_ids


def test_replay_plan_loads_typed_plan_and_corpus(tmp_path):
    corpus_path = tmp_path / "bridge.jsonl"
    corpus_path.write_text('\n'.join(json.dumps(row) for row in _small_tgg_bridge_corpus()), encoding="utf-8")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({
        "platform": "whatsapp",
        "delivery_mode": "drop",
        "corpus": {"path": corpus_path.name},
    }), encoding="utf-8")

    plan = ReplayPlan.from_path(plan_path)

    assert plan.platform == "whatsapp"
    assert plan.delivery_mode == "drop"
    assert len(plan.messages) == 3
    assert plan.source_path == str(corpus_path)


def test_send_message_tool_is_captured_in_replay_context():
    from gateway.replay import replay_context
    from tools.send_message_tool import _handle_send

    plan = ReplayPlan(platform="whatsapp", run_id="run-tool", attempt_id="attempt-tool")
    with replay_context(plan) as ctx:
        payload = json.loads(_handle_send({"target": "whatsapp:120363@g.us", "message": "do not send live"}))

    assert payload == {"success": True, "message_id": "replay-1", "replay": "capture"}
    assert ctx.outbound[0]["kind"] == "send_message_tool"
    assert ctx.outbound[0]["kwargs"]["target"] == "whatsapp:120363@g.us"


def test_replay_context_sets_pa_history_cap():
    from gateway.replay import replay_context, set_replay_turn_history_before_ts
    from tools.pa_business_tools import _history_before_ts_cap

    plan = ReplayPlan(platform="whatsapp", history_before_ts=111)
    with replay_context(plan):
        assert _history_before_ts_cap() == 111
        set_replay_turn_history_before_ts(222)
        assert _history_before_ts_cap() == 222
