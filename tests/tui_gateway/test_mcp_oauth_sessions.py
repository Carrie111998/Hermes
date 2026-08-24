from __future__ import annotations


class _Flow:
    def __init__(self):
        self.error = None

    def mark_error(self, error: str) -> None:
        self.error = error


class _Listener:
    def __init__(self):
        self.shutdown_called = False
        self.close_called = False

    def shutdown(self) -> None:
        self.shutdown_called = True

    def server_close(self) -> None:
        self.close_called = True


def test_cancel_flow_marks_error_closes_listener_and_removes_session():
    from tui_gateway import mcp_oauth_sessions

    flow = _Flow()
    listener = _Listener()
    session_id = "session-cancel"
    mcp_oauth_sessions._sessions[session_id] = {
        "session_id": session_id,
        "server_name": "linear",
        "hermes_home": "/tmp/hermes-test",
        "flow": flow,
        "httpd": listener,
        "created_at": 0,
    }

    try:
        result = mcp_oauth_sessions.cancel_flow(session_id, "linear")
    finally:
        mcp_oauth_sessions._sessions.pop(session_id, None)

    assert result == {
        "status": "error",
        "error_message": "OAuth cancelled by user",
    }
    assert flow.error == "OAuth cancelled by user"
    assert listener.shutdown_called is True
    assert listener.close_called is True
    assert session_id not in mcp_oauth_sessions._sessions


def test_cancel_flow_rejects_a_server_name_mismatch():
    from tui_gateway import mcp_oauth_sessions

    flow = _Flow()
    session_id = "session-mismatch"
    mcp_oauth_sessions._sessions[session_id] = {
        "session_id": session_id,
        "server_name": "linear",
        "hermes_home": "/tmp/hermes-test",
        "flow": flow,
        "httpd": None,
        "created_at": 0,
    }

    try:
        result = mcp_oauth_sessions.cancel_flow(session_id, "notion")
        assert session_id in mcp_oauth_sessions._sessions
    finally:
        mcp_oauth_sessions._sessions.pop(session_id, None)

    assert result == {
        "status": "error",
        "error_message": "server name mismatch for session",
    }
    assert flow.error is None
