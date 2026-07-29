"""Cross-process Desktop → messaging approval bridge contracts."""

from __future__ import annotations

import secrets
import stat
import threading
import time
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

import gateway.cross_surface_bridge as bridge
from hermes_constants import reset_hermes_home_override, set_hermes_home_override

_DESTINATION = {
    "chat_id": "8068859990",
    "thread_id": None,
    "user_id": "8068859990",
}


def _approval_payload(**overrides):
    payload = {
        "request_id": secrets.token_urlsafe(32),
        "expires_at": time.time() + 300,
        "command": "redacted",
        "choices": ["once", "session", "always", "deny"],
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def isolated_bridge(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        bridge,
        "_settings",
        lambda: {
            "enabled": True,
            "target": "telegram",
            "process_notifications": True,
        },
    )
    monkeypatch.setattr(bridge, "_ensure_resolver_thread", lambda: None)
    with bridge._local_lock:
        bridge._local_approvals.clear()
        bridge._resolver_stop.clear()
        bridge._resolver_terminal = False
    yield tmp_path
    with bridge._local_lock:
        bridge._local_approvals.clear()
        bridge._resolver_stop.clear()
        bridge._resolver_terminal = False


def test_approval_mailbox_uses_opaque_single_use_token_and_omits_session_key(isolated_bridge):
    token = bridge.publish_approval(
        "desktop-secret-session-key",
        _approval_payload(**{
            "command": "redacted command",
            "description": "dangerous command",
            "allow_permanent": True,
        }),
    )
    assert token and len(token) >= 32

    claimed = bridge.claim_events("telegram", "gateway-A")
    assert len(claimed) == 1
    assert claimed[0]["token"] == token
    assert claimed[0]["payload"]["command"] == "redacted command"

    db_bytes = bridge._db_path().read_bytes()
    assert b"desktop-secret-session-key" not in db_bytes
    assert stat.S_IMODE(bridge._db_path().stat().st_mode) == 0o600
    for suffix in ("-wal", "-shm"):
        sidecar = type(bridge._db_path())(f"{bridge._db_path()}{suffix}")
        if sidecar.exists():
            assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
            assert b"desktop-secret-session-key" not in sidecar.read_bytes()

    assert bridge.mark_delivered(
        token, "gateway-A", "telegram-message-1", **_DESTINATION
    ) is True
    assert bridge.resolve_request(
        token,
        "once",
        chat_id=_DESTINATION["chat_id"],
        thread_id="unexpected-topic",
        user_id=_DESTINATION["user_id"],
        message_id="telegram-message-1",
    ) is False
    assert bridge.resolve_request(
        token, "once", message_id="telegram-message-1", **_DESTINATION
    ) is True
    assert bridge.resolve_request(token, "deny", **_DESTINATION) is False  # single-use
    assert bridge.resolve_request(token, "bogus", **_DESTINATION) is False


def test_resolution_rejects_wrong_destination_user_thread_and_choice(isolated_bridge):
    payload = _approval_payload(choices=["once", "deny"])
    token = bridge.publish_approval("desktop-session", payload)
    assert token
    assert bridge.claim_events("telegram", "gateway-A")
    assert bridge.mark_delivered(
        token,
        "gateway-A",
        "message-bound",
        chat_id="home-chat",
        thread_id="home-thread",
        user_id="owner-user",
    )

    assert not bridge.resolve_request(
        token, "once", chat_id="other-chat", thread_id="home-thread", user_id="owner-user",
        message_id="message-bound",
    )
    assert not bridge.resolve_request(
        token, "once", chat_id="home-chat", thread_id="other-thread", user_id="owner-user",
        message_id="message-bound",
    )
    assert not bridge.resolve_request(
        token, "once", chat_id="home-chat", thread_id="home-thread", user_id="other-user",
        message_id="message-bound",
    )
    assert not bridge.resolve_request(
        token, "always", chat_id="home-chat", thread_id="home-thread", user_id="owner-user",
        message_id="message-bound",
    )
    assert not bridge.resolve_request(
        token, "once", chat_id="home-chat", thread_id="home-thread", user_id="owner-user",
        message_id="other-message",
    )
    assert bridge.resolve_request(
        token, "deny", chat_id="home-chat", thread_id="home-thread", user_id="owner-user",
        message_id="message-bound",
    )


def test_recorded_decision_resolves_original_desktop_session_once(isolated_bridge, monkeypatch):
    token = bridge.publish_approval("desktop-session-A", _approval_payload())
    assert token
    assert bridge.claim_events("telegram", "gateway-A")
    assert bridge.mark_delivered(token, "gateway-A", "message-2", **_DESTINATION)
    assert bridge.resolve_request(
        token, "session", message_id="message-2", **_DESTINATION
    )

    calls = []
    monkeypatch.setattr("tools.approval.has_blocking_approval", lambda key: True)
    monkeypatch.setattr(
        "tools.approval.resolve_gateway_approval_by_id",
        lambda key, request_id, choice: calls.append((key, request_id, choice)) or 1,
    )

    assert bridge._resolve_local_decisions_once() == 1
    assert calls == [("desktop-session-A", token, "session")]
    assert bridge._resolve_local_decisions_once() == 0
    with bridge._connect() as conn:
        row = conn.execute(
            "SELECT status, decision FROM bridge_events WHERE token=?", (token,)
        ).fetchone()
    assert tuple(row) == ("resolved", "session")


def test_resolver_restores_publishing_profile_home(isolated_bridge, monkeypatch):
    profile_home = isolated_bridge / "profiles" / "secondary"
    profile_home.mkdir(parents=True)
    scope = set_hermes_home_override(profile_home)
    try:
        token = bridge.publish_approval("secondary-session", _approval_payload())
        assert token
        assert bridge.claim_events("telegram", "gateway-secondary")
        assert bridge.mark_delivered(
            token, "gateway-secondary", "message-profile", **_DESTINATION
        )
        assert bridge.resolve_request(
            token, "once", message_id="message-profile", **_DESTINATION
        )
    finally:
        reset_hermes_home_override(scope)

    calls = []
    monkeypatch.setattr("tools.approval.has_blocking_approval", lambda key: True)
    monkeypatch.setattr(
        "tools.approval.resolve_gateway_approval_by_id",
        lambda key, request_id, choice: calls.append((key, request_id, choice)) or 1,
    )
    assert bridge._resolve_local_decisions_once() == 1
    assert calls == [("secondary-session", token, "once")]


def test_claim_lease_prevents_duplicate_gateway_delivery(isolated_bridge):
    token = bridge.publish_notification(
        "Desktop background process completed.", dedupe_key="process:one"
    )
    assert token
    assert len(bridge.claim_events("telegram", "gateway-A", lease_seconds=30)) == 1
    assert bridge.claim_events("telegram", "gateway-B") == []
    assert bridge.release_claim(token, "gateway-B") is False
    assert bridge.release_claim(token, "gateway-A") is True
    assert len(bridge.claim_events("telegram", "gateway-B")) == 1


def test_process_notification_deduplication(isolated_bridge):
    text = "Desktop background process completed (exit code 0)."
    first = bridge.publish_notification(text, dedupe_key="process:abc")
    second = bridge.publish_notification(text, dedupe_key="process:abc")
    assert first
    assert second is None
    assert len(bridge.claim_events("telegram", "gateway-A")) == 1


def test_process_notification_api_rejects_freeform_sensitive_text(isolated_bridge):
    assert bridge.publish_notification(
        "command=curl secret-output", dedupe_key="process:sensitive"
    ) is None
    assert bridge.claim_events("telegram", "gateway-A") == []


def test_shutdown_invalidates_item_snapshotted_by_resolver(isolated_bridge, monkeypatch):
    token = bridge.publish_approval("desktop-session", _approval_payload())
    assert token
    assert bridge.claim_events("telegram", "gateway-A")
    assert bridge.mark_delivered(token, "gateway-A", "message", **_DESTINATION)
    assert bridge.resolve_request(token, "once", message_id="message", **_DESTINATION)

    decision_started = threading.Event()
    release_decision = threading.Event()
    original_decision = bridge._decision

    def blocked_decision(request_token):
        decision_started.set()
        assert release_decision.wait(2)
        return original_decision(request_token)

    calls = []
    monkeypatch.setattr(bridge, "_decision", blocked_decision)
    monkeypatch.setattr("tools.approval.has_blocking_approval", lambda _key: True)
    monkeypatch.setattr(
        "tools.approval.resolve_gateway_approval_by_id",
        lambda *args: calls.append(args) or 1,
    )

    resolver = threading.Thread(target=bridge._resolve_local_decisions_once)
    resolver.start()
    assert decision_started.wait(2)
    bridge.stop_local_resolver()
    release_decision.set()
    resolver.join(2)

    assert calls == []
    assert token not in bridge._local_approvals


def test_shutdown_is_terminal_for_late_publication(isolated_bridge):
    bridge.stop_local_resolver()

    assert bridge.publish_approval("late-session", _approval_payload()) is None
    assert bridge.publish_notification(
        "Desktop background process completed.", dedupe_key="process:late"
    ) is None
    assert bridge._resolver_stop.is_set()
    assert bridge._resolver_terminal is True
    assert bridge._local_approvals == {}


def test_expired_or_undelivered_request_cannot_be_resolved(isolated_bridge):
    token = bridge.publish_approval(
        "desktop-session", _approval_payload(command="safe preview")
    )
    assert token
    assert bridge.resolve_request(token, "once", **_DESTINATION) is False

    with bridge._connect() as conn:
        conn.execute(
            "UPDATE bridge_events SET expires_at=? WHERE token=?",
            (time.time() - 1, token),
        )
    assert bridge.claim_events("telegram", "gateway-A") == []
    assert bridge.resolve_request(token, "once", **_DESTINATION) is False


def test_payload_is_bounded_and_allowlisted(isolated_bridge):
    token = bridge.publish_approval(
        "desktop-session",
        _approval_payload(**{
            "command": "x" * 9000,
            "description": "y" * 9000,
            "choices": ["once", "root", "deny"],
            "untrusted": {"session_key": "must-not-persist"},
        }),
    )
    event = bridge.claim_events("telegram", "gateway-A")[0]
    assert len(event["payload"]["command"]) == bridge._MAX_TEXT
    assert len(event["payload"]["description"]) == bridge._MAX_TEXT
    assert event["payload"]["choices"] == ["once", "deny"]
    assert "untrusted" not in event["payload"]


@pytest.mark.asyncio
async def test_gateway_watcher_routes_approval_to_home_with_opaque_session_key(
    isolated_bridge, monkeypatch
):
    from gateway.config import Platform
    from gateway.run import GatewayRunner

    runner = cast(Any, object.__new__(GatewayRunner))
    runner._running = True
    adapter = SimpleNamespace()

    async def send_exec_approval(**kwargs):
        runner._running = False
        return SimpleNamespace(success=True, message_id="tg-42", error=None)

    adapter.send_exec_approval = AsyncMock(side_effect=send_exec_approval)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner.config = SimpleNamespace(
        get_home_channel=lambda platform: SimpleNamespace(
            chat_id="8068859990", thread_id=None, user_id="8068859990"
        )
    )

    token = bridge.publish_approval("desktop-owner", _approval_payload())
    delivered = []
    monkeypatch.setattr(
        bridge,
        "mark_delivered",
        lambda token, owner, message_id=None, **destination: delivered.append(
            (token, message_id, destination)
        ) or True,
    )

    await runner._cross_surface_bridge_watcher()

    kwargs = adapter.send_exec_approval.call_args.kwargs
    assert kwargs["chat_id"] == "8068859990"
    assert kwargs["session_key"] == f"xsurf:{token}"
    assert kwargs["command"] == "redacted"
    assert delivered == [
        (
            token,
            "tg-42",
            {"chat_id": "8068859990", "thread_id": None, "user_id": "8068859990"},
        )
    ]


@pytest.mark.asyncio
async def test_gateway_watcher_routes_notification_to_configured_topic(
    isolated_bridge, monkeypatch
):
    from gateway.config import Platform
    from gateway.run import GatewayRunner

    runner = cast(Any, object.__new__(GatewayRunner))
    runner._running = True

    async def send(*args, **kwargs):
        runner._running = False
        return SimpleNamespace(success=True, message_id="tg-notice", error=None)

    adapter = SimpleNamespace(send=AsyncMock(side_effect=send))
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner.config = SimpleNamespace(
        get_home_channel=lambda platform: SimpleNamespace(
            chat_id="8068859990", thread_id="topic-7", user_id="8068859990"
        )
    )
    token = bridge.publish_notification(
        "Desktop background process completed.", dedupe_key="process:topic"
    )
    assert token
    delivered = []
    monkeypatch.setattr(
        bridge,
        "mark_delivered",
        lambda token, owner, message_id=None, **destination: delivered.append(
            (token, message_id, destination)
        )
        or True,
    )

    await runner._cross_surface_bridge_watcher()

    args = adapter.send.call_args.args
    kwargs = adapter.send.call_args.kwargs
    assert args[0] == "8068859990"
    assert kwargs["metadata"] == {"thread_id": "topic-7"}
    assert delivered == [
        (
            token,
            "tg-notice",
            {
                "chat_id": "8068859990",
                "thread_id": "topic-7",
                "user_id": "8068859990",
            },
        )
    ]
