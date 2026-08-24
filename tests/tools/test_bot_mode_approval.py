import threading
import time
from unittest.mock import patch


def test_query_file_bot_mode_approval_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools.bot_mode_approval import (
        get_pending_bot_mode_approval,
        request_bot_mode_approval,
        resolve_bot_mode_approval,
    )

    result = []
    worker = threading.Thread(
        target=lambda: result.append(
            request_bot_mode_approval(
                session_key="bot-chat-session",
                command="write: /other-profile/SOUL.md",
                description="Protected instruction file write",
                choices=["once", "deny"],
                timeout=2.0,
            )
        ),
        daemon=True,
    )
    worker.start()

    pending = None
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        pending = get_pending_bot_mode_approval("bot-chat-session")
        if pending is not None:
            break
        time.sleep(0.01)

    assert pending is not None
    assert pending["command"] == "write: /other-profile/SOUL.md"
    assert pending["choices"] == ["once", "deny"]
    assert resolve_bot_mode_approval(
        "bot-chat-session", "once", request_id=pending["request_id"]
    )

    worker.join(timeout=1.0)
    assert not worker.is_alive()
    assert result == ["once"]
    assert get_pending_bot_mode_approval("bot-chat-session") is None


def test_bridge_rejects_unoffered_choice_and_invalid_request_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools.bot_mode_approval import (
        get_pending_bot_mode_approval,
        request_bot_mode_approval,
        resolve_bot_mode_approval,
    )

    result = []
    worker = threading.Thread(
        target=lambda: result.append(
            request_bot_mode_approval(
                session_key="bot-chat-session",
                command="write: /other-profile/SOUL.md",
                description="Protected instruction file write",
                choices=["once", "deny"],
                timeout=2.0,
            )
        ),
        daemon=True,
    )
    worker.start()

    pending = None
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        pending = get_pending_bot_mode_approval("bot-chat-session")
        if pending is not None:
            break
        time.sleep(0.01)

    assert pending is not None
    request_id = pending["request_id"]
    assert not resolve_bot_mode_approval(
        "bot-chat-session", "session", request_id=request_id
    )
    assert not resolve_bot_mode_approval(
        "bot-chat-session", "once", request_id="../outside"
    )
    assert resolve_bot_mode_approval(
        "bot-chat-session", "deny", request_id=request_id
    )

    worker.join(timeout=1.0)
    assert not worker.is_alive()
    assert result == ["deny"]


def test_installed_callback_routes_one_operation_approval(monkeypatch):
    monkeypatch.setenv("HERMES_BOT_MODE_QUERY_FILE", "1")

    import tools.bot_mode_approval as bridge
    from tools.terminal_tool import _get_approval_callback, set_approval_callback

    seen = {}

    def fake_request(**kwargs):
        seen.update(kwargs)
        return "once"

    class FakeCli:
        session_id = "bot-chat-session"

        @staticmethod
        def _approval_choices(command, **kwargs):
            assert kwargs["allow_session"] is False
            return ["once", "deny"]

    monkeypatch.setattr(bridge, "request_bot_mode_approval", fake_request)
    try:
        assert bridge.install_bot_mode_approval_callback(FakeCli(), timeout=60.0)
        callback = _get_approval_callback()
        assert callback is not None
        assert callback(
            "write: /other-profile/SOUL.md",
            "Protected instruction file write",
            allow_permanent=False,
            allow_session=False,
        ) == "once"
    finally:
        set_approval_callback(None)

    assert seen == {
        "session_key": "bot-chat-session",
        "command": "write: /other-profile/SOUL.md",
        "description": "Protected instruction file write",
        "choices": ["once", "deny"],
        "timeout": 60.0,
    }


def test_installed_callback_timeout_is_a_downstream_denial(monkeypatch):
    monkeypatch.setenv("HERMES_BOT_MODE_QUERY_FILE", "1")
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")

    import tools.bot_mode_approval as bridge
    from tools import approval
    from tools.terminal_tool import set_approval_callback

    class FakeCli:
        session_id = "bot-chat-session"

        @staticmethod
        def _approval_choices(command, **kwargs):
            return ["once", "deny"]

    monkeypatch.setattr(bridge, "request_bot_mode_approval", lambda **kwargs: "timeout")
    approval._session_approved.clear()
    approval._permanent_approved.clear()

    try:
        assert bridge.install_bot_mode_approval_callback(FakeCli(), timeout=0.01)

        with patch("hermes_cli.config.load_config_readonly", return_value={"approvals": {"mode": "manual"}}):
            result = approval.check_all_command_guards("rm -rf /var/data", "local")
    finally:
        set_approval_callback(None)

    assert result["approved"] is False
    assert result["outcome"] == "timeout"
    assert result["user_consent"] is False
    assert "Silence is not consent" in result["message"]


def test_resolution_lock_allows_only_one_writer(tmp_path):
    from tools.bot_mode_approval import _resolution_lock

    record = tmp_path / "approval.json"

    with _resolution_lock(record) as first:
        assert first

        with _resolution_lock(record) as second:
            assert not second

    with _resolution_lock(record) as after_release:
        assert after_release


def test_desktop_gateway_surfaces_and_resolves_query_file_approval(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools.bot_mode_approval import request_bot_mode_approval
    from tui_gateway import server

    result = []
    worker = threading.Thread(
        target=lambda: result.append(
            request_bot_mode_approval(
                session_key="bot-chat-session",
                command="write: /other-profile/SOUL.md",
                description="Protected instruction file write",
                choices=["once", "deny"],
                timeout=2.0,
            )
        ),
        daemon=True,
    )
    worker.start()

    payload = None
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        payload = server._pending_approval_request_payload("bot-chat-session")
        if payload is not None:
            break
        time.sleep(0.01)

    assert payload is not None
    assert payload["choices"] == ["once", "deny"]
    assert payload["command"] == "write: /other-profile/SOUL.md"

    emitted = []
    monkeypatch.setattr(
        server,
        "_emit",
        lambda event, sid, data=None: emitted.append((event, sid, data)),
    )
    seen_request_ids = set()
    server._poll_bot_mode_approval(
        "live-bot-chat",
        {"session_key": "bot-chat-session"},
        seen_request_ids,
    )
    server._poll_bot_mode_approval(
        "live-bot-chat",
        {"session_key": "bot-chat-session"},
        seen_request_ids,
    )
    assert emitted == [("approval.request", "live-bot-chat", payload)]

    server._sessions["live-bot-chat"] = {"session_key": "bot-chat-session"}
    try:
        response = server.handle_request(
            {
                "id": "approval-1",
                "method": "approval.respond",
                "params": {
                    "session_id": "live-bot-chat",
                    "request_id": payload["request_id"],
                    "choice": "once",
                },
            }
        )
    finally:
        server._sessions.pop("live-bot-chat", None)

    assert response is not None
    assert type(response["result"]["resolved"]) is int
    assert response["result"]["resolved"] == 1
    worker.join(timeout=1.0)
    assert not worker.is_alive()
    assert result == ["once"]
