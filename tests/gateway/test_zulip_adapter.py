"""Behavior tests for the direct-conversation Zulip inbound gateway adapter."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx

from plugins.platforms.zulip.adapter import (
    _ZULIP_SAFE_MESSAGE_LENGTH,
    _format_zulip_presentation,
    _ZulipAPI,
    _REQUEST_TIMEOUT_SECONDS,
    ZulipAdapter,
    _split_zulip_message,
    extract_direct_text,
    extract_mention_text,
)


def _config() -> SimpleNamespace:
    return SimpleNamespace(extra={})


def _event(
    content: str,
    *,
    sender_id: int = 7,
    message_id: int = 101,
    subject: str = "Unraid",
    stream: str = "Infrastructure",
    attachments: list | None = None,
    bot_user_id: int = 99,
    bot_full_name: str = "Hermes",
) -> dict:
    message: dict = {
        "id": message_id,
        "type": "stream",
        "display_recipient": stream,
        "subject": subject,
        "sender_id": sender_id,
        "sender_full_name": "Master",
        "sender_email": "master@example.test",
        "content": content,
        "timestamp": 1_700_000_000,
    }
    if attachments is not None:
        message["attachments"] = attachments
    return {
        "type": "message",
        "id": message_id + 1000,
        "message": message,
    }


def _wire(adapter: ZulipAdapter) -> None:
    adapter._bot_user_id = 99
    adapter._bot_full_name = "Hermes"
    adapter.handle_message = AsyncMock()


# -- text helpers ---------------------------------------------------------

def test_extract_mention_text_accepts_zulip_markdown_mention() -> None:
    assert extract_mention_text("@**Hermes** 查询当前状态", "Hermes") == "查询当前状态"


def test_extract_mention_text_rejects_non_mention() -> None:
    assert extract_mention_text("Hermes 查询当前状态", "Hermes") is None


def test_extract_direct_text_passes_plain_through() -> None:
    # Plain text without an explicit mention must surface unchanged so that
    # every non-bot stream message is dispatched.
    assert extract_direct_text("hello", "Hermes") == "hello"


def test_extract_direct_text_strips_markdown_mention() -> None:
    # @Hermes-style messages remain accepted; the mention is stripped but
    # the body reaches the runtime.
    assert extract_direct_text("@**Hermes** hello again", "Hermes") == "hello again"


def test_extract_direct_text_strips_plain_mention() -> None:
    assert extract_direct_text("@Hermes body", "Hermes") == "body"


# -- dispatch behaviour (Case A/B/C/D contract) --------------------------

def test_message_event_dispatches_without_mention() -> None:
    """Case A: a plain message in any topic is dispatched."""
    adapter = ZulipAdapter(_config())
    _wire(adapter)

    asyncio.run(adapter._handle_event(_event("hello")))

    dispatched = adapter.handle_message.await_args.args[0]
    assert dispatched.text == "hello"
    assert dispatched.message_id == "101"
    assert dispatched.source.platform.value == "zulip"
    assert dispatched.source.chat_id == "Infrastructure"
    assert dispatched.source.thread_id == "Unraid"
    assert dispatched.source.message_id == "101"


def test_message_event_dispatches_with_mention() -> None:
    """Case B: explicit @Hermes still works; mention is stripped."""
    adapter = ZulipAdapter(_config())
    _wire(adapter)

    asyncio.run(adapter._handle_event(_event("@**Hermes** hello again")))

    dispatched = adapter.handle_message.await_args.args[0]
    assert dispatched.text == "hello again"


def test_message_event_ignores_bot_self_reply() -> None:
    """Case C: bot's own messages echo back through the long-poll queue but
    must NEVER trigger another inbound runtime dispatch."""
    adapter = ZulipAdapter(_config())
    _wire(adapter)

    asyncio.run(adapter._handle_event(_event("我自己的回复", sender_id=99)))

    adapter.handle_message.assert_not_awaited()


def test_message_event_dispatches_in_a_new_topic() -> None:
    """Case D: a plain message in a *different* topic auto-dispatches and the
    binding stays scoped to that new topic."""
    adapter = ZulipAdapter(_config())
    _wire(adapter)

    asyncio.run(adapter._handle_event(
        _event("检查一下 OpenClaw", subject="OpenClaw", stream="Forge", message_id=202),
    ))

    dispatched = adapter.handle_message.await_args.args[0]
    assert dispatched.text == "检查一下 OpenClaw"
    assert dispatched.source.chat_id == "Forge"
    assert dispatched.source.thread_id == "OpenClaw"
    assert dispatched.message_id == "202"


# -- ignored event types --------------------------------------------------

def test_message_event_ignores_reactions_typing_presence() -> None:
    """Non-``message`` event types must NEVER trigger dispatch."""
    adapter = ZulipAdapter(_config())
    _wire(adapter)

    for ignored in (
        {"type": "reaction", "id": 1, "message": {"id": 1}},
        {"type": "typing", "id": 2, "message": {"id": 1}},
        {"type": "presence", "id": 3},
        {"type": "update_message", "id": 4, "message": {"id": 1}},
    ):
        asyncio.run(adapter._handle_event(ignored))

    adapter.handle_message.assert_not_awaited()


def test_message_event_ignores_direct_messages() -> None:
    """Private DM messages are out of scope: only stream messages dispatch."""
    adapter = ZulipAdapter(_config())
    _wire(adapter)

    dm_event = _event("hello")
    dm_event["message"]["type"] = "private"
    asyncio.run(adapter._handle_event(dm_event))

    adapter.handle_message.assert_not_awaited()


def test_message_event_dispatches_attachment_only_post() -> None:
    """Attachment-only messages (empty content, with attachments) must still
    dispatch so the existing zulip_fetch_attachment hook remains reachable."""
    adapter = ZulipAdapter(_config())
    _wire(adapter)

    asyncio.run(adapter._handle_event(
        _event("", attachments=[{"url": "/user_uploads/abc.png"}]),
    ))

    adapter.handle_message.assert_awaited_once()


def test_message_event_skips_pure_empty_message() -> None:
    """Empty-content messages with no attachments have no usable runtime
    input and should be dropped without dispatching."""
    adapter = ZulipAdapter(_config())
    _wire(adapter)

    asyncio.run(adapter._handle_event(_event("   ")))

    adapter.handle_message.assert_not_awaited()


# -- existing invariants preserved ---------------------------------------

def test_message_event_binds_stream_topic_and_message_id() -> None:
    adapter = ZulipAdapter(_config())
    _wire(adapter)

    asyncio.run(adapter._handle_event(_event("@**Hermes** 查询当前状态")))

    dispatched = adapter.handle_message.await_args.args[0]
    assert dispatched.text == "查询当前状态"
    assert dispatched.message_id == "101"
    assert dispatched.source.platform.value == "zulip"
    assert dispatched.source.chat_id == "Infrastructure"
    assert dispatched.source.thread_id == "Unraid"
    assert dispatched.source.message_id == "101"


# -- presentation -----------------------------------------------------------


def test_long_report_gets_a_compact_executive_summary_before_sections() -> None:
    report = """# Q2 Operations Report

## Overall Status
- On track

## Key Findings
- Conversion improved

## Risks
- Inventory is tight

## Recommended Actions
- Replenish priority SKUs

""" + ("Detailed evidence line.\n" * 120)

    presented = _format_zulip_presentation(report)

    assert presented.startswith("## Executive Summary\n")
    assert 3 <= len(presented.split("\n\n", 1)[0].splitlines()) <= 8
    assert "## Overall Status" in presented
    assert "## Risks" in presented


def test_summary_uses_only_actual_status_content_and_skips_generic_reports() -> None:
    report = "## Overall Status\n- On track\n\n## Risks\n- Inventory tight\n\n" + ("Evidence.\n" * 200)
    presented = _format_zulip_presentation(report)
    assert "- **Overall Status:** On track" in presented
    assert "- **Risks:** Inventory tight" in presented
    assert "Report:" not in presented
    assert _format_zulip_presentation("Plain detail.\n" * 300).startswith("Plain detail.")


def test_health_report_uses_compact_status_list() -> None:
    report = """## Health Check

| Component | Status | Detail |
| --- | --- | --- |
| Gateway | healthy | responding |
| Redis | warning | high memory |
| Database | failed | unavailable |
"""
    presented = _format_zulip_presentation(report)
    assert "- ✅ **Gateway** — healthy" in presented
    assert "- ⚠️ **Redis** — warning · high memory" in presented
    assert "- ❌ **Database** — failed · unavailable" in presented


def test_presentation_polish_normalizes_bullets_numbers_and_empty_sections() -> None:
    report = """## Key Metrics

* GMV: 120000
• CVR: 4.12345%
- Ratio: 0.3567

## Risks

## Analysis

* Positive trend
"""
    presented = _format_zulip_presentation(report)
    assert "- GMV: 120,000" in presented
    assert "- CVR: 4.12%" in presented
    assert "- Ratio: 0.36" in presented
    assert "## Risks" not in presented
    assert "\n---\n\n## Analysis" in presented


def test_existing_executive_summary_is_not_duplicated() -> None:
    report = "## Executive Summary\n- Already supplied\n\n" + ("Detail\n" * 300)

    assert _format_zulip_presentation(report).count("## Executive Summary") == 1


def test_wide_table_becomes_metrics_and_analysis_without_data_loss() -> None:
    report = """## Performance

| GMV | ROI | UV | CVR | Analysis |
| --- | --- | --- | --- | --- |
| 120000 | 3.2 | 9000 | 4.1% | Conversion improved after creative refresh, but inventory risk remains. |
"""

    presented = _format_zulip_presentation(report)

    assert "### Key Metrics" in presented
    assert "### Analysis" in presented
    assert "| Metric | Value |" in presented
    assert "| GMV | 120000 |" in presented
    assert "- **Analysis:** Conversion improved after creative refresh, but inventory risk remains." in presented
    assert "| GMV | ROI | UV | CVR | Analysis |" not in presented


def test_small_table_and_code_blocks_are_preserved() -> None:
    report = """| Metric | Value |
| --- | --- |
| ROI | 3.2 |

```text
| log | literal |
* GMV: 120000
## Risks

## Analysis
```
"""

    assert _format_zulip_presentation(report) == report


def test_malformed_health_table_is_preserved_instead_of_raising() -> None:
    report = """## Health Check

| Component | Status | Detail |
| --- | --- | --- |
| Gateway | healthy | responding | unexpected |
"""

    assert _format_zulip_presentation(report) == report


# -- outbound chunking ------------------------------------------------------

def _strip_segment_labels(parts: list[str]) -> str:
    return "".join(part.split("\n", 1)[1] for part in parts)


def test_short_message_is_sent_unchanged() -> None:
    content = "简短 Markdown **报告**"
    assert _split_zulip_message(content) == [content]


def test_message_just_below_safe_limit_is_not_segmented() -> None:
    content = "x" * (_ZULIP_SAFE_MESSAGE_LENGTH - 1)
    assert _split_zulip_message(content) == [content]


def test_multisegment_markdown_prefers_paragraph_boundaries() -> None:
    content = "# 结论\n\n" + "A" * 42 + "\n\n## 证据\n\n" + "B" * 42 + "\n\n## 后续\n\n" + "C" * 42
    parts = _split_zulip_message(content, limit=64)

    assert len(parts) == 3
    assert [part.split("\n", 1)[0] for part in parts] == ["（1/3）", "（2/3）", "（3/3）"]
    assert "A" * 42 in parts[0]
    assert _strip_segment_labels(parts) == content
    assert all(len(part) <= 64 for part in parts)


def test_short_heading_is_not_emitted_as_a_tiny_standalone_segment() -> None:
    content = "# 标题\n\n" + ("正文行。\n" * 30)
    parts = _split_zulip_message(content, limit=64)

    assert parts[0].split("\n", 1)[1] != "# 标题\n\n"


def test_single_overlong_paragraph_splits_at_sentence_or_safe_character_boundary() -> None:
    content = "第一句很长。第二句也很长！第三句继续保持连续。" * 8
    parts = _split_zulip_message(content, limit=48)

    assert len(parts) > 1
    assert _strip_segment_labels(parts) == content
    assert all(len(part) <= 48 for part in parts)


def test_chinese_content_is_preserved_across_segments() -> None:
    content = "中文段落一。" * 20 + "\n\n" + "中文段落二。" * 20
    parts = _split_zulip_message(content, limit=60)

    assert _strip_segment_labels(parts) == content
    assert all(part.startswith(f"（{index}/{len(parts)}）\n") for index, part in enumerate(parts, 1))


def test_segment_send_stops_at_first_failure_and_identifies_segment_number() -> None:
    adapter = ZulipAdapter(_config())
    adapter.MAX_MESSAGE_LENGTH = 64
    adapter._client = AsyncMock()
    adapter._client.post.side_effect = [
        {"result": "success", "id": 201},
        {"result": "error", "msg": "temporary failure"},
    ]
    content = "段落一\n\n" + "A" * 42 + "\n\n段落二\n\n" + "B" * 42 + "\n\n段落三\n\n" + "C" * 42

    result = asyncio.run(adapter.send(
        "Infrastructure",
        content,
        metadata={"thread_id": "Unraid"},
    ))

    assert result.success is False
    assert "segment 2/3" in (result.error or "")
    assert adapter._client.post.await_count == 2
    calls = adapter._client.post.await_args_list
    assert all(call.args[0] == "/api/v1/messages" for call in calls)
    assert all(call.kwargs["data"]["to"] == "Infrastructure" for call in calls)
    assert all(call.kwargs["data"]["topic"] == "Unraid" for call in calls)


def test_reply_routes_to_original_stream_and_topic() -> None:
    adapter = ZulipAdapter(_config())
    adapter._client = AsyncMock()
    adapter._client.post.return_value = {"result": "success", "id": 202}

    result = asyncio.run(adapter.send(
        "Infrastructure",
        "当前状态正常",
        metadata={"thread_id": "Unraid"},
    ))

    assert result.success is True
    assert result.message_id == "202"
    adapter._client.post.assert_awaited_once_with(
        "/api/v1/messages",
        data={
            "type": "stream",
            "to": "Infrastructure",
            "topic": "Unraid",
            "content": "当前状态正常",
        },
    )


def test_adapter_re_registers_after_bad_event_queue() -> None:
    adapter = ZulipAdapter(_config())
    adapter._client = AsyncMock()
    adapter._queue_id = "stale"
    adapter._last_event_id = 12
    adapter._register_queue = AsyncMock()
    adapter._client.get.return_value = {"result": "error", "code": "BAD_EVENT_QUEUE_ID"}

    asyncio.run(adapter._poll_once())

    adapter._register_queue.assert_awaited_once()


def test_event_poll_extends_only_read_timeout_from_registered_longpoll_timeout() -> None:
    adapter = ZulipAdapter(_config())
    adapter._client = AsyncMock()
    adapter._client.post.return_value = {
        "result": "success",
        "queue_id": "active",
        "last_event_id": 12,
        "event_queue_longpoll_timeout_seconds": 90,
    }
    asyncio.run(adapter._register_queue())
    adapter._client.get.return_value = {"result": "success", "events": []}

    asyncio.run(adapter._poll_once())

    timeout = adapter._client.get.await_args.kwargs["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == 95.0
    assert timeout.connect == _REQUEST_TIMEOUT_SECONDS
    assert timeout.write == _REQUEST_TIMEOUT_SECONDS
    assert timeout.pool == _REQUEST_TIMEOUT_SECONDS


def test_event_poll_uses_95_second_read_timeout_when_register_omits_longpoll_timeout() -> None:
    adapter = ZulipAdapter(_config())
    adapter._client = AsyncMock()
    adapter._client.post.return_value = {
        "result": "success",
        "queue_id": "active",
        "last_event_id": 12,
    }
    asyncio.run(adapter._register_queue())
    adapter._client.get.return_value = {"result": "success", "events": []}

    asyncio.run(adapter._poll_once())

    timeout = adapter._client.get.await_args.kwargs["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == 95.0
    assert timeout.connect == _REQUEST_TIMEOUT_SECONDS
    assert timeout.write == _REQUEST_TIMEOUT_SECONDS
    assert timeout.pool == _REQUEST_TIMEOUT_SECONDS


def test_event_poll_uses_95_second_read_timeout_when_register_returns_null_longpoll_timeout() -> None:
    adapter = ZulipAdapter(_config())
    adapter._client = AsyncMock()
    adapter._client.post.return_value = {
        "result": "success",
        "queue_id": "active",
        "last_event_id": 12,
        "event_queue_longpoll_timeout_seconds": None,
    }
    asyncio.run(adapter._register_queue())
    adapter._client.get.return_value = {"result": "success", "events": []}

    asyncio.run(adapter._poll_once())

    timeout = adapter._client.get.await_args.kwargs["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == 95.0
    assert timeout.connect == _REQUEST_TIMEOUT_SECONDS
    assert timeout.write == _REQUEST_TIMEOUT_SECONDS
    assert timeout.pool == _REQUEST_TIMEOUT_SECONDS


def test_non_event_requests_keep_the_original_scalar_timeout() -> None:
    api = object.__new__(_ZulipAPI)
    response = MagicMock()
    response.json.return_value = {"result": "success"}
    api._http = AsyncMock()
    api._http.get.return_value = response
    api._http.post.return_value = response

    asyncio.run(api.get("/api/v1/users/me"))
    asyncio.run(api.post("/api/v1/register"))
    asyncio.run(api.post("/api/v1/messages"))

    assert api._http.get.await_args.kwargs["timeout"] == _REQUEST_TIMEOUT_SECONDS
    assert [call.kwargs["timeout"] for call in api._http.post.await_args_list] == [
        _REQUEST_TIMEOUT_SECONDS,
        _REQUEST_TIMEOUT_SECONDS,
    ]