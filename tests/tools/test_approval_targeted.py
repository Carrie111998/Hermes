"""Request-targeted gateway approval resolution contracts."""

from __future__ import annotations

import threading
import time
from typing import Any, cast

import pytest

from tools import approval


@pytest.fixture(autouse=True)
def clean_gateway_queues():
    with approval._lock:
        approval._gateway_queues.clear()
    yield
    with approval._lock:
        approval._gateway_queues.clear()


def _entry(*, choices=None, **data):
    payload = dict(data)
    payload.setdefault("expires_at", time.time() + 60)
    if choices is not None:
        payload["choices"] = choices
    return approval._ApprovalEntry(payload)


def test_targeted_resolution_selects_exact_parallel_entry_not_fifo_head():
    first = _entry(choices=["once", "deny"], command="first")
    second = _entry(choices=["once", "deny"], command="second")
    approval._gateway_queues["desktop-session"] = [first, second]

    assert approval.resolve_gateway_approval_by_id(
        "desktop-session", second.request_id, "once"
    ) == 1
    assert second.event.is_set()
    assert second.result == "once"
    assert not first.event.is_set()
    assert approval._gateway_queues["desktop-session"] == [first]


def test_targeted_resolution_rejects_wrong_session_stale_and_replay():
    entry = _entry(choices=["once", "deny"])
    approval._gateway_queues["owner-session"] = [entry]

    assert approval.resolve_gateway_approval_by_id(
        "other-session", entry.request_id, "once"
    ) == 0
    assert approval.resolve_gateway_approval_by_id(
        "owner-session", "A" * 43, "once"
    ) == 0
    assert approval.resolve_gateway_approval_by_id(
        "owner-session", entry.request_id, "deny"
    ) == 1
    assert approval.resolve_gateway_approval_by_id(
        "owner-session", entry.request_id, "once"
    ) == 0


def test_targeted_resolution_rejects_choice_not_offered():
    entry = _entry(choices=["once", "deny"], smart_denied=True)
    approval._gateway_queues["desktop-session"] = [entry]

    assert approval.resolve_gateway_approval_by_id(
        "desktop-session", entry.request_id, "always"
    ) == 0
    assert not entry.event.is_set()
    assert approval.resolve_gateway_approval_by_id(
        "desktop-session", entry.request_id, "once"
    ) == 1


def test_targeted_resolution_publishes_result_while_timeout_lock_is_held():
    entered_set = threading.Event()
    release_set = threading.Event()

    class BlockingEvent:
        def set(self):
            entered_set.set()
            assert release_set.wait(2)

        def is_set(self):
            return entered_set.is_set()

    entry = _entry(choices=["once", "deny"])
    cast(Any, entry).event = BlockingEvent()
    approval._gateway_queues["desktop-session"] = [entry]
    result = []
    resolver = threading.Thread(
        target=lambda: result.append(
            approval.resolve_gateway_approval_by_id(
                "desktop-session", entry.request_id, "once"
            )
        )
    )
    resolver.start()
    assert entered_set.wait(2)

    acquired = threading.Event()

    def observe_lock():
        with approval._lock:
            acquired.set()

    observer = threading.Thread(target=observe_lock)
    observer.start()
    assert not acquired.wait(0.05)
    assert entry.result == "once"

    release_set.set()
    resolver.join(2)
    observer.join(2)
    assert result == [1]
    assert acquired.is_set()


def test_targeted_resolution_rejects_expired_authoritative_entry():
    entry = _entry(choices=["once", "deny"], expires_at=time.time() - 1)
    approval._gateway_queues["desktop-session"] = [entry]

    assert approval.resolve_gateway_approval_by_id(
        "desktop-session", entry.request_id, "once"
    ) == 0
    assert not entry.event.is_set()


def test_interrupt_atomically_owns_entry_before_late_remote_approval(monkeypatch):
    payloads = []
    monkeypatch.setattr(approval, "_get_approval_timeout", lambda: 60)
    monkeypatch.setattr(approval, "is_interrupted", lambda: True)

    result = approval._await_gateway_decision(
        "desktop-session",
        lambda payload: payloads.append(dict(payload)),
        {
            "command": "rm example",
            "description": "test",
            "choices": ["once", "deny"],
        },
        surface="desktop",
    )

    assert result["resolved"] is True
    assert result["choice"] == "deny"
    assert approval.resolve_gateway_approval_by_id(
        "desktop-session", payloads[0]["request_id"], "once"
    ) == 0


def test_notify_effect_then_failure_honors_committed_targeted_choice(monkeypatch):
    session_key = "desktop-session"
    monkeypatch.setattr(approval, "_get_approval_timeout", lambda: 60)
    monkeypatch.setattr(approval, "is_interrupted", lambda: False)

    def notify(payload):
        assert approval.resolve_gateway_approval_by_id(
            session_key, payload["request_id"], "once"
        ) == 1
        raise RuntimeError("delivery acknowledged, notifier cleanup failed")

    result = approval._await_gateway_decision(
        session_key,
        notify,
        {
            "command": "rm example",
            "description": "test",
            "choices": ["once", "deny"],
        },
        surface="desktop",
    )

    assert result["resolved"] is True
    assert result["choice"] == "once"
    assert "notify_failed" not in result


def test_gateway_notification_receives_opaque_id_and_canonical_expiry(monkeypatch):
    payloads = []
    session_key = "desktop-session"

    def notify(payload):
        payloads.append(dict(payload))
        approval.resolve_gateway_approval_by_id(
            session_key, payload["request_id"], "deny"
        )

    monkeypatch.setattr(approval, "_get_approval_timeout", lambda: 60)
    result = approval._await_gateway_decision(
        session_key,
        notify,
        {
            "command": "rm example",
            "description": "test",
            "choices": ["once", "deny"],
        },
        surface="desktop",
    )

    assert result["resolved"] is True
    assert result["choice"] == "deny"
    assert len(payloads) == 1
    request_id = payloads[0]["request_id"]
    assert len(request_id) == 43
    assert payloads[0]["expires_at"] > time.time()
    assert "session_key" not in payloads[0]
