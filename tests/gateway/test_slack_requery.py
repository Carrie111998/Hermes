"""Bounded Slack re-query intent and root-context recovery."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.slack.adapter import SlackAdapter, _slack_requery_intent


@pytest.fixture()
def routing_adapter():
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="redacted"))
    adapter._bot_user_id = "U_BOT"
    adapter._running = True
    adapter._app = SimpleNamespace(
        client=SimpleNamespace(
            users_info=AsyncMock(
                return_value={
                    "user": {
                        "is_bot": False,
                        "profile": {"display_name": "Tester"},
                    }
                }
            ),
            conversations_info=AsyncMock(return_value={"channel": {"name": "reports"}}),
        )
    )
    adapter.handle_message = AsyncMock()
    adapter._fetch_requery_context = AsyncMock(return_value="fresh-query-context")
    adapter._fetch_thread_context = AsyncMock(return_value="thread-report-context")
    adapter._fetch_thread_parent_text = AsyncMock(return_value="")
    adapter._collect_thread_root_images = AsyncMock(return_value=([], []))
    adapter._has_active_session_for_thread = MagicMock(return_value=False)
    adapter._reactions_enabled = MagicMock(return_value=False)
    return adapter


@pytest.mark.parametrize(
    "text",
    [
        "다시 값을 가져와줘",
        "방금 보고 데이터를 다시 조회해줘",
        "이전 결과를 최신 값으로 갱신해줘",
    ],
)
def test_requery_intent_accepts_bounded_korean_variants(text):
    assert _slack_requery_intent(text)


@pytest.mark.parametrize(
    "text",
    ["다시 설명해줘", "값 알려줘", "오늘 날씨 조회해줘", "hello everyone"],
)
def test_requery_intent_rejects_unrelated_messages(text):
    assert not _slack_requery_intent(text)


def _adapter(messages):
    adapter = object.__new__(SlackAdapter)
    adapter.config = PlatformConfig(enabled=True, extra={})
    adapter._bot_user_id = "U_BOT"
    adapter._team_bot_user_ids = {}
    client = SimpleNamespace(
        conversations_history=AsyncMock(return_value={"messages": messages})
    )
    adapter._get_client = lambda *_args, **_kwargs: client
    return adapter, client


@pytest.mark.asyncio
@pytest.mark.parametrize("channel_id", ["C_PUBLIC", "G_PRIVATE", "D_DM"])
async def test_root_requery_recovers_single_nearest_self_report(channel_id):
    adapter, client = _adapter([
        {"ts": "99.0", "user": "U_BOT", "text": "매출 보고: " + "신규 원본 값 " * 8},
        {"ts": "98.0", "user": "U_HUMAN", "text": "unrelated human text"},
    ])

    context = await adapter._fetch_requery_context(channel_id, "100.0")

    assert "신규 원본 값" in context
    assert "원본 도구를 새로 실행" in context
    assert "unrelated human text" not in context
    client.conversations_history.assert_awaited_once_with(
        channel=channel_id, latest="100.0", inclusive=False, limit=50
    )


@pytest.mark.asyncio
async def test_root_requery_asks_when_multiple_reports_are_ambiguous():
    adapter, _ = _adapter([
        {"ts": "99.0", "user": "U_BOT", "text": "첫 번째 보고 " * 10},
        {"ts": "98.0", "user": "U_BOT", "text": "두 번째 보고 " * 10},
    ])

    context = await adapter._fetch_requery_context("C1", "100.0")

    assert "어느 보고서" in context
    assert "원본 도구를 실행하지 마십시오" in context


@pytest.mark.asyncio
async def test_root_requery_ignores_self_reply_and_bot_duplicates():
    adapter, _ = _adapter([
        {
            "ts": "99.0",
            "thread_ts": "90.0",
            "user": "U_BOT",
            "text": "스레드 응답 " * 10,
        },
        {"ts": "98.0", "user": "U_OTHER_BOT", "text": "다른 봇 보고 " * 10},
    ])

    context = await adapter._fetch_requery_context("C1", "100.0")

    assert "연결할 최근 보고서를 찾지 못했습니다" in context


@pytest.mark.asyncio
async def test_root_requery_bypasses_mention_gate_and_unrelated_text_does_not(
    routing_adapter,
):
    base = {
        "type": "message",
        "channel": "C_PUBLIC",
        "channel_type": "channel",
        "user": "U_HUMAN",
        "client_msg_id": "client-1",
    }
    await routing_adapter._handle_slack_message({
        **base,
        "ts": "100.0",
        "text": "다시 값을 가져와줘",
    })
    await routing_adapter._handle_slack_message({
        **base,
        "ts": "100.0",
        "text": "다시 값을 가져와줘",
    })
    await routing_adapter._handle_slack_message({
        **base,
        "ts": "100.5",
        "user": "U_BOT",
        "bot_id": "B_SELF",
        "client_msg_id": "bot-copy",
        "text": "다시 값을 가져와줘",
    })
    await routing_adapter._handle_slack_message({
        **base,
        "ts": "101.0",
        "client_msg_id": "client-2",
        "text": "다시 설명해줘",
    })

    routing_adapter.handle_message.assert_awaited_once()
    routing_adapter._fetch_requery_context.assert_awaited_once_with(
        channel_id="C_PUBLIC", current_ts="100.0", team_id=""
    )
    delivered = routing_adapter.handle_message.await_args.args[0]
    assert delivered.source.thread_id == "100.0"
    assert delivered.channel_context == "fresh-query-context"


@pytest.mark.asyncio
async def test_thread_requery_bypasses_strict_mention_and_hydrates_thread(
    routing_adapter,
):
    routing_adapter.config.extra["strict_mention"] = True

    await routing_adapter._handle_slack_message({
        "type": "message",
        "channel": "G_PRIVATE",
        "channel_type": "group",
        "user": "U_HUMAN",
        "client_msg_id": "client-3",
        "ts": "101.0",
        "thread_ts": "100.0",
        "text": "이전 보고 최신 값으로 갱신해줘",
    })

    routing_adapter.handle_message.assert_awaited_once()
    routing_adapter._fetch_thread_context.assert_awaited_once()
