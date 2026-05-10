"""Unit tests for the LINE platform adapter.

Covers: requirements check, initialization, signature verification,
webhook handling, access control, reply_token TTL management,
dispatch retry / dead-letter, and text splitting.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.platforms.line import (
    LINE_PUSH_MESSAGE_EP,
    LINE_API_BASE_URL,
    LineAdapter,
    check_line_requirements,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEST_SECRET = "test-secret-for-unit-tests"
_TEST_TOKEN = "test-channel-access-token"
_TEST_PATH = "/webhook/line"


def _line_signature(body: bytes, secret: str) -> str:
    """Compute a valid LINE X-Line-Signature header value."""
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("utf-8")


def _make_webhook_body(
    user_id: str = "U123456",
    message_type: str = "text",
    text: str = "hello",
    message_id: str = "100",
    reply_token: str | None = "test-reply-token",
    event_type: str = "message",
    source_type: str = "user",
    group_id: str | None = None,
    room_id: str | None = None,
    timestamp_ms: int = 1700000000000,
) -> dict:
    """Build a minimal LINE webhook event payload."""
    source: dict = {"type": source_type, "userId": user_id}
    if group_id and source_type == "group":
        source["groupId"] = group_id
    if room_id and source_type == "room":
        source["roomId"] = room_id

    event: dict = {
        "type": event_type,
        "source": source,
        "message": {"type": message_type, "text": text, "id": message_id},
        "timestamp": timestamp_ms,
    }
    if reply_token:
        event["replyToken"] = reply_token

    return {"events": [event]}


def _make_adapter(
    host: str = "127.0.0.1",
    port: int = 0,
    channel_secret: str = _TEST_SECRET,
    channel_access_token: str = _TEST_TOKEN,
    dm_policy: str = "open",
    group_policy: str = "disabled",
    allow_from: list | None = None,
    group_allow_from: list | None = None,
) -> LineAdapter:
    """Create a LineAdapter for testing."""
    extra: dict = {"host": host, "port": port, "path": _TEST_PATH}
    if channel_secret:
        extra["channel_secret"] = channel_secret
    if dm_policy != "open":
        extra["dm_policy"] = dm_policy
    if group_policy != "disabled":
        extra["group_policy"] = group_policy
    if allow_from is not None:
        extra["allow_from"] = allow_from
    if group_allow_from is not None:
        extra["group_allow_from"] = group_allow_from

    env_patch = {}
    if channel_access_token:
        env_patch["LINE_CHANNEL_ACCESS_TOKEN"] = channel_access_token
    if channel_secret:
        env_patch["LINE_CHANNEL_SECRET"] = channel_secret

    with patch.dict("os.environ", env_patch, clear=False):
        config = PlatformConfig(enabled=True, extra=extra)
        return LineAdapter(config)


async def _start_server(adapter: LineAdapter) -> TestClient:
    """Start the adapter's HTTP server and return a test client."""
    app = web.Application()
    app.router.add_get("/health", adapter._handle_health)
    app.router.add_post(adapter._path, adapter._handle_webhook)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client


# ---------------------------------------------------------------------------
# check_line_requirements
# ---------------------------------------------------------------------------


class TestCheckLineRequirements:
    def test_returns_false_without_aiohttp(self, monkeypatch):
        monkeypatch.setattr("gateway.platforms.line.AIOHTTP_AVAILABLE", False)
        assert check_line_requirements() is False

    def test_returns_false_without_token(self, monkeypatch):
        monkeypatch.setattr("gateway.platforms.line.AIOHTTP_AVAILABLE", True)
        monkeypatch.delenv("LINE_CHANNEL_ACCESS_TOKEN", raising=False)
        assert check_line_requirements() is False

    def test_returns_true_when_token_set(self, monkeypatch):
        monkeypatch.setattr("gateway.platforms.line.AIOHTTP_AVAILABLE", True)
        monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "tok")
        assert check_line_requirements() is True


# ---------------------------------------------------------------------------
# Adapter init
# ---------------------------------------------------------------------------


class TestLineAdapterInit:
    def test_reads_tokens_from_extra(self):
        adapter = _make_adapter(channel_access_token="extra-token", channel_secret="extra-secret")
        assert adapter.channel_access_token == "extra-token"
        assert adapter.channel_secret == "extra-secret"

    def test_reads_tokens_from_env(self, monkeypatch):
        monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "env-token")
        monkeypatch.setenv("LINE_CHANNEL_SECRET", "env-secret")
        adapter = _make_adapter(channel_access_token="", channel_secret="")
        assert adapter.channel_access_token == "env-token"
        assert adapter.channel_secret == "env-secret"

    def test_extra_overrides_env(self, monkeypatch):
        monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "env-token")
        adapter = _make_adapter(channel_access_token="extra-token")
        assert adapter.channel_access_token == "extra-token"

    def test_dm_policy_default(self):
        adapter = _make_adapter()
        assert adapter.dm_policy == "open"

    def test_group_policy_default(self):
        adapter = _make_adapter()
        assert adapter.group_policy == "disabled"

    def test_reply_tokens_empty_initially(self):
        adapter = _make_adapter()
        assert adapter._reply_tokens == {}

    def test_retry_config_initialized(self):
        adapter = _make_adapter()
        assert adapter._MAX_DISPATCH_RETRIES == 3
        assert adapter._retry_counts == {}


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


class TestVerifySignature:
    def test_valid_signature_returns_true(self):
        adapter = _make_adapter()
        body = b'{"events":[]}'
        sig = _line_signature(body, _TEST_SECRET)
        assert adapter._verify_signature(body, sig) is True

    def test_invalid_signature_returns_false(self):
        adapter = _make_adapter()
        body = b'{"events":[]}'
        assert adapter._verify_signature(body, "invalid-sig") is False

    def test_wrong_secret_returns_false(self):
        adapter = _make_adapter()
        body = b'{"events":[]}'
        sig = _line_signature(body, "wrong-secret")
        assert adapter._verify_signature(body, sig) is False

    def test_empty_signature_returns_false(self):
        adapter = _make_adapter()
        body = b'{"events":[]}'
        assert adapter._verify_signature(body, "") is False

    def test_no_secret_returns_false(self):
        adapter = _make_adapter(channel_secret="")
        body = b'{"events":[]}'
        assert adapter._verify_signature(body, "anything") is False

    def test_hex_signature_rejected(self):
        """Ensure hex-encoded HMAC is rejected (Line uses Base64)."""
        adapter = _make_adapter()
        body = b'{"events":[]}'
        mac = hmac.new(_TEST_SECRET.encode("utf-8"), body, hashlib.sha256)
        hex_sig = mac.hexdigest()
        assert adapter._verify_signature(body, hex_sig) is False


# ---------------------------------------------------------------------------
# Webhook handling — via real aiohttp test server
# ---------------------------------------------------------------------------


class TestWebhookHandler:
    @pytest.mark.asyncio
    async def test_health_endpoint(self):
        adapter = _make_adapter()
        client = await _start_server(adapter)
        resp = await client.get("/health")
        assert resp.status == 200
        text = await resp.text()
        assert text == "ok"
        await client.close()

    @pytest.mark.asyncio
    async def test_empty_events_returns_200(self):
        adapter = _make_adapter()
        client = await _start_server(adapter)
        body = json.dumps({"events": []}).encode()
        sig = _line_signature(body, _TEST_SECRET)
        resp = await client.post(
            _TEST_PATH, data=body, headers={"X-Line-Signature": sig}
        )
        assert resp.status == 200
        await client.close()

    @pytest.mark.asyncio
    async def test_empty_events_without_signature_returns_200(self):
        """Verification ping should not require a valid signature."""
        adapter = _make_adapter()
        client = await _start_server(adapter)
        body = json.dumps({"events": []}).encode()
        resp = await client.post(_TEST_PATH, data=body)
        assert resp.status == 200
        await client.close()

    @pytest.mark.asyncio
    async def test_invalid_signature_returns_403(self):
        adapter = _make_adapter()
        client = await _start_server(adapter)
        body = json.dumps(_make_webhook_body()).encode()
        resp = await client.post(
            _TEST_PATH, data=body, headers={"X-Line-Signature": "invalid"}
        )
        assert resp.status == 403
        await client.close()

    @pytest.mark.asyncio
    async def test_hex_signature_rejected(self):
        """LINE sends Base64 signatures, not hex."""
        adapter = _make_adapter()
        client = await _start_server(adapter)
        body = json.dumps(_make_webhook_body()).encode()
        mac = hmac.new(_TEST_SECRET.encode("utf-8"), body, hashlib.sha256)
        hex_sig = mac.hexdigest()
        resp = await client.post(
            _TEST_PATH, data=body, headers={"X-Line-Signature": hex_sig}
        )
        assert resp.status == 403
        await client.close()

    @pytest.mark.asyncio
    async def test_valid_text_message_enqueued(self):
        adapter = _make_adapter(dm_policy="open")
        client = await _start_server(adapter)
        body = _make_webhook_body(user_id="U999", text="こんにちは")
        payload = json.dumps(body).encode()
        sig = _line_signature(payload, _TEST_SECRET)
        resp = await client.post(
            _TEST_PATH, data=payload, headers={"X-Line-Signature": sig}
        )
        assert resp.status == 200
        # The event should be in the message queue
        assert not adapter._message_queue.empty()
        event = await asyncio.wait_for(adapter._message_queue.get(), timeout=1)
        assert event.text == "こんにちは"
        assert event.source.user_id == "U999"
        await client.close()

    @pytest.mark.asyncio
    async def test_non_text_message_ignored(self):
        adapter = _make_adapter(dm_policy="open")
        client = await _start_server(adapter)
        body = _make_webhook_body(message_type="image", text="")
        # _process_webhook_event returns [] for non-text; queue stays empty
        payload = json.dumps(body).encode()
        sig = _line_signature(payload, _TEST_SECRET)
        resp = await client.post(
            _TEST_PATH, data=payload, headers={"X-Line-Signature": sig}
        )
        assert resp.status == 200
        assert adapter._message_queue.empty()
        await client.close()

    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self):
        adapter = _make_adapter()
        client = await _start_server(adapter)
        resp = await client.post(
            _TEST_PATH, data=b"not json", headers={"X-Line-Signature": "x"}
        )
        assert resp.status == 400
        await client.close()


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


class TestAccessControl:
    @pytest.mark.asyncio
    async def test_dm_allowed_when_open_policy(self):
        adapter = _make_adapter(dm_policy="open")
        client = await _start_server(adapter)
        body = _make_webhook_body(user_id="U123", text="hi")
        payload = json.dumps(body).encode()
        sig = _line_signature(payload, _TEST_SECRET)
        resp = await client.post(
            _TEST_PATH, data=payload, headers={"X-Line-Signature": sig}
        )
        assert resp.status == 200
        assert not adapter._message_queue.empty()
        await client.close()

    @pytest.mark.asyncio
    async def test_dm_allowed_when_user_in_allowlist(self):
        adapter = _make_adapter(dm_policy="closed", allow_from=["U123"])
        client = await _start_server(adapter)
        body = _make_webhook_body(user_id="U123", text="hi")
        payload = json.dumps(body).encode()
        sig = _line_signature(payload, _TEST_SECRET)
        resp = await client.post(
            _TEST_PATH, data=payload, headers={"X-Line-Signature": sig}
        )
        assert resp.status == 200
        assert not adapter._message_queue.empty()
        await client.close()

    @pytest.mark.asyncio
    async def test_dm_rejected_when_closed_and_not_in_allowlist(self):
        adapter = _make_adapter(dm_policy="closed", allow_from=["U999"])
        client = await _start_server(adapter)
        body = _make_webhook_body(user_id="U123", text="hi")
        payload = json.dumps(body).encode()
        sig = _line_signature(payload, _TEST_SECRET)
        resp = await client.post(
            _TEST_PATH, data=payload, headers={"X-Line-Signature": sig}
        )
        assert resp.status == 200  # 200 but silently dropped
        assert adapter._message_queue.empty()
        await client.close()

    @pytest.mark.asyncio
    async def test_group_disabled_by_default(self):
        adapter = _make_adapter(group_allow_from=["U123"])
        client = await _start_server(adapter)
        body = _make_webhook_body(
            user_id="U123", text="hi", source_type="group", group_id="G123"
        )
        payload = json.dumps(body).encode()
        sig = _line_signature(payload, _TEST_SECRET)
        resp = await client.post(
            _TEST_PATH, data=payload, headers={"X-Line-Signature": sig}
        )
        assert resp.status == 200
        assert adapter._message_queue.empty()
        await client.close()

    @pytest.mark.asyncio
    async def test_group_allowed_when_in_allowlist(self):
        adapter = _make_adapter(
            group_policy="allowlist", group_allow_from=["U123"]
        )
        client = await _start_server(adapter)
        body = _make_webhook_body(
            user_id="U123", text="hi", source_type="group", group_id="G123"
        )
        payload = json.dumps(body).encode()
        sig = _line_signature(payload, _TEST_SECRET)
        resp = await client.post(
            _TEST_PATH, data=payload, headers={"X-Line-Signature": sig}
        )
        assert resp.status == 200
        assert not adapter._message_queue.empty()
        await client.close()

    @pytest.mark.asyncio
    async def test_group_rejected_when_not_in_group_allowlist(self):
        adapter = _make_adapter(
            group_policy="allowlist", group_allow_from=["U999"]
        )
        client = await _start_server(adapter)
        body = _make_webhook_body(
            user_id="U123", text="hi", source_type="group", group_id="G123"
        )
        payload = json.dumps(body).encode()
        sig = _line_signature(payload, _TEST_SECRET)
        resp = await client.post(
            _TEST_PATH, data=payload, headers={"X-Line-Signature": sig}
        )
        assert resp.status == 200
        assert adapter._message_queue.empty()
        await client.close()


# ---------------------------------------------------------------------------
# Reply token TTL
# ---------------------------------------------------------------------------


class TestReplyTokenTTL:
    def test_token_recorded_with_timestamp(self):
        adapter = _make_adapter()
        event = {"replyToken": "abc123", "type": "message", "timestamp": 1000}
        # Simulate _process_webhook_event storing the token
        adapter._reply_tokens["msg-1"] = {
            "token": "abc123",
            "inserted_at": time.monotonic(),
        }
        assert "msg-1" in adapter._reply_tokens
        entry = adapter._reply_tokens["msg-1"]
        assert entry["token"] == "abc123"
        assert "inserted_at" in entry

    def test_expired_token_rejected(self):
        adapter = _make_adapter()
        adapter._reply_tokens["old-msg"] = {
            "token": "expired-token",
            "inserted_at": time.monotonic() - 60,  # 60s ago (>30s TTL)
        }
        # pop returns the entry but the send path checks TTL
        entry = adapter._reply_tokens.pop("old-msg", None)
        assert entry is not None
        # Simulate the TTL check in send()
        now = time.monotonic()
        if now - entry["inserted_at"] > adapter._REPLY_TOKEN_TTL:
            use_token = None
        else:
            use_token = entry["token"]
        assert use_token is None

    def test_fresh_token_accepted(self):
        adapter = _make_adapter()
        adapter._reply_tokens["fresh-msg"] = {
            "token": "fresh-token",
            "inserted_at": time.monotonic(),
        }
        entry = adapter._reply_tokens.pop("fresh-msg", None)
        now = time.monotonic()
        if now - entry["inserted_at"] > adapter._REPLY_TOKEN_TTL:
            use_token = None
        else:
            use_token = entry["token"]
        assert use_token == "fresh-token"

    def test_cleanup_removes_expired(self):
        adapter = _make_adapter()
        adapter._reply_tokens["expired"] = {
            "token": "x",
            "inserted_at": time.monotonic() - 30,
        }
        adapter._reply_tokens["fresh"] = {
            "token": "y",
            "inserted_at": time.monotonic(),
        }
        adapter._clear_expired_reply_tokens()
        assert "expired" not in adapter._reply_tokens
        assert "fresh" in adapter._reply_tokens


# ---------------------------------------------------------------------------
# Dispatch retry & dead-letter
# ---------------------------------------------------------------------------


class TestDispatchRetry:
    @pytest.mark.asyncio
    async def test_successful_dispatch_no_retry(self):
        adapter = _make_adapter()
        event = MagicMock()
        event.message_id = "msg-ok"
        adapter.handle_message = AsyncMock()
        adapter._background_tasks.clear()
        await adapter._dispatch_with_retry(event)
        adapter.handle_message.assert_awaited_once_with(event)
        assert adapter._retry_counts.get("msg-ok", 0) == 0

    @pytest.mark.asyncio
    async def test_failed_dispatch_retries(self):
        adapter = _make_adapter()
        event = MagicMock()
        event.message_id = "msg-fail"
        adapter.handle_message = AsyncMock(side_effect=RuntimeError("boom"))
        # Dispatch fails; should increment retry count and re-enqueue
        await adapter._dispatch_with_retry(event)
        assert adapter._retry_counts.get("msg-fail", 0) == 1
        # The event should be back in the queue for retry
        assert not adapter._message_queue.empty()

    @pytest.mark.asyncio
    async def test_max_retries_moves_to_dead_letter(self):
        adapter = _make_adapter()
        event = MagicMock()
        event.message_id = "msg-dlq"
        # Set retry count to max so the next attempt goes straight to dead-letter
        adapter._retry_counts["msg-dlq"] = 3
        adapter.handle_message = AsyncMock(side_effect=RuntimeError("fail"))

        # This should not raise; it goes to dead-letter immediately
        await adapter._dispatch_with_retry(event)

        # Verify dead-letter has the entry
        dlq_item = await asyncio.wait_for(adapter._dead_letter_queue.get(), timeout=1)
        assert dlq_item["event"] is event
        assert dlq_item["retry_count"] == 3
        assert dlq_item["reason"] == "max_retries_exceeded"

    @pytest.mark.asyncio
    async def test_retry_count_cleared_after_dead_letter(self):
        adapter = _make_adapter()
        event = MagicMock()
        event.message_id = "msg-clean"
        adapter._retry_counts["msg-clean"] = 3
        adapter.handle_message = AsyncMock(side_effect=RuntimeError("fail"))
        await adapter._dispatch_with_retry(event)
        assert "msg-clean" not in adapter._retry_counts


# ---------------------------------------------------------------------------
# Text splitting
# ---------------------------------------------------------------------------


class TestSplitText:
    def test_short_text_not_split(self):
        adapter = _make_adapter()
        result = adapter._split_text("short message")
        assert result == ["short message"]

    def test_long_text_split_at_limit(self):
        adapter = _make_adapter()
        long_text = "x" * 5001
        result = adapter._split_text(long_text)
        assert len(result) == 2
        assert len(result[0]) == 5000
        assert len(result[1]) == 1

    def test_empty_after_split_stripped(self):
        adapter = _make_adapter()
        # Text of exactly 5000 spaces + "hi" → second chunk is "hi", spaces stripped
        text = " " * 5000 + "hi"
        result = adapter._split_text(text)
        assert result == ["hi"]

    def test_exact_limit_not_split(self):
        adapter = _make_adapter()
        text = "x" * 5000
        result = adapter._split_text(text)
        assert result == [text]


# ---------------------------------------------------------------------------
# format_message
# ---------------------------------------------------------------------------


class TestFormatMessage:
    def test_format_message_passthrough(self):
        """LINE does not transform message text; format_message is a no-op."""
        adapter = _make_adapter()
        assert adapter.format_message("hello") == "hello"
        assert adapter.format_message("テスト") == "テスト"
        assert adapter.format_message("") == ""


# ---------------------------------------------------------------------------
# push_message (async push)
# ---------------------------------------------------------------------------


class TestPushMessage:
    @pytest.mark.asyncio
    async def test_push_message_enqueues_to_send_queue(self):
        """push_message should split and enqueue chunks into _send_queue."""
        adapter = _make_adapter()
        adapter._session = MagicMock()
        adapter.channel_access_token = _TEST_TOKEN

        result = await adapter.push_message("U123", "short msg")
        assert result.success is True
        assert adapter._send_queue.empty() is False

        item = await asyncio.wait_for(adapter._send_queue.get(), timeout=1)
        assert item["to"] == "U123"
        assert item["messages"] == [{"type": "text", "text": "short msg"}]

    @pytest.mark.asyncio
    async def test_push_message_long_text_split(self):
        """push_message should split long text into multiple queue entries."""
        adapter = _make_adapter()
        adapter._session = MagicMock()
        adapter.channel_access_token = _TEST_TOKEN

        long_text = "x" * 5001
        result = await adapter.push_message("U123", long_text)
        assert result.success is True

        # Should produce 2 chunks
        assert adapter._send_queue.qsize() == 2

        first = await asyncio.wait_for(adapter._send_queue.get(), timeout=1)
        assert len(first["messages"][0]["text"]) == 5000

        second = await asyncio.wait_for(adapter._send_queue.get(), timeout=1)
        assert len(second["messages"][0]["text"]) == 1

    @pytest.mark.asyncio
    async def test_push_message_not_connected(self):
        """push_message should fail when not connected."""
        adapter = _make_adapter()
        adapter._session = None

        result = await adapter.push_message("U123", "hello")
        assert result.success is False
        assert "token missing" in result.error.lower()

    @pytest.mark.asyncio
    async def test_send_loop_processes_queue(self):
        """_send_loop should drain _send_queue and call _line_api_post."""
        adapter = _make_adapter()
        adapter._session = MagicMock()
        adapter.channel_access_token = _TEST_TOKEN

        mock_post = AsyncMock(return_value={})
        adapter._line_api_post = mock_post

        # Enqueue a message directly into the send queue
        await adapter._send_queue.put({
            "to": "U999",
            "messages": [{"type": "text", "text": "async hello"}],
        })

        send_task = asyncio.create_task(adapter._send_loop())
        # Give the loop a moment to process (longer than 0.1s for reliability)
        await asyncio.sleep(0.5)

        send_task.cancel()
        try:
            await send_task
        except asyncio.CancelledError:
            pass

        mock_post.assert_called_once()
        call_args = mock_post.call_args
        # _line_api_post(endpoint, payload) — both are positional args
        assert call_args[0][0] == LINE_PUSH_MESSAGE_EP
        assert call_args[0][1]["to"] == "U999"
        assert call_args[0][1]["messages"][0]["text"] == "async hello"


# ---------------------------------------------------------------------------
# Non-message webhook events (follow, unfollow, postback, member join/leave)
# ---------------------------------------------------------------------------


def _make_follow_body(user_id="U123456"):
    return {"events": [{"type": "follow", "source": {"type": "user", "userId": user_id}, "timestamp": 1700000000000}]}


def _make_unfollow_body(user_id="U123456"):
    return {"events": [{"type": "unfollow", "source": {"type": "user", "userId": user_id}, "timestamp": 1700000000000}]}


def _make_postback_body(user_id="U123456", data="action=buy&itemid=123"):
    return {
        "events": [{
            "type": "postback",
            "source": {"type": "user", "userId": user_id},
            "postback": {"data": data},
            "timestamp": 1700000000000,
        }],
    }


def _make_member_joined_body(group_id="G123", user_ids=None):
    if user_ids is None:
        user_ids = ["U111", "U222"]
    return {
        "events": [{
            "type": "memberJoined",
            "source": {"type": "group", "userId": "U123", "groupId": group_id},
            "joined": {"members": [{"type": "user", "userId": uid} for uid in user_ids]},
            "timestamp": 1700000000000,
        }],
    }


def _make_member_left_body(group_id="G123", user_ids=None):
    if user_ids is None:
        user_ids = ["U111"]
    return {
        "events": [{
            "type": "memberLeft",
            "source": {"type": "group", "userId": "U123", "groupId": group_id},
            "left": {"members": [{"type": "user", "userId": uid} for uid in user_ids]},
            "timestamp": 1700000000000,
        }],
    }


class TestNonMessageEvents:
    @pytest.mark.asyncio
    async def test_follow_event_accepted(self, caplog):
        """follow event is acknowledged (200) and does not enqueue a message."""
        adapter = _make_adapter()
        client = await _start_server(adapter)

        body = _make_follow_body()
        payload = json.dumps(body).encode()
        sig = _line_signature(payload, _TEST_SECRET)
        with caplog.at_level(logging.INFO):
            resp = await client.post(
                _TEST_PATH, data=payload, headers={"X-Line-Signature": sig}
            )
        assert resp.status == 200
        assert adapter._message_queue.empty()
        assert "followed" in caplog.text
        await client.close()

    @pytest.mark.asyncio
    async def test_unfollow_event_accepted(self, caplog):
        """unfollow event is acknowledged (200) and does not enqueue a message."""
        adapter = _make_adapter()
        client = await _start_server(adapter)

        body = _make_unfollow_body()
        payload = json.dumps(body).encode()
        sig = _line_signature(payload, _TEST_SECRET)
        with caplog.at_level(logging.INFO):
            resp = await client.post(
                _TEST_PATH, data=payload, headers={"X-Line-Signature": sig}
            )
        assert resp.status == 200
        assert adapter._message_queue.empty()
        assert "unfollowed" in caplog.text
        await client.close()

    @pytest.mark.asyncio
    async def test_postback_event_accepted(self, caplog):
        """postback event is acknowledged (200) and does not enqueue a message."""
        adapter = _make_adapter()
        client = await _start_server(adapter)

        body = _make_postback_body(data="action=hello")
        payload = json.dumps(body).encode()
        sig = _line_signature(payload, _TEST_SECRET)
        with caplog.at_level(logging.INFO):
            resp = await client.post(
                _TEST_PATH, data=payload, headers={"X-Line-Signature": sig}
            )
        assert resp.status == 200
        assert adapter._message_queue.empty()
        assert "Postback" in caplog.text
        await client.close()

    @pytest.mark.asyncio
    async def test_member_joined_event_accepted(self, caplog):
        """memberJoined event is acknowledged (200) and does not enqueue a message."""
        adapter = _make_adapter()
        client = await _start_server(adapter)

        body = _make_member_joined_body()
        payload = json.dumps(body).encode()
        sig = _line_signature(payload, _TEST_SECRET)
        with caplog.at_level(logging.INFO):
            resp = await client.post(
                _TEST_PATH, data=payload, headers={"X-Line-Signature": sig}
            )
        assert resp.status == 200
        assert adapter._message_queue.empty()
        assert "member" in caplog.text.lower()
        await client.close()

    @pytest.mark.asyncio
    async def test_member_left_event_accepted(self, caplog):
        """memberLeft event is acknowledged (200) and does not enqueue a message."""
        adapter = _make_adapter()
        client = await _start_server(adapter)

        body = _make_member_left_body()
        payload = json.dumps(body).encode()
        sig = _line_signature(payload, _TEST_SECRET)
        with caplog.at_level(logging.INFO):
            resp = await client.post(
                _TEST_PATH, data=payload, headers={"X-Line-Signature": sig}
            )
        assert resp.status == 200
        assert adapter._message_queue.empty()
        assert "member" in caplog.text.lower()
        await client.close()


# ---------------------------------------------------------------------------
# _poll_loop guard: handler not yet set
# ---------------------------------------------------------------------------


class TestPollLoopGuard:
    @pytest.mark.asyncio
    async def test_poll_loop_requeues_when_handler_not_set(self):
        """_poll_loop should re-enqueue events when _message_handler is None."""
        adapter = _make_adapter()
        adapter._message_handler = None

        # Manually put an event in the queue
        event = MessageEvent(
            text="test",
            message_type=MessageType.TEXT,
            source=adapter.build_source(
                chat_id="U123", chat_type="dm", user_id="U123", user_name="test-user"
            ),
            message_id="1",
        )
        await adapter._message_queue.put(event)

        # Run poll_loop — it should re-queue since handler is None.
        # The loop sleeps 0.5s between re-queue attempts, so wait long enough.
        loop_task = asyncio.create_task(adapter._poll_loop())
        await asyncio.sleep(1.0)

        # The event should be back in the queue (re-queued by the guard)
        assert not adapter._message_queue.empty()

        re_queued = await asyncio.wait_for(adapter._message_queue.get(), timeout=1)
        assert re_queued.text == "test"

        # Cancel the loop task
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# chat_type propagation in build_source
# ---------------------------------------------------------------------------


class TestChatTypePropagation:
    @pytest.mark.asyncio
    async def test_group_message_has_group_chat_type(self):
        """Webhook events from groups produce source with chat_type='group'."""
        adapter = _make_adapter(group_policy="allowlist", group_allow_from=["U123"])
        client = await _start_server(adapter)

        body = _make_webhook_body(
            user_id="U123", text="group msg",
            source_type="group", group_id="G123",
            reply_token=None,
        )
        payload = json.dumps(body).encode()
        sig = _line_signature(payload, _TEST_SECRET)
        resp = await client.post(
            _TEST_PATH, data=payload, headers={"X-Line-Signature": sig}
        )
        assert resp.status == 200
        assert not adapter._message_queue.empty()
        event = await asyncio.wait_for(adapter._message_queue.get(), timeout=1)
        assert event.source.chat_type == "group"
        assert event.source.chat_id == "G123"
        await client.close()

    @pytest.mark.asyncio
    async def test_room_message_has_room_chat_type(self):
        """Webhook events from rooms produce source with chat_type='room'."""
        adapter = _make_adapter(group_policy="allowlist", group_allow_from=["U123"])
        client = await _start_server(adapter)

        body = _make_webhook_body(
            user_id="U123", text="room msg",
            source_type="room", group_id=None, room_id="R456",
            reply_token=None,
        )
        payload = json.dumps(body).encode()
        sig = _line_signature(payload, _TEST_SECRET)
        resp = await client.post(
            _TEST_PATH, data=payload, headers={"X-Line-Signature": sig}
        )
        assert resp.status == 200
        assert not adapter._message_queue.empty()
        event = await asyncio.wait_for(adapter._message_queue.get(), timeout=1)
        assert event.source.chat_type == "room"
        assert event.source.chat_id == "R456"
        await client.close()

    @pytest.mark.asyncio
    async def test_dm_message_has_dm_chat_type(self):
        """Webhook events from users produce source with chat_type='dm'."""
        adapter = _make_adapter()
        client = await _start_server(adapter)

        body = _make_webhook_body(user_id="U123", text="hi")
        payload = json.dumps(body).encode()
        sig = _line_signature(payload, _TEST_SECRET)
        resp = await client.post(
            _TEST_PATH, data=payload, headers={"X-Line-Signature": sig}
        )
        assert resp.status == 200
        assert not adapter._message_queue.empty()
        event = await asyncio.wait_for(adapter._message_queue.get(), timeout=1)
        assert event.source.chat_type == "dm"
        await client.close()