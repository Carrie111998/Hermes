"""TUI/Desktop approval response binding for sensitive requests."""


def _clear_state():
    from tools import approval as mod

    mod._gateway_queues.clear()
    mod._gateway_notify_cbs.clear()
    mod._session_approved.clear()
    mod._permanent_approved.clear()
    mod._pending.clear()


def setup_function():
    _clear_state()


def teardown_function():
    _clear_state()


def test_approval_respond_with_request_id_preserves_local_plumbing(monkeypatch):
    from tui_gateway import server

    sid = "ui-session-1"
    server._sessions.clear()
    server._sessions[sid] = {"session_key": "stored-session-1"}

    captured = {}

    def fake_resolve(session_key, choice, *, request_id, observed_context, reason=None):
        captured.update(
            {
                "choice": choice,
                "observed_context": observed_context,
                "reason": reason,
                "request_id": request_id,
                "session_key": session_key,
            }
        )
        return {"resolved": True, "status": "approved", "request_id": request_id}

    monkeypatch.setattr(
        "tools.approval.resolve_sensitive_gateway_approval",
        fake_resolve,
    )
    monkeypatch.setattr(server, "_start_agent_build", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_wait_agent", lambda *_args, **_kwargs: None)

    response = server._methods["approval.respond"](
        1,
        {"choice": "once", "request_id": "req-local", "session_id": sid},
    )

    assert response["result"]["resolved"] is True
    assert captured == {
        "choice": "once",
        "observed_context": {
            "platform": "tui",
            "session_id": sid,
            "session_key": "stored-session-1",
        },
        "reason": None,
        "request_id": "req-local",
        "session_key": "stored-session-1",
    }


def test_local_json_rpc_cannot_resolve_sensitive_user_bound_approval(monkeypatch):
    from tools.approval import _ApprovalEntry, _gateway_queues
    from tui_gateway import server

    sid = "ui-session-1"
    session_key = "stored-session-1"
    request_id = "req-sensitive-local"
    server._sessions.clear()
    server._sessions[sid] = {"session_key": session_key}

    entry = _ApprovalEntry(
        {
            "command": "redacted display only",
            "description": "DPS-class write",
            "expected_context": {
                "platform": "telegram",
                "chat_id": "chat-1",
                "user_id": "telegram-user-1",
                "thread_id": "thread-1",
                "session_key": session_key,
            },
            "request_id": request_id,
            "sensitive": True,
        }
    )
    _gateway_queues[session_key] = [entry]

    monkeypatch.setattr(server, "_start_agent_build", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_wait_agent", lambda *_args, **_kwargs: None)

    response = server._methods["approval.respond"](
        1,
        {"choice": "once", "request_id": request_id, "session_id": sid},
    )

    result = response["result"]
    assert result["resolved"] is False
    assert result["status"] == "context_mismatch"
    assert "user_id" in result["mismatched_fields"]
    assert not entry.event.is_set()
    assert _gateway_queues[session_key] == [entry]


def test_local_json_rpc_preserves_ordinary_non_sensitive_approvals(monkeypatch):
    from tools.approval import _ApprovalEntry, _gateway_queues
    from tui_gateway import server

    sid = "ui-session-ordinary"
    session_key = "stored-session-ordinary"
    server._sessions.clear()
    server._sessions[sid] = {"session_key": session_key}
    entry = _ApprovalEntry({"command": "ordinary dangerous command"})
    _gateway_queues[session_key] = [entry]

    monkeypatch.setattr(server, "_start_agent_build", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_wait_agent", lambda *_args, **_kwargs: None)

    response = server._methods["approval.respond"](
        1,
        {"choice": "once", "session_id": sid},
    )

    assert response["result"] == {"resolved": 1}
    assert entry.event.is_set()
    assert entry.result == "once"
