"""Regression coverage for SimpleX upstream-review findings.

Kept separate from ``test_simplex_plugin.py`` so the adapter's corrective
contracts remain reviewable without extending the already-large core suite.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.gateway._plugin_adapter_loader import load_plugin_adapter

_simplex = load_plugin_adapter("simplex")

SimplexAdapter = _simplex.SimplexAdapter


def _adapter(*, extra: dict | None = None) -> SimplexAdapter:
    from gateway.config import PlatformConfig

    return SimplexAdapter(
        PlatformConfig(
            enabled=True,
            extra=(
                extra
                if extra is not None
                else {"ws_url": "ws://localhost:5225"}
            ),
        )
    )


def _text_event(adapter: SimplexAdapter, message_id: str, text: str):
    from gateway.platforms.base import MessageEvent, MessageType

    return MessageEvent(
        source=adapter.build_source(
            chat_id="42",
            chat_name="contact-42",
            chat_type="dm",
            user_id="42",
            user_name="contact-42",
            message_id=message_id,
        ),
        text=text,
        message_type=MessageType.TEXT,
        message_id=message_id,
    )


@pytest.mark.asyncio
async def test_cancelled_flush_rebuffers_complete_batch_before_newer_text():
    """Cancellation after pop must not lose text or edit-correlation metadata."""
    adapter = _adapter()
    adapter._text_batch_delay = 0
    first_dispatch_started = asyncio.Event()
    dispatched = []

    async def blocked_then_capture(event):
        if event.text == "first":
            first_dispatch_started.set()
            await asyncio.Event().wait()
        dispatched.append(event)

    adapter.handle_message = blocked_then_capture
    adapter._enqueue_text_event(_text_event(adapter, "1", "first"))
    await asyncio.wait_for(first_dispatch_started.wait(), timeout=1)

    # This resets the timer while the prior flush is already dispatching.
    adapter._enqueue_text_event(_text_event(adapter, "2", "second"))
    await asyncio.gather(*list(adapter._pending_text_batch_tasks.values()))

    assert [event.text for event in dispatched] == ["first\nsecond"]
    assert dispatched[0].metadata["simplex_batch_items"] == [
        {"message_id": "1", "text": "first"},
        {"message_id": "2", "text": "second"},
    ]


@pytest.fixture
def secondary_profile_scope():
    from agent.secret_scope import (
        reset_secret_scope,
        set_multiplex_active,
        set_secret_scope,
    )

    set_multiplex_active(True)
    token = set_secret_scope({})
    try:
        yield
    finally:
        reset_secret_scope(token)
        set_multiplex_active(False)


def test_secondary_profile_does_not_inherit_default_simplex_env(
    monkeypatch, secondary_profile_scope
):
    monkeypatch.setenv("SIMPLEX_WS_URL", "ws://default-daemon:5225")
    monkeypatch.setenv("SIMPLEX_AUTO_ACCEPT", "false")
    monkeypatch.setenv("SIMPLEX_GROUP_ALLOWED", "*")

    configured = _adapter(
        extra={
            "ws_url": "ws://secondary-daemon:5225",
            "auto_accept": True,
            "group_allowed": "secondary-only",
        }
    )
    unconfigured = _adapter(extra={})

    assert configured.ws_url == "ws://secondary-daemon:5225"
    assert configured.auto_accept is True
    assert configured.group_allow_from == {"secondary-only"}
    assert unconfigured.auto_accept is True
    assert unconfigured.group_allow_from == set()
    assert _simplex.validate_config(unconfigured.config) is False
    assert _simplex.is_connected(unconfigured.config) is False
    assert _simplex._env_enablement() is None


@pytest.mark.asyncio
async def test_secondary_standalone_send_uses_profile_daemon(
    monkeypatch, secondary_profile_scope
):
    monkeypatch.setenv("SIMPLEX_WS_URL", "ws://default-daemon:5225")
    connected_urls = []

    class DummyWs:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def send(self, payload):
            self.corr_id = json.loads(payload)["corrId"]

        async def recv(self):
            return json.dumps(
                {
                    "corrId": self.corr_id,
                    "resp": {
                        "type": "newChatItems",
                        "chatItems": [
                            {"chatItem": {"meta": {"itemId": 7}}}
                        ],
                    },
                }
            )

    def fake_connect(url, **_kwargs):
        connected_urls.append(url)
        return DummyWs()

    import websockets

    monkeypatch.setattr(websockets, "connect", fake_connect)
    result = await _simplex._standalone_send(
        SimpleNamespace(extra={"ws_url": "ws://secondary-daemon:5225"}),
        "42",
        "hello",
    )

    assert result["success"] is True
    assert connected_urls == ["ws://secondary-daemon:5225"]


@pytest.mark.asyncio
async def test_connect_holds_scoped_listener_lock_until_disconnect(monkeypatch):
    adapter = _adapter()
    acquire = MagicMock(return_value=True)
    release = MagicMock()
    adapter._acquire_platform_lock = acquire
    adapter._release_platform_lock = release

    async def ready_listener():
        adapter._ws = AsyncMock()
        adapter._ws_ready.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(adapter, "_ws_listener", ready_listener)
    monkeypatch.setattr(adapter, "_health_monitor", AsyncMock())

    assert await adapter.connect() is True
    acquire.assert_called_once_with(
        "simplex-ws", "ws://localhost:5225", "SimpleX daemon URL"
    )
    release.assert_not_called()

    await adapter.disconnect()
    release.assert_called_once()


@pytest.mark.asyncio
async def test_connect_fails_before_listener_when_lock_is_held(monkeypatch):
    adapter = _adapter()
    adapter._acquire_platform_lock = MagicMock(return_value=False)
    adapter._release_platform_lock = MagicMock()
    listener = AsyncMock()
    monkeypatch.setattr(adapter, "_ws_listener", listener)

    assert await adapter.connect() is False
    listener.assert_not_awaited()
    adapter._release_platform_lock.assert_not_called()


def _group_item(group_id: int, member_id: int, text: str) -> dict:
    return {
        "chatInfo": {
            "type": "group",
            "groupInfo": {
                "groupId": group_id,
                "localDisplayName": f"group-{group_id}",
            },
        },
        "chatItem": {
            "chatDir": {
                "type": "groupRcv",
                "groupMember": {
                    "memberId": member_id,
                    "localDisplayName": f"member-{member_id}",
                },
            },
            "meta": {"itemId": 1, "itemTs": "2026-01-01T00:00:00Z"},
            "content": {
                "type": "rcvMsgContent",
                "msgContent": {"type": "text", "text": text},
            },
        },
    }


@pytest.mark.asyncio
async def test_allowed_group_carries_authorization_to_gateway(monkeypatch):
    monkeypatch.setenv("SIMPLEX_GROUP_ALLOWED", "12")
    adapter = _adapter()
    adapter._text_batch_delay = 0
    dispatched = []

    async def capture(event):
        dispatched.append(event)

    adapter.handle_message = capture
    await adapter._handle_chat_item(_group_item(12, 99, "authorized group"))
    await asyncio.gather(*list(adapter._pending_text_batch_tasks.values()))

    assert dispatched[0].source.role_authorized is True

    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.pairing_store = SimpleNamespace(is_approved=lambda *_a, **_kw: False)
    monkeypatch.setenv("SIMPLEX_ALLOWED_USERS", "42")
    monkeypatch.delenv("SIMPLEX_ALLOW_ALL_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOWED_USERS", raising=False)
    assert runner._is_user_authorized(dispatched[0].source) is True


@pytest.mark.asyncio
async def test_unlisted_group_is_dropped_without_authorization(monkeypatch):
    monkeypatch.setenv("SIMPLEX_GROUP_ALLOWED", "12")
    adapter = _adapter()
    adapter._text_batch_delay = 0
    adapter.handle_message = AsyncMock()

    await adapter._handle_chat_item(_group_item(99, 7, "not authorized"))
    assert adapter._pending_text_batch_tasks == {}
    adapter.handle_message.assert_not_awaited()
