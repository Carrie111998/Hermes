"""Tests for SlackAdapter.send_or_update_status (issue #30045, Slack).

The status-update path must:
  1. Send a fresh message on the first call for a (channel, thread, key).
  2. Edit that same message on subsequent calls with the same key.
  3. Fall back to sending fresh when the cached message edit fails.
  4. Keep distinct keys and distinct threads independent.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest


def _ensure_slack_mock():
    if "slack_bolt" in sys.modules and hasattr(sys.modules["slack_bolt"], "__file__"):
        return
    slack_bolt = MagicMock()
    slack_bolt.async_app.AsyncApp = MagicMock
    slack_bolt.adapter.socket_mode.async_handler.AsyncSocketModeHandler = MagicMock
    slack_sdk = MagicMock()
    slack_sdk.web.async_client.AsyncWebClient = MagicMock
    for name, mod in [
        ("slack_bolt", slack_bolt),
        ("slack_bolt.async_app", slack_bolt.async_app),
        ("slack_bolt.adapter", slack_bolt.adapter),
        ("slack_bolt.adapter.socket_mode", slack_bolt.adapter.socket_mode),
        (
            "slack_bolt.adapter.socket_mode.async_handler",
            slack_bolt.adapter.socket_mode.async_handler,
        ),
        ("slack_sdk", slack_sdk),
        ("slack_sdk.web", slack_sdk.web),
        ("slack_sdk.web.async_client", slack_sdk.web.async_client),
    ]:
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("aiohttp", MagicMock())


_ensure_slack_mock()

import plugins.platforms.slack.adapter as _slack_mod  # noqa: E402

_slack_mod.SLACK_AVAILABLE = True

from gateway.config import PlatformConfig  # noqa: E402
from gateway.run import _send_or_update_status_coro  # noqa: E402
from plugins.platforms.slack.adapter import SlackAdapter  # noqa: E402


@pytest.fixture()
def adapter():
    config = PlatformConfig(enabled=True, token="***")
    a = SlackAdapter(config)
    a._app = MagicMock()
    client = AsyncMock()
    client.chat_postMessage = AsyncMock(
        side_effect=lambda **kw: {"ok": True, "ts": f"ts_{client.chat_postMessage.call_count}"}
    )
    client.chat_update = AsyncMock(return_value={"ok": True})
    a._get_client = MagicMock(return_value=client)
    a._bot_user_id = "U_BOT"
    a._running = True
    a.stop_typing = AsyncMock()
    return a


METADATA = {"thread_id": "1784585355.415219"}


@pytest.mark.asyncio
async def test_first_call_sends_fresh(adapter):
    result = await adapter.send_or_update_status(
        "C_CHAN", "context_pressure", "compressing 1/3", metadata=METADATA
    )
    assert result.success
    client = adapter._get_client.return_value
    assert client.chat_postMessage.call_count == 1
    assert client.chat_update.call_count == 0


@pytest.mark.asyncio
async def test_second_call_edits_same_message(adapter):
    r1 = await adapter.send_or_update_status(
        "C_CHAN", "context_pressure", "compressing 1/3", metadata=METADATA
    )
    r2 = await adapter.send_or_update_status(
        "C_CHAN", "context_pressure", "compressing 2/3", metadata=METADATA
    )
    assert r1.success and r2.success
    client = adapter._get_client.return_value
    assert client.chat_postMessage.call_count == 1
    assert client.chat_update.call_count == 1
    # The edit must target the ts of the first send.
    assert client.chat_update.call_args.kwargs["ts"] == r1.message_id


@pytest.mark.asyncio
async def test_edit_failure_falls_back_to_fresh_send(adapter):
    await adapter.send_or_update_status(
        "C_CHAN", "context_pressure", "compressing 1/3", metadata=METADATA
    )
    client = adapter._get_client.return_value
    client.chat_update = AsyncMock(side_effect=RuntimeError("message_not_found"))
    r2 = await adapter.send_or_update_status(
        "C_CHAN", "context_pressure", "compressing 2/3", metadata=METADATA
    )
    assert r2.success
    assert client.chat_postMessage.call_count == 2
    # Cached id was replaced: a third call edits the NEW message.
    client.chat_update = AsyncMock(return_value={"ok": True})
    r3 = await adapter.send_or_update_status(
        "C_CHAN", "context_pressure", "compressing 3/3", metadata=METADATA
    )
    assert r3.success
    assert client.chat_update.call_args.kwargs["ts"] == r2.message_id


@pytest.mark.asyncio
async def test_distinct_keys_do_not_crosstalk(adapter):
    await adapter.send_or_update_status(
        "C_CHAN", "context_pressure", "compressing", metadata=METADATA
    )
    await adapter.send_or_update_status(
        "C_CHAN", "model_fallback", "falling back", metadata=METADATA
    )
    client = adapter._get_client.return_value
    assert client.chat_postMessage.call_count == 2
    assert client.chat_update.call_count == 0


@pytest.mark.asyncio
async def test_distinct_threads_do_not_crosstalk(adapter):
    await adapter.send_or_update_status(
        "C_CHAN", "context_pressure", "compressing", metadata={"thread_id": "111.1"}
    )
    await adapter.send_or_update_status(
        "C_CHAN", "context_pressure", "compressing", metadata={"thread_id": "222.2"}
    )
    client = adapter._get_client.return_value
    assert client.chat_postMessage.call_count == 2
    assert client.chat_update.call_count == 0


@pytest.mark.asyncio
async def test_concurrent_updates_post_one_status_bubble(adapter):
    """A retry burst must not race empty-cache reads into duplicate posts."""
    client = adapter._get_client.return_value

    async def delayed_post(**kwargs):
        await asyncio.sleep(0.01)
        return {"ok": True, "ts": "status_ts"}

    client.chat_postMessage = AsyncMock(side_effect=delayed_post)

    results = await asyncio.gather(
        *(
            _send_or_update_status_coro(
                adapter,
                "C_CHAN",
                "provider_retry",
                f"retry {attempt}",
                METADATA,
            )
            for attempt in range(8)
        )
    )

    assert all(result.success for result in results)
    assert client.chat_postMessage.await_count == 1
    assert client.chat_update.await_count == 7


@pytest.mark.asyncio
async def test_same_ids_in_two_workspaces_do_not_share_status(adapter):
    """Slack-local channel/thread IDs need the workspace in the cache key."""
    one = AsyncMock()
    one.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "same_ts"})
    one.chat_update = AsyncMock(return_value={"ok": True})
    two = AsyncMock()
    two.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "same_ts"})
    two.chat_update = AsyncMock(return_value={"ok": True})
    adapter._get_client = MagicMock(
        side_effect=lambda _chat_id, team_id=None: {"T_ONE": one, "T_TWO": two}[
            team_id
        ]
    )

    await adapter.send_or_update_status(
        "C_SHARED",
        "provider_retry",
        "workspace one",
        metadata={"thread_id": "same_thread", "slack_team_id": "T_ONE"},
    )
    await adapter.send_or_update_status(
        "C_SHARED",
        "provider_retry",
        "workspace two",
        metadata={"thread_id": "same_thread", "slack_team_id": "T_TWO"},
    )

    one.chat_postMessage.assert_awaited_once()
    two.chat_postMessage.assert_awaited_once()
    one.chat_update.assert_not_awaited()
    two.chat_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_transient_edit_failure_keeps_existing_status_id(adapter):
    """An ambiguous chat.update timeout must not fall through to a new post."""
    first = await adapter.send_or_update_status(
        "C_CHAN", "provider_retry", "retry 1", metadata=METADATA
    )
    client = adapter._get_client.return_value
    client.chat_update = AsyncMock(side_effect=TimeoutError("transport stalled"))

    failed = await adapter.send_or_update_status(
        "C_CHAN", "provider_retry", "retry 2", metadata=METADATA
    )

    assert failed.success is False
    assert failed.retryable is True
    assert client.chat_postMessage.await_count == 1
    assert adapter._status_message_ids[
        ("", "C_CHAN", METADATA["thread_id"], "provider_retry")
    ] == first.message_id


@pytest.mark.asyncio
async def test_status_send_does_not_clear_live_assistant_status(adapter):
    """A progress bubble is not a final response and must keep typing alive."""
    original_metadata = dict(METADATA)

    await adapter.send_or_update_status(
        "C_CHAN", "provider_retry", "retrying", metadata=original_metadata
    )

    adapter.stop_typing.assert_not_awaited()
    assert original_metadata == METADATA


@pytest.mark.asyncio
async def test_status_does_not_consume_private_slash_reply_context(adapter):
    """Only the final slash-command reply may claim its response_url."""
    adapter._pop_slash_context = MagicMock(
        return_value={"response_url": "https://local.invalid/response"}
    )

    result = await adapter.send_or_update_status(
        "C_CHAN", "provider_retry", "retrying", metadata=METADATA
    )

    assert result.success is True
    adapter._pop_slash_context.assert_not_called()
    adapter._get_client.return_value.chat_postMessage.assert_awaited_once()
