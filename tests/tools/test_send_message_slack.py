"""Slack-specific send_message delivery regressions.

Salvaged from #47547 and adapted to the post-#41112 plugin layout: the legacy
``_send_slack`` helper moved to ``plugins/platforms/slack/adapter.py::
_standalone_send`` and text sends now route through ``_send_via_adapter``
(live adapter first, registry standalone fallback).
"""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform
from tools.send_message_tool import _send_to_platform


def _ensure_slack_mock(monkeypatch):
    """Install lightweight Slack modules when optional Slack deps are absent."""
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
        ("slack_bolt.adapter.socket_mode.async_handler", slack_bolt.adapter.socket_mode.async_handler),
        ("slack_sdk", slack_sdk),
        ("slack_sdk.web", slack_sdk.web),
        ("slack_sdk.web.async_client", slack_sdk.web.async_client),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)


def test_slack_send_to_platform_routes_through_send_via_adapter(monkeypatch):
    """Slack text sends go through _send_via_adapter (live adapter first)."""
    _ensure_slack_mock(monkeypatch)

    live_send = AsyncMock(return_value={"success": True, "message_id": "live-ts"})

    with patch("tools.send_message_tool._send_via_adapter", live_send):
        result = asyncio.run(
            _send_to_platform(
                Platform.SLACK,
                SimpleNamespace(enabled=True, token="bad-token,good-token", extra={}),
                "C123",
                "**hello** from Hermes",
                thread_id="171.1",
            )
        )

    assert result == {"success": True, "message_id": "live-ts"}
    live_send.assert_awaited_once()
    call = live_send.await_args
    assert call.args[0] == Platform.SLACK
    assert call.args[2] == "C123"
    assert call.kwargs["thread_id"] == "171.1"


class _SlackResponse:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


class _SlackPostContext:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _SlackSession:
    """Fake aiohttp session whose good-token posts succeed."""

    def __init__(self):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, url, *, headers, json, **kwargs):
        token = headers["Authorization"].removeprefix("Bearer ")
        self.calls.append((token, json))
        if token == "good-token":
            payload = {"ok": True, "ts": "171.123"}
        else:
            payload = {"ok": False, "error": "invalid_auth"}
        return _SlackPostContext(_SlackResponse(payload))


@pytest.fixture
def _standalone_send(monkeypatch):
    _ensure_slack_mock(monkeypatch)
    from plugins.platforms.slack import adapter as slack_adapter

    return slack_adapter._standalone_send


def test_standalone_send_stops_on_non_token_error(monkeypatch, _standalone_send):
    """Terminal errors (not token-scoped) must not burn the remaining tokens."""

    class _FatalSession(_SlackSession):
        def post(self, url, *, headers, json, **kwargs):
            token = headers["Authorization"].removeprefix("Bearer ")
            self.calls.append((token, json))
            return _SlackPostContext(
                _SlackResponse({"ok": False, "error": "msg_too_long"})
            )

    fake_session = _FatalSession()
    monkeypatch.setattr(
        "aiohttp.ClientSession", lambda *args, **kwargs: fake_session
    )

    pconfig = SimpleNamespace(enabled=True, token="tok-a,tok-b", extra={})
    result = asyncio.run(_standalone_send(pconfig, "C123", "hello"))

    assert result == {"error": "Slack API error: msg_too_long"}
    assert len(fake_session.calls) == 1


def test_standalone_send_applies_rich_blocks(monkeypatch, _standalone_send):
    """Out-of-process Slack sends must match the live adapter's rich layout."""

    fake_session = _SlackSession()
    monkeypatch.setattr(
        "aiohttp.ClientSession", lambda *args, **kwargs: fake_session
    )
    message = """# Status

| Item | Result |
|---|---|
| Table | OK |"""
    pconfig = SimpleNamespace(
        enabled=True,
        token="good-token",
        extra={"rich_blocks": True, "feedback_buttons": False},
    )

    result = asyncio.run(_standalone_send(pconfig, "C123", message))

    assert result["success"] is True
    payload = fake_session.calls[0][1]
    assert payload["text"]
    assert [block["type"] for block in payload["blocks"]] == ["header", "table"]


def test_standalone_send_retries_without_rejected_blocks(monkeypatch, _standalone_send):
    """A formatting rejection must not drop the standalone message."""

    class _RejectBlocksSession(_SlackSession):
        def post(self, url, *, headers, json, **kwargs):
            self.calls.append(("good-token", dict(json)))
            if "blocks" in json:
                return _SlackPostContext(
                    _SlackResponse({"ok": False, "error": "invalid_blocks"})
                )
            return _SlackPostContext(_SlackResponse({"ok": True, "ts": "171.456"}))

    fake_session = _RejectBlocksSession()
    monkeypatch.setattr(
        "aiohttp.ClientSession", lambda *args, **kwargs: fake_session
    )
    pconfig = SimpleNamespace(
        enabled=True,
        token="good-token",
        extra={"rich_blocks": True, "feedback_buttons": False},
    )

    result = asyncio.run(_standalone_send(pconfig, "C123", "# Status"))

    assert result["success"] is True
    assert len(fake_session.calls) == 2
    assert "blocks" in fake_session.calls[0][1]
    assert "blocks" not in fake_session.calls[1][1]
