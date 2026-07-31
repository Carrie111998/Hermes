"""Integration tests: SlackAdapter wiring of Block Kit into send paths.

Verifies the behaviour contract:
  * rich_blocks unset (default ON) => ``blocks`` present AND ``text`` fallback set
  * rich_blocks: false             => no ``blocks`` kwarg, plain ``text`` only
  * edit_message: blocks only on finalize (streaming edits stay plain)
  * multi-chunk (>39k) messages fall back to plain text
"""

from unittest.mock import AsyncMock, MagicMock, call

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.slack import adapter as slack_module
from plugins.platforms.slack.adapter import SlackAdapter


def _make_adapter(extra=None):
    config = PlatformConfig(enabled=True, token="xoxb-fake", extra=extra or {})
    a = SlackAdapter(config)
    a._app = MagicMock()
    client = AsyncMock()
    client.chat_postMessage = AsyncMock(return_value={"ts": "111.222"})
    client.chat_update = AsyncMock(return_value={"ts": "111.222"})
    a._get_client = MagicMock(return_value=client)
    a.stop_typing = AsyncMock()
    a._running = True
    return a, client


RICH_MD = "# Title\n\n- a\n  - nested\n\n---\n\nbody text"
RICH_TABLE_MD = (
    "| Item | Status | Note |\n"
    "|---|---:|---|\n"
    "| Hermes | ok | table |"
)


class SlackRejectedBlocks(Exception):
    def __init__(self, error="invalid_blocks"):
        super().__init__(f"Slack API rejected blocks: {error}")
        self.response = {"error": error}


def _slack_connection_key():
    from aiohttp.client_reqrep import ConnectionKey

    return ConnectionKey(
        host="slack.com",
        port=443,
        is_ssl=True,
        ssl=True,
        proxy=None,
        proxy_auth=None,
        proxy_headers_hash=None,
    )


class TestSendMessageBlocks:
    @pytest.mark.asyncio
    async def test_enabled_by_default_emits_blocks(self):
        # rich_blocks unset -> default ON: blocks present, text fallback kept
        adapter, client = _make_adapter()
        await adapter.send("C1", RICH_MD)
        kwargs = client.chat_postMessage.await_args.kwargs
        assert "blocks" in kwargs and kwargs["blocks"]
        assert kwargs["text"]  # plain text fallback still sent

    @pytest.mark.asyncio
    async def test_explicit_opt_out_no_blocks(self):
        # rich_blocks: false -> revert to flat mrkdwn text, no blocks
        adapter, client = _make_adapter({"rich_blocks": False})
        await adapter.send("C1", RICH_MD)
        kwargs = client.chat_postMessage.await_args.kwargs
        assert "blocks" not in kwargs
        assert kwargs["text"]  # plain text still sent


    @pytest.mark.asyncio
    async def test_over_cap_render_batches_instead_of_falling_back_to_text(self):
        # 60 dividers -> 60 blocks (> 50). This used to collapse to plain text
        # (renderer returned None over the cap). It now batches loss-free: the
        # blocks are split across multiple posts instead of being dropped.
        adapter, client = _make_adapter({"rich_blocks": True})
        client.chat_postMessage = AsyncMock(
            side_effect=[{"ts": "111.222"}, {"ts": "111.999"}]
        )
        await adapter.send("C1", "\n\n".join(["---"] * 60))
        calls = client.chat_postMessage.await_args_list
        assert len(calls) == 2
        assert all(c.kwargs.get("blocks") for c in calls)
        assert all(c.kwargs["text"] for c in calls)


    @pytest.mark.asyncio
    async def test_feedback_buttons_opt_in_appended_to_blocks(self):
        adapter, client = _make_adapter({"rich_blocks": True, "feedback_buttons": True})

        await adapter.send("C1", "final answer")

        blocks = client.chat_postMessage.await_args.kwargs["blocks"]
        feedback = blocks[-1]
        assert feedback["type"] == "context_actions"
        assert feedback["elements"][0]["type"] == "feedback_buttons"
        assert feedback["elements"][0]["action_id"] == "hermes_feedback"


class TestEditMessageBlocks:
    @pytest.mark.asyncio
    async def test_intermediate_edit_no_blocks(self):
        adapter, client = _make_adapter({"rich_blocks": True})
        await adapter.edit_message("C1", "111.222", RICH_MD, finalize=False)
        kwargs = client.chat_update.await_args.kwargs
        assert "blocks" not in kwargs
        assert kwargs["text"]

    @pytest.mark.asyncio
    async def test_finalize_edit_gets_blocks(self):
        adapter, client = _make_adapter({"rich_blocks": True})
        await adapter.edit_message("C1", "111.222", RICH_MD, finalize=True)
        kwargs = client.chat_update.await_args.kwargs
        assert "blocks" in kwargs and kwargs["blocks"]
        assert kwargs["text"]


    @pytest.mark.asyncio
    async def test_block_rejection_retries_edit_without_blocks_using_workspace_client(self):
        adapter, client = _make_adapter({"rich_blocks": True})
        client.chat_update = AsyncMock(
            side_effect=[SlackRejectedBlocks("invalid_blocks"), {"ts": "111.222"}]
        )

        result = await adapter.edit_message(
            "C1",
            "111.222",
            RICH_TABLE_MD,
            finalize=True,
            metadata={"team_id": "T_SECONDARY"},
        )

        assert result.success is True
        assert adapter._get_client.call_args_list == [
            call("C1", team_id="T_SECONDARY"),
            call("C1", team_id="T_SECONDARY"),
        ]
        assert client.chat_update.await_count == 2
        first = client.chat_update.await_args_list[0].kwargs
        second = client.chat_update.await_args_list[1].kwargs
        assert "blocks" in first and first["blocks"]
        assert second["blocks"] == []
        assert second["text"]

    @pytest.mark.asyncio
    async def test_timeout_error_on_edit_is_retryable_transient(self):
        adapter, client = _make_adapter()
        client.chat_update = AsyncMock(side_effect=TimeoutError("timed out"))

        result = await adapter.edit_message("C1", "111.222", RICH_MD, finalize=True)

        assert result.success is False
        assert result.retryable is True
        assert result.error_kind == "transient"


# ---------------------------------------------------------------------------
# markdown_blocks mode — Slack's native ``markdown`` Block Kit block (#8552)
# ---------------------------------------------------------------------------


class TestMarkdownBlockMode:
    """Opt-in ``markdown_blocks`` renders raw standard markdown via Slack's
    native ``markdown`` block, keeping the mrkdwn ``text`` fallback."""

    @pytest.mark.asyncio
    async def test_disabled_by_default(self):
        # Isolate markdown_blocks: turn rich_blocks off so any block that
        # appears would have to come from markdown_blocks mode.
        adapter, client = _make_adapter({"rich_blocks": False})
        await adapter.send("C1", RICH_TABLE_MD)
        kwargs = client.chat_postMessage.await_args.kwargs
        assert "blocks" not in kwargs

    @pytest.mark.asyncio
    async def test_enabled_sends_markdown_block_with_raw_content(self):
        adapter, client = _make_adapter({"markdown_blocks": True})
        await adapter.send("C1", RICH_TABLE_MD)
        kwargs = client.chat_postMessage.await_args.kwargs
        blocks = kwargs["blocks"]
        assert blocks[0]["type"] == "markdown"
        # RAW standard markdown, not mrkdwn-converted — Slack translates it
        assert blocks[0]["text"] == RICH_TABLE_MD
        # mrkdwn fallback text is still present for notifications/search
        assert kwargs["text"]


    @pytest.mark.asyncio
    async def test_edit_finalize_uses_markdown_block(self):
        adapter, client = _make_adapter({"markdown_blocks": True})
        await adapter.edit_message("C1", "111.222", RICH_TABLE_MD, finalize=True)
        kwargs = client.chat_update.await_args.kwargs
        assert kwargs["blocks"][0]["type"] == "markdown"
        assert kwargs["blocks"][0]["text"] == RICH_TABLE_MD


# ---------------------------------------------------------------------------
# Loss-free batching — renders over Slack's 50-block ceiling (#batch-over-50)
# ---------------------------------------------------------------------------


# 60 dividers render to 60 ``divider`` blocks (> MAX_BLOCKS=50). Before the
# batching fix this collapsed to plain text (rich_blocks) or silently truncated
# at out[:50] (sanitize_blocks). Now it must post across multiple messages.
OVER_CAP_MD = "\n\n".join(["---"] * 60)


class TestSendBlockBatchingOver50:
    @pytest.mark.asyncio
    async def test_over_cap_render_posts_multiple_messages_no_block_loss(self):
        adapter, client = _make_adapter({"rich_blocks": True})
        client.chat_postMessage = AsyncMock(
            side_effect=[{"ts": "111.222"}, {"ts": "111.999"}]
        )

        result = await adapter.send("C1", OVER_CAP_MD)

        assert result.success is True
        # 60 blocks -> 50 + 10 -> two chat.postMessage calls
        calls = client.chat_postMessage.await_args_list
        assert len(calls) == 2

        first, second = calls[0].kwargs, calls[1].kwargs
        # every batch present, none over the 50-block cap
        assert len(first["blocks"]) == 50
        assert len(second["blocks"]) == 10
        assert all(len(c.kwargs["blocks"]) <= 50 for c in calls)
        # no block loss: 50 + 10 == 60 rendered blocks
        total = sum(len(c.kwargs["blocks"]) for c in calls)
        assert total == 60
        # every message carries a text fallback
        assert first["text"]
        assert second["text"]

    @pytest.mark.asyncio
    async def test_continuation_batches_thread_under_first_message(self):
        adapter, client = _make_adapter({"rich_blocks": True})
        client.chat_postMessage = AsyncMock(
            side_effect=[{"ts": "111.222"}, {"ts": "111.999"}]
        )

        # Not itself a threaded reply -> continuation threads under msg #1 ts.
        await adapter.send("C1", OVER_CAP_MD)

        calls = client.chat_postMessage.await_args_list
        first, second = calls[0].kwargs, calls[1].kwargs
        # first message is a top-level post (no thread_ts), no broadcast
        assert "thread_ts" not in first
        assert "reply_broadcast" not in first
        # continuation is threaded under the first message and never broadcasts
        assert second["thread_ts"] == "111.222"
        assert "reply_broadcast" not in second

    @pytest.mark.asyncio
    async def test_over_cap_reply_keeps_thread_and_broadcasts_only_first(self):
        adapter, client = _make_adapter(
            {"rich_blocks": True, "reply_broadcast": True}
        )
        client.chat_postMessage = AsyncMock(
            side_effect=[{"ts": "111.222"}, {"ts": "111.999"}]
        )

        # reply_to makes this a threaded reply; broadcast only on the first msg.
        await adapter.send("C1", OVER_CAP_MD, reply_to="900.000")

        calls = client.chat_postMessage.await_args_list
        first, second = calls[0].kwargs, calls[1].kwargs
        assert first["thread_ts"] == "900.000"
        assert first.get("reply_broadcast") is True
        # all batches stay in the same thread; continuation never broadcasts
        assert second["thread_ts"] == "900.000"
        assert "reply_broadcast" not in second

    @pytest.mark.asyncio
    async def test_feedback_block_appears_once_on_last_batch(self):
        adapter, client = _make_adapter(
            {"rich_blocks": True, "feedback_buttons": True}
        )
        client.chat_postMessage = AsyncMock(
            side_effect=[{"ts": "111.222"}, {"ts": "111.999"}]
        )

        await adapter.send("C1", OVER_CAP_MD)

        calls = client.chat_postMessage.await_args_list
        all_blocks = [blk for c in calls for blk in c.kwargs["blocks"]]
        feedback = [
            b for b in all_blocks if b.get("type") == "context_actions"
        ]
        # feedback controls appear exactly once, across all batches
        assert len(feedback) == 1
        # ...and specifically on the LAST batch
        assert calls[-1].kwargs["blocks"][-1]["type"] == "context_actions"

    @pytest.mark.asyncio
    async def test_block_rejection_retry_is_per_batch(self):
        adapter, client = _make_adapter({"rich_blocks": True})
        # First batch rejected -> resend without blocks; second batch fine.
        client.chat_postMessage = AsyncMock(
            side_effect=[
                SlackRejectedBlocks("invalid_blocks"),
                {"ts": "111.222"},
                {"ts": "111.999"},
            ]
        )

        result = await adapter.send("C1", OVER_CAP_MD)

        assert result.success is True
        # 3 calls: batch1 (reject) -> batch1 retry (no blocks) -> batch2
        calls = client.chat_postMessage.await_args_list
        assert len(calls) == 3
        # the retry dropped blocks but kept the text fallback
        assert "blocks" not in calls[1].kwargs
        assert calls[1].kwargs["text"]
        # the second batch still carries its blocks
        assert calls[2].kwargs["blocks"]

    @pytest.mark.asyncio
    async def test_within_cap_still_single_post_with_blocks(self):
        # Regression: the common single-batch case behaves exactly as before.
        adapter, client = _make_adapter({"rich_blocks": True})
        await adapter.send("C1", RICH_MD)
        assert client.chat_postMessage.await_count == 1
        kwargs = client.chat_postMessage.await_args.kwargs
        assert kwargs["blocks"]
        assert kwargs["text"]



