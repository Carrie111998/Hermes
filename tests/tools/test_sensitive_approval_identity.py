"""Sensitive approval identity binding.

These tests pin the generic primitive used by future high-risk rails: a
sensitive approval must resolve by request id plus exact observed approver
context, never by FIFO session key alone.
"""

import threading
import time


def _clear_state():
    from tools import approval as mod

    mod._gateway_queues.clear()
    mod._gateway_notify_cbs.clear()
    mod._session_approved.clear()
    mod._permanent_approved.clear()
    mod._pending.clear()


def _ctx(session_key="sess-sensitive", user_id="u1", session_id="sid-sensitive"):
    return {
        "platform": "telegram",
        "chat_id": "chat-1",
        "user_id": user_id,
        "thread_id": "thread-1",
        "session_id": session_id,
        "session_key": session_key,
    }


def _entry(session_key="sess-sensitive", **data):
    from tools.approval import _ApprovalEntry, _gateway_queues

    payload = {
        "command": "redacted display only",
        "description": "sensitive operation",
        "sensitive": True,
        "expected_context": _ctx(session_key),
        **data,
    }
    entry = _ApprovalEntry(payload)
    _gateway_queues[session_key] = [entry]
    return entry


def setup_function():
    _clear_state()


def teardown_function():
    _clear_state()


def test_sensitive_entry_mints_request_id_and_freezes_expected_context():
    entry = _entry()
    request_id = entry.data["request_id"]
    assert isinstance(request_id, str)
    assert len(request_id) >= 32

    entry.data["expected_context"]["user_id"] = "mallory"
    result = entry.resolve_sensitive("once", request_id=request_id, observed_context=_ctx())

    assert result["resolved"] is True
    assert entry.result == "once"


def test_sensitive_requires_non_empty_expected_user_id():
    from tools.approval import _ApprovalEntry

    for expected_context in (
        {"platform": "telegram", "session_key": "sess-only"},
        {"platform": "telegram", "chat_id": "chat-only"},
        {"platform": "telegram", "thread_id": "thread-only"},
        {"platform": "telegram", "user_id": ""},
    ):
        try:
            _ApprovalEntry(
                {
                    "command": "redacted display only",
                    "description": "sensitive operation",
                    "sensitive": True,
                    "expected_context": expected_context,
                }
            )
        except ValueError as exc:
            assert "expected user_id" in str(exc)
        else:
            raise AssertionError(f"accepted weak context: {expected_context!r}")


def test_sensitive_cannot_resolve_by_fifo_or_resolve_all():
    from tools.approval import resolve_gateway_approval, _gateway_queues

    entry = _entry()
    session_key = "sess-sensitive"

    assert resolve_gateway_approval(session_key, "once") == 0
    assert resolve_gateway_approval(session_key, "once", resolve_all=True) == 0
    assert _gateway_queues[session_key] == [entry]
    assert not entry.event.is_set()


def test_sensitive_cannot_resolve_without_request_id():
    from tools.approval import resolve_gateway_approval, _gateway_queues

    session_key = "sess-sensitive"
    entry = _entry(session_key=session_key)

    assert (
        resolve_gateway_approval(
            session_key,
            "once",
            observed_context=_ctx(session_key),
        )
        == 0
    )
    assert _gateway_queues[session_key] == [entry]
    assert not entry.event.is_set()


def test_sensitive_exact_context_required_for_every_non_null_field():
    from tools.approval import resolve_sensitive_gateway_approval, _gateway_queues

    session_key = "sess-sensitive"
    entry = _entry(session_key=session_key)

    for field, value in {
        "platform": "slack",
        "chat_id": "chat-2",
        "user_id": "u2",
        "thread_id": "thread-2",
        "session_id": "sid-2",
        "session_key": "other-session",
    }.items():
        observed = _ctx(session_key)
        observed[field] = value
        result = resolve_sensitive_gateway_approval(
            session_key,
            "once",
            request_id=entry.data["request_id"],
            observed_context=observed,
        )
        assert result["resolved"] is False
        assert result["status"] == "context_mismatch"
        assert _gateway_queues[session_key] == [entry]
        assert not entry.event.is_set()


def test_sensitive_exact_request_resolves_once_and_replay_fails():
    from tools.approval import resolve_sensitive_gateway_approval

    session_key = "sess-sensitive"
    entry = _entry(session_key=session_key)

    first = resolve_sensitive_gateway_approval(
        session_key,
        "once",
        request_id=entry.data["request_id"],
        observed_context=_ctx(session_key),
    )
    replay = resolve_sensitive_gateway_approval(
        session_key,
        "once",
        request_id=entry.data["request_id"],
        observed_context=_ctx(session_key),
    )

    assert first["resolved"] is True
    assert first["status"] == "approved"
    assert first["approval_event_id"].startswith("ape_")
    assert first["approved_at"].endswith("Z")
    assert entry.event.is_set()
    assert entry.result == "once"
    assert replay["resolved"] is False
    assert replay["status"] == "not_found"


def test_sensitive_unsupported_adapter_identity_fails_closed():
    from tools.approval import resolve_sensitive_gateway_approval, _gateway_queues

    session_key = "sess-sensitive"
    entry = _entry(session_key=session_key)

    result = resolve_sensitive_gateway_approval(
        session_key,
        "once",
        request_id=entry.data["request_id"],
        observed_context={
            "platform": "unsupported-local",
            "session_id": "local-session",
            "session_key": session_key,
        },
    )

    assert result["resolved"] is False
    assert result["status"] == "context_mismatch"
    assert "user_id" in result["mismatched_fields"]
    assert _gateway_queues[session_key] == [entry]
    assert not entry.event.is_set()


def test_sensitive_session_and_always_choices_fail_closed():
    from tools.approval import resolve_sensitive_gateway_approval, _gateway_queues

    for choice in ("session", "always"):
        session_key = f"sess-{choice}"
        entry = _entry(session_key=session_key)

        result = resolve_sensitive_gateway_approval(
            session_key,
            choice,
            request_id=entry.data["request_id"],
            observed_context=_ctx(session_key),
        )

        assert result["resolved"] is False
        assert result["status"] == "invalid_choice"
        assert "approval_event_id" not in result
        assert "approved_at" not in result
        assert entry.event.is_set()
        assert entry.result == "deny"
        assert session_key not in _gateway_queues


def test_sensitive_ttl_caps_gateway_wait(monkeypatch):
    from tools import approval as mod

    session_key = "sess-wait-ttl"
    monkeypatch.setattr(mod, "_get_approval_config", lambda: {"timeout": 300})

    notified = []
    start = time.monotonic()
    result = mod._await_gateway_decision(
        session_key,
        lambda data: notified.append(data["request_id"]),
        {
            "command": "redacted display only",
            "description": "sensitive operation",
            "sensitive": True,
            "expected_context": _ctx(session_key),
            "ttl_seconds": 0.01,
        },
    )
    elapsed = time.monotonic() - start

    assert notified
    assert result["resolved"] is False
    assert result["choice"] is None
    assert result["reason"] is None
    assert result["approval_request_id"] == notified[0]
    assert "approval_event_id" not in result
    assert "approved_at" not in result
    assert elapsed < 1
    assert not mod.has_blocking_approval(session_key)


def test_sensitive_expired_request_fails_closed():
    from tools.approval import resolve_sensitive_gateway_approval, _gateway_queues

    session_key = "sess-expired"
    entry = _entry(session_key=session_key, ttl_seconds=-0.01)
    time.sleep(0.02)

    result = resolve_sensitive_gateway_approval(
        session_key,
        "once",
        request_id=entry.data["request_id"],
        observed_context=_ctx(session_key),
    )

    assert result["resolved"] is False
    assert result["status"] == "expired"
    assert entry.event.is_set()
    assert entry.result == "deny"
    assert session_key not in _gateway_queues


def test_sensitive_double_response_is_atomic_one_shot():
    from tools.approval import resolve_sensitive_gateway_approval

    session_key = "sess-race"
    entry = _entry(session_key=session_key)
    request_id = entry.data["request_id"]
    barrier = threading.Barrier(3)
    results = []

    def respond():
        barrier.wait()
        results.append(
            resolve_sensitive_gateway_approval(
                session_key,
                "once",
                request_id=request_id,
                observed_context=_ctx(session_key),
            )
        )

    threads = [threading.Thread(target=respond), threading.Thread(target=respond)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert len(results) == 2
    assert sum(1 for result in results if result["resolved"]) == 1
    assert sum(1 for result in results if result["status"] == "not_found") == 1
    assert entry.event.is_set()
    assert entry.result == "once"


def test_sensitive_result_logs_digest_not_raw_request_id(caplog):
    import logging
    from tools.approval import resolve_sensitive_gateway_approval

    caplog.set_level(logging.INFO, logger="tools.approval")
    session_key = "sess-logs"
    raw_request_id = "raw-sensitive-request-id-123456789"
    _entry(session_key=session_key, request_id=raw_request_id)

    resolve_sensitive_gateway_approval(
        session_key,
        "once",
        request_id=raw_request_id,
        observed_context=_ctx(session_key),
    )

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert raw_request_id not in messages
    assert "request_digest=" in messages


def test_sensitive_mismatch_and_denial_do_not_create_approval_event():
    from tools.approval import resolve_sensitive_gateway_approval

    session_key = "sess-no-event"
    entry = _entry(session_key=session_key)
    mismatch = resolve_sensitive_gateway_approval(
        session_key,
        "once",
        request_id=entry.data["request_id"],
        observed_context={**_ctx(session_key), "chat_id": "other"},
    )
    denied = resolve_sensitive_gateway_approval(
        session_key,
        "deny",
        request_id=entry.data["request_id"],
        observed_context=_ctx(session_key),
    )

    assert mismatch["status"] == "context_mismatch"
    assert "approval_event_id" not in mismatch
    assert denied["status"] == "denied"
    assert "approval_event_id" not in denied
    assert "approved_at" not in denied
