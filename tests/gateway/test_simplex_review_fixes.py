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
async def test_new_text_does_not_cancel_an_inflight_dispatch():
    """A downstream side effect must not be duplicated by batch cancellation."""
    adapter = _adapter()
    adapter._text_batch_delay = 0
    first_dispatch_started = asyncio.Event()
    allow_first_dispatch_to_finish = asyncio.Event()
    dispatched = []

    async def blocked_then_capture(event):
        dispatched.append(event)
        if event.text == "first":
            first_dispatch_started.set()
            await allow_first_dispatch_to_finish.wait()

    adapter.handle_message = blocked_then_capture
    adapter._enqueue_text_event(_text_event(adapter, "1", "first"))
    await asyncio.wait_for(first_dispatch_started.wait(), timeout=1)

    # This resets the timer while the prior flush is already dispatching.
    adapter._enqueue_text_event(_text_event(adapter, "2", "second"))
    allow_first_dispatch_to_finish.set()
    await asyncio.gather(*list(adapter._pending_text_batch_tasks.values()))

    assert [event.text for event in dispatched] == ["first", "second"]
    assert dispatched[0].metadata["correlated_message_items"] == [
        {"message_id": "1", "text": "first"},
    ]
    assert dispatched[1].metadata["correlated_message_items"] == [
        {"message_id": "2", "text": "second"},
    ]


@pytest.mark.asyncio
async def test_cancelled_inflight_dispatch_retries_when_adapter_is_running():
    adapter = _adapter()
    adapter._running = True
    adapter._text_batch_delay = 0
    first_dispatch_started = asyncio.Event()
    dispatched = []
    attempts = 0

    async def cancelled_then_capture(event):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            first_dispatch_started.set()
            await asyncio.Event().wait()
        dispatched.append(event)

    adapter.handle_message = cancelled_then_capture
    adapter._enqueue_text_event(_text_event(adapter, "1", "first"))
    await asyncio.wait_for(first_dispatch_started.wait(), timeout=1)
    adapter._pending_text_batch_tasks[
        adapter._text_batch_key(_text_event(adapter, "1", "first"))
    ].cancel()
    await asyncio.sleep(0)
    await asyncio.gather(*list(adapter._pending_text_batch_tasks.values()))

    assert [event.text for event in dispatched] == ["first"]
    assert dispatched[0].metadata["correlated_message_items"] == [
        {"message_id": "1", "text": "first"},
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
    monkeypatch.setenv("HERMES_SIMPLEX_TEXT_BATCH_DELAY", "30")

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
    assert configured._text_batch_delay == 0.8
    assert unconfigured.auto_accept is True
    assert unconfigured.group_allow_from == set()
    assert _simplex.validate_config(unconfigured.config) is False
    assert _simplex.is_connected(unconfigured.config) is False
    assert _simplex._env_enablement() is None


def test_profile_scope_detection_failure_does_not_fall_through_to_env(
    monkeypatch,
):
    import sys

    from plugins.platforms.simplex import config as simplex_config

    monkeypatch.setenv("SIMPLEX_WS_URL", "ws://default-daemon:5225")
    monkeypatch.setitem(sys.modules, "agent.secret_scope", None)

    assert simplex_config.profile_scoped() is True
    assert (
        simplex_config.scoped_platform_setting("SIMPLEX_WS_URL", {}, "ws_url")
        is None
    )


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
async def test_secondary_standalone_send_without_profile_daemon_fails_closed(
    monkeypatch, secondary_profile_scope
):
    monkeypatch.setenv("SIMPLEX_WS_URL", "ws://default-daemon:5225")

    import websockets

    connect = MagicMock()
    monkeypatch.setattr(websockets, "connect", connect)

    result = await _simplex._standalone_send(
        SimpleNamespace(extra={}),
        "42",
        "hello",
    )

    assert result == {
        "error": (
            "SimpleX standalone send: SIMPLEX_WS_URL is required for the "
            "active profile"
        )
    }
    connect.assert_not_called()


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


@pytest.mark.asyncio
async def test_connect_during_reconnect_does_not_start_second_listener(monkeypatch):
    adapter = _adapter()
    adapter._connect_timeout = 0.01
    adapter._running = True
    adapter._ws = None
    adapter._ws_ready.clear()
    adapter._ws_task = asyncio.create_task(asyncio.Event().wait())
    adapter._acquire_platform_lock = MagicMock(return_value=True)

    try:
        assert await adapter.connect() is False
        adapter._acquire_platform_lock.assert_not_called()
        assert not adapter._ws_task.done()
    finally:
        adapter._ws_task.cancel()
        await asyncio.gather(adapter._ws_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_connect_double_cancel_still_releases_listener_lock(monkeypatch):
    adapter = _adapter()
    adapter._acquire_platform_lock = MagicMock(return_value=True)
    adapter._release_platform_lock = MagicMock()
    listener_started = asyncio.Event()
    listener_cancelled = asyncio.Event()
    allow_listener_exit = asyncio.Event()

    async def slow_listener_cleanup():
        listener_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            listener_cancelled.set()
            await allow_listener_exit.wait()
            raise

    monkeypatch.setattr(adapter, "_ws_listener", slow_listener_cleanup)
    connect_task = asyncio.create_task(adapter.connect())
    await asyncio.wait_for(listener_started.wait(), timeout=1)

    connect_task.cancel()
    await asyncio.wait_for(listener_cancelled.wait(), timeout=1)
    connect_task.cancel()
    allow_listener_exit.set()

    with pytest.raises(asyncio.CancelledError):
        await connect_task
    adapter._release_platform_lock.assert_called_once()


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


@pytest.mark.asyncio
async def test_group_is_dropped_when_group_allowlist_is_unset(monkeypatch):
    monkeypatch.delenv("SIMPLEX_GROUP_ALLOWED", raising=False)
    adapter = _adapter()
    adapter.handle_message = AsyncMock()

    await adapter._handle_chat_item(_group_item(12, 7, "no group policy"))

    assert adapter.group_allow_from == set()
    assert adapter._pending_text_batch_tasks == {}
    adapter.handle_message.assert_not_awaited()


def test_group_authorization_grant_is_not_persisted():
    from gateway.session import SessionSource

    source = _adapter().build_source(
        chat_id="group:12",
        chat_type="group",
        user_id="99",
        role_authorized=True,
    )

    serialized = source.to_dict()
    restored = SessionSource.from_dict(serialized)

    assert "role_authorized" not in serialized
    assert restored.role_authorized is False


@pytest.mark.asyncio
async def test_allowed_group_file_keeps_intake_authorization(monkeypatch):
    monkeypatch.setenv("SIMPLEX_GROUP_ALLOWED", "12")
    adapter = _adapter()
    adapter._receive_file = AsyncMock()
    adapter.set_authorization_check(lambda *_args: False)
    wrapper = _group_item(12, 99, "authorized attachment")
    wrapper["chatItem"]["file"] = {
        "fileId": 7,
        "fileName": "report.pdf",
        "fileStatus": {"type": "rcvInvitation"},
    }

    await adapter._handle_event(
        {
            "resp": {
                "type": "rcvFileDescrReady",
                "rcvFileTransfer": {"fileId": 7, "fileName": "report.pdf"},
                "chatItem": wrapper,
            }
        }
    )
    await asyncio.gather(*list(adapter._command_tasks))

    adapter._receive_file.assert_awaited_once_with(7, None)


@pytest.mark.asyncio
async def test_unlisted_group_file_is_rejected_before_receive(monkeypatch):
    monkeypatch.setenv("SIMPLEX_GROUP_ALLOWED", "12")
    adapter = _adapter()
    adapter._receive_file = AsyncMock()
    adapter.set_authorization_check(lambda *_args: True)
    wrapper = _group_item(99, 7, "unapproved attachment")
    wrapper["chatItem"]["file"] = {
        "fileId": 8,
        "fileName": "report.pdf",
        "fileStatus": {"type": "rcvInvitation"},
    }

    await adapter._handle_event(
        {
            "resp": {
                "type": "rcvFileDescrReady",
                "rcvFileTransfer": {"fileId": 8, "fileName": "report.pdf"},
                "chatItem": wrapper,
            }
        }
    )

    adapter._receive_file.assert_not_awaited()
