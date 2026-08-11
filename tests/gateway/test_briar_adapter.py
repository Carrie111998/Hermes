"""Tests for the Briar platform adapter plugin."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from tests.gateway._plugin_adapter_loader import load_plugin_adapter

_briar_mod = load_plugin_adapter("briar")

BriarAdapter = _briar_mod.BriarAdapter
check_requirements = _briar_mod.check_requirements
validate_config = _briar_mod.validate_config
register = _briar_mod.register
_resolve_token = _briar_mod._resolve_token
_normalize_api_url = _briar_mod._normalize_api_url
_parse_comma_list = _briar_mod._parse_comma_list
BRIAR_DEFAULT_API_URL = _briar_mod.BRIAR_DEFAULT_API_URL
MAX_MESSAGE_LENGTH = _briar_mod.MAX_MESSAGE_LENGTH
BRIAR_MESSAGES_PATH_TEMPLATE = _briar_mod.BRIAR_MESSAGES_PATH_TEMPLATE


class FakeResponse:
    def __init__(self, *, status=200, json_data=None, text=""):
        self.status_code = status
        self._json = json_data or {}
        self.text = text

    def __await__(self):
        yield
        return self

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=None,
                response=self,
            )

    def json(self):
        return self._json


class FakeWSStream:
    status_code = 200

    def __init__(self, messages):
        self._messages = messages

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def aiter_text(self):
        for msg in self._messages:
            yield msg
        raise httpx.ReadError("stream closed")


async def _cancel(task):
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Protocol / module-level helpers
# ---------------------------------------------------------------------------


class TestBriarHelpers:

    def test_normalize_api_url_trailing_slash(self):
        assert _normalize_api_url("http://127.0.0.1:7000/") == "http://127.0.0.1:7000"

    def test_normalize_api_url_empty(self):
        assert _normalize_api_url("") == ""
        assert _normalize_api_url("   ") == ""

    def test_parse_comma_list(self):
        assert _parse_comma_list("a, b ,c") == ["a", "b", "c"]
        assert _parse_comma_list("") == []
        assert _parse_comma_list("single") == ["single"]

    def test_constants(self):
        assert MAX_MESSAGE_LENGTH == 4000
        assert "contact_id" in BRIAR_MESSAGES_PATH_TEMPLATE


# ---------------------------------------------------------------------------
# Init / config
# ---------------------------------------------------------------------------


class TestBriarAdapterInit:

    def test_init_from_config_extra(self, monkeypatch):
        monkeypatch.delenv("BRIAR_API_URL", raising=False)
        monkeypatch.delenv("BRIAR_CONTACT_ID", raising=False)
        monkeypatch.delenv("BRIAR_API_TOKEN", raising=False)
        from gateway.config import PlatformConfig

        cfg = PlatformConfig(
            enabled=True,
            extra={
                "api_url": "http://127.0.0.1:7000",
                "contact_id": "peer-a",
                "api_token": "secret",
            },
        )
        adapter = BriarAdapter(cfg)
        assert adapter.api_url == "http://127.0.0.1:7000"
        assert adapter.contact_id == "peer-a"
        assert adapter.token == "secret"
        assert adapter.allowed_users == []

    def test_env_overrides_config_extra(self, monkeypatch):
        monkeypatch.setenv("BRIAR_API_URL", "http://env:7000")
        monkeypatch.setenv("BRIAR_CONTACT_ID", "env-peer")
        monkeypatch.setenv("BRIAR_API_TOKEN", "env-tok")
        from gateway.config import PlatformConfig

        cfg = PlatformConfig(
            enabled=True,
            extra={
                "api_url": "http://cfg:7000",
                "contact_id": "cfg-peer",
                "api_token": "cfg-tok",
            },
        )
        adapter = BriarAdapter(cfg)
        assert adapter.api_url == "http://env:7000"
        assert adapter.contact_id == "env-peer"
        assert adapter.token == "env-tok"

    def test_allowed_users_from_env(self, monkeypatch):
        monkeypatch.setenv("BRIAR_ALLOWED_USERS", "a, b ,c")
        from gateway.config import PlatformConfig

        adapter = BriarAdapter(PlatformConfig(enabled=True, extra={}))
        assert adapter.allowed_users == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Send path
# ---------------------------------------------------------------------------


class TestBriarAdapterSend:

    @pytest.fixture
    def adapter(self, monkeypatch):
        monkeypatch.setenv("BRIAR_API_URL", "http://127.0.0.1:7000")
        monkeypatch.setenv("BRIAR_CONTACT_ID", "me")
        monkeypatch.setenv("BRIAR_API_TOKEN", "tok")
        from gateway.config import PlatformConfig

        return BriarAdapter(PlatformConfig(enabled=True, extra={}))

    @pytest.mark.asyncio
    async def test_send_success(self, adapter):
        client = MagicMock()
        client.post = AsyncMock(return_value=FakeResponse(status=200, json_data={"id": "msg-1"}))
        adapter._client = client

        result = await adapter.send("peer-b", "hello briar")
        assert result.success is True
        assert result.message_id == "msg-1"
        client.post.assert_called_once()
        call_url = client.post.call_args[0][0]
        assert "/v1/messages/peer-b" in call_url

    @pytest.mark.asyncio
    async def test_send_uses_default_contact_id(self, adapter):
        client = MagicMock()
        client.post = AsyncMock(return_value=FakeResponse(status=200, json_data={"id": "msg-2"}))
        adapter._client = client

        result = await adapter.send(None, "fallback contact")
        assert result.success is True
        call_url = client.post.call_args[0][0]
        assert "/v1/messages/me" in call_url

    @pytest.mark.asyncio
    async def test_send_not_connected(self, adapter):
        adapter._client = None
        result = await adapter.send("peer-b", "hi")
        assert result.success is False
        assert result.error == "not connected"

    @pytest.mark.asyncio
    async def test_send_http_error(self, adapter):
        client = MagicMock()
        client.post = AsyncMock(return_value=FakeResponse(status=403, text="forbidden"))
        adapter._client = client

        result = await adapter.send("peer-b", "hi")
        assert result.success is False
        assert "403" in result.error


# ---------------------------------------------------------------------------
# Inbound dispatch
# ---------------------------------------------------------------------------


class TestBriarAdapterInbound:

    @pytest.fixture
    def adapter(self, monkeypatch):
        monkeypatch.setenv("BRIAR_API_URL", "http://127.0.0.1:7000")
        monkeypatch.setenv("BRIAR_CONTACT_ID", "me")
        monkeypatch.setenv("BRIAR_API_TOKEN", "tok")
        from gateway.config import PlatformConfig

        return BriarAdapter(PlatformConfig(enabled=True, extra={}))

    @pytest.mark.asyncio
    async def test_dispatches_text_message(self, adapter):
        adapter.dispatch = AsyncMock()
        await adapter._handle_incoming_message(
            json.dumps(
                {
                    "name": "ConversationMessageReceivedEvent",
                    "data": {
                        "text": "hi",
                        "contactId": "peer",
                        "timestamp": 1,
                        "local": False,
                    },
                }
            )
        )
        assert adapter.dispatch.call_count == 1
        event = adapter.dispatch.call_args[0][0]
        assert event.text == "hi"
        assert event.source.chat_id == "peer"
        assert event.source.user_id == "peer"

    @pytest.mark.asyncio
    async def test_allowed_users_filters_inbound(self, adapter):
        adapter.allowed_users = ["allowed"]
        adapter.dispatch = AsyncMock()
        await adapter._handle_incoming_message(
            json.dumps(
                {
                    "name": "ConversationMessageReceivedEvent",
                    "data": {
                        "text": "ok",
                        "contactId": "allowed",
                        "timestamp": 1,
                        "local": False,
                    },
                }
            )
        )
        await adapter._handle_incoming_message(
            json.dumps(
                {
                    "name": "ConversationMessageReceivedEvent",
                    "data": {
                        "text": "nope",
                        "contactId": "blocked",
                        "timestamp": 2,
                        "local": False,
                    },
                }
            )
        )
        assert adapter.dispatch.call_count == 1
        event = adapter.dispatch.call_args[0][0]
        assert event.source.user_id == "allowed"

    @pytest.mark.asyncio
    async def test_ignores_empty_message(self, adapter):
        adapter.dispatch = AsyncMock()
        await adapter._handle_incoming_message(
            json.dumps(
                {
                    "name": "ConversationMessageReceivedEvent",
                    "data": {
                        "timestamp": 1,
                        "contactId": "peer",
                        "local": False,
                    },
                }
            )
        )
        assert adapter.dispatch.call_count == 0

    @pytest.mark.asyncio
    async def test_ignores_local_message(self, adapter):
        adapter.dispatch = AsyncMock()
        await adapter._handle_incoming_message(
            json.dumps(
                {
                    "name": "ConversationMessageReceivedEvent",
                    "data": {
                        "text": "self",
                        "contactId": "me",
                        "timestamp": 1,
                        "local": True,
                    },
                }
            )
        )
        assert adapter.dispatch.call_count == 0

    @pytest.mark.asyncio
    async def test_ignores_non_message_event(self, adapter):
        adapter.dispatch = AsyncMock()
        await adapter._handle_incoming_message(
            json.dumps({"name": "ContactAddedEvent", "data": {}})
        )
        assert adapter.dispatch.call_count == 0

    @pytest.mark.asyncio
    async def test_ignores_malformed_json(self, adapter):
        adapter.dispatch = AsyncMock()
        await adapter._handle_incoming_message("not-json")
        assert adapter.dispatch.call_count == 0


# ---------------------------------------------------------------------------
# Connection lifecycle / websocket loop
# ---------------------------------------------------------------------------


class TestBriarAdapterLifecycle:

    @pytest.fixture
    def adapter(self, monkeypatch):
        monkeypatch.setenv("BRIAR_API_URL", "http://127.0.0.1:7000")
        monkeypatch.setenv("BRIAR_CONTACT_ID", "cid")
        monkeypatch.setenv("BRIAR_API_TOKEN", "tok")
        from gateway.config import PlatformConfig

        return BriarAdapter(PlatformConfig(enabled=True, extra={}))

    @pytest.mark.asyncio
    async def test_connect_starts_websocket_task(self, adapter):
        client = MagicMock()
        client.get.return_value = FakeResponse(status=200)
        client.stream.return_value = FakeWSStream([])
        client.aclose = AsyncMock()
        with patch("plugins.platforms.briar.adapter.httpx.AsyncClient", return_value=client):
            result = await adapter.connect()
        assert result is True
        assert adapter._ws_task is not None
        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_disconnect_cancels_task(self, adapter):
        client = MagicMock()
        client.get.return_value = FakeResponse(status=200)
        client.stream.return_value = FakeWSStream([])
        client.aclose = AsyncMock()
        with patch("plugins.platforms.briar.adapter.httpx.AsyncClient", return_value=client):
            await adapter.connect()
        task = adapter._ws_task
        assert task is not None
        await adapter.disconnect()
        assert task.done()
        assert adapter._client is None

    @pytest.mark.asyncio
    async def test_websocket_dispatches_messages(self, adapter):
        client = MagicMock()
        client.get.return_value = FakeResponse(status=200)
        client.stream.return_value = FakeWSStream(
            [
                json.dumps(
                    {
                        "name": "ConversationMessageReceivedEvent",
                        "data": {
                            "text": "hello",
                            "contactId": "peer",
                            "timestamp": 1,
                            "local": False,
                        },
                    }
                )
            ]
        )
        client.aclose = AsyncMock()
        adapter.dispatch = AsyncMock()
        with patch("plugins.platforms.briar.adapter.httpx.AsyncClient", return_value=client):
            await adapter.connect()
            await asyncio.sleep(0)
            if adapter._ws_task:
                adapter._ws_task.cancel()
                try:
                    await adapter._ws_task
                except asyncio.CancelledError:
                    pass
        assert adapter.dispatch.call_count == 1
        await adapter.disconnect()
