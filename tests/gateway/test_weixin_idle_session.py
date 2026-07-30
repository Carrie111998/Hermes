"""Tests for WeChat iLink idle send-session health check (issue #74852).

The bug: after long idle periods (hours), intermediate network devices
(NAT, firewalls, Cloudflare Warp) silently drop TCP connections.
The aiohttp connection pool is unaware, so POST requests appear to succeed
(HTTP 200) while iLink silently discards messages.

The fix: track the last send time and recreate the aiohttp session
when it exceeds the idle threshold (SEND_SESSION_MAX_IDLE_SECONDS).
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def weixin_adapter():
    """Create a minimal WeixinAdapter with mocked internals for session tests."""
    from gateway.platforms.weixin import WeixinAdapter, SEND_SESSION_MAX_IDLE_SECONDS

    config = MagicMock()
    config.extra = {"account_id": "test-account"}
    config.name = "weixin"
    config.token = "test-token"

    with patch.object(WeixinAdapter, "__init__", lambda self, cfg: None):
        adapter = WeixinAdapter.__new__(WeixinAdapter)
        adapter._send_session = AsyncMock()
        adapter._send_session.closed = False
        adapter._token = "test-token"
        adapter._base_url = "https://ilinkai.weixin.qq.com"
        adapter._account_id = "test-account"
        adapter._typing_cache = MagicMock()
        adapter._token_store = MagicMock()
        adapter._token_store.get.return_value = None
        adapter.config = config
        # Mock platform so that adapter.name (which reads platform.value.title()) works
        platform_mock = MagicMock()
        platform_value_mock = MagicMock()
        platform_value_mock.title.return_value = "Weixin"
        platform_mock.value = platform_value_mock
        adapter.platform = platform_mock
        adapter._last_send_time = time.monotonic()
        adapter._send_chunk_delay_seconds = 0
        adapter._send_text_gate = asyncio.Lock()
        adapter._split_multiline_messages = False
        adapter.MAX_MESSAGE_LENGTH = 2000
        adapter._send_chunk_retries = 4
        adapter._send_chunk_retry_delay_seconds = 1.0
        adapter._rate_limit_circuit_threshold = 1
        adapter._rate_limit_circuit_window_seconds = 30.0
        adapter._rate_limit_circuit_open_seconds = 30.0
        adapter._rate_limit_circuit_until = 0.0
        adapter._rate_limit_events = []

    return adapter


class TestEnsureSendSessionAlive:
    """Tests for _ensure_send_session_alive — the fix for silent message drops."""

    @pytest.mark.asyncio
    async def test_no_recreate_when_session_fresh(self, weixin_adapter):
        """If the session was recently used, do NOT recreate it."""
        weixin_adapter._last_send_time = time.monotonic()  # just now
        old_session = weixin_adapter._send_session

        await weixin_adapter._ensure_send_session_alive()

        assert weixin_adapter._send_session is old_session
        old_session.close.assert_not_called()

    @pytest.mark.asyncio
    async def test_recreate_when_session_idle_too_long(self, weixin_adapter):
        """When idle beyond the threshold, recreate the session."""
        weixin_adapter._last_send_time = time.monotonic() - 200  # 200s ago (> 120s threshold)
        old_session = weixin_adapter._send_session

        with patch("gateway.platforms.weixin._make_ssl_connector") as mock_connector:
            mock_connector.return_value = MagicMock()
            with patch("aiohttp.ClientSession") as mock_session_cls:
                new_session = AsyncMock()
                new_session.closed = False
                mock_session_cls.return_value = new_session

                await weixin_adapter._ensure_send_session_alive()

                old_session.close.assert_called_once()
                mock_session_cls.assert_called_once()
                assert weixin_adapter._send_session is new_session

    @pytest.mark.asyncio
    async def test_no_recreate_when_session_already_closed(self, weixin_adapter):
        """If the session is already closed, do nothing (connect() recreates)."""
        weixin_adapter._last_send_time = time.monotonic() - 200
        weixin_adapter._send_session.closed = True
        old_session = weixin_adapter._send_session

        await weixin_adapter._ensure_send_session_alive()

        # Should not attempt to recreate when already closed
        assert weixin_adapter._send_session is old_session

    @pytest.mark.asyncio
    async def test_no_recreate_when_session_is_none(self, weixin_adapter):
        """If there is no session, do nothing."""
        weixin_adapter._send_session = None
        weixin_adapter._last_send_time = time.monotonic() - 200

        await weixin_adapter._ensure_send_session_alive()

        assert weixin_adapter._send_session is None

    @pytest.mark.asyncio
    async def test_send_calls_ensure_session_alive(self, weixin_adapter):
        """The send() method should call _ensure_send_session_alive before delivering."""
        weixin_adapter._ensure_send_session_alive = AsyncMock()

        with patch.object(weixin_adapter, "_split_text", return_value=["hello"]), \
             patch.object(weixin_adapter, "format_message", return_value="hello"), \
             patch.object(weixin_adapter, "extract_media", return_value=([], "hello")), \
             patch.object(weixin_adapter, "filter_media_delivery_paths", return_value=[]), \
             patch.object(weixin_adapter, "extract_images", return_value=([], "hello")), \
             patch.object(weixin_adapter, "extract_local_files", return_value=([], "hello")), \
             patch.object(weixin_adapter, "filter_local_delivery_paths", return_value=[]), \
             patch.object(weixin_adapter, "_send_text_chunk", new_callable=AsyncMock):

            result = await weixin_adapter.send("user-123", "hello")

            weixin_adapter._ensure_send_session_alive.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_returns_error_when_not_connected(self, weixin_adapter):
        """send() returns failure when _send_session is None."""
        weixin_adapter._send_session = None

        result = await weixin_adapter.send("user-123", "hello")

        assert result.success is False
        assert result.error == "Not connected"

    @pytest.mark.asyncio
    async def test_send_returns_error_when_no_token(self, weixin_adapter):
        """send() returns failure when _token is missing."""
        weixin_adapter._token = ""

        result = await weixin_adapter.send("user-123", "hello")

        assert result.success is False
        assert result.error == "Not connected"


class TestRecordSend:
    """Tests for _record_send — tracks last send time for idle detection."""

    def test_record_send_updates_timestamp(self, weixin_adapter):
        """_record_send should set _last_send_time to current monotonic time."""
        old_time = time.monotonic() - 1000
        weixin_adapter._last_send_time = old_time

        weixin_adapter._record_send()

        assert abs(weixin_adapter._last_send_time - time.monotonic()) < 1.0

    def test_record_send_value_is_monotonic(self, weixin_adapter):
        """Recorded time should increase (or stay same) after each call."""
        weixin_adapter._last_send_time = 0.0

        weixin_adapter._record_send()
        t1 = weixin_adapter._last_send_time
        time.sleep(0.01)
        weixin_adapter._record_send()
        t2 = weixin_adapter._last_send_time

        assert t2 >= t1


class TestSendSessionMaxIdleConstant:
    """Verify the idle threshold constant is reasonable."""

    def test_threshold_is_positive(self):
        from gateway.platforms.weixin import SEND_SESSION_MAX_IDLE_SECONDS
        assert SEND_SESSION_MAX_IDLE_SECONDS > 0

    def test_threshold_less_than_one_hour(self):
        """Threshold should be well under an hour to catch stale connections early."""
        from gateway.platforms.weixin import SEND_SESSION_MAX_IDLE_SECONDS
        assert SEND_SESSION_MAX_IDLE_SECONDS < 3600
