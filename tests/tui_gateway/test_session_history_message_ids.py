import contextlib
import threading

from tui_gateway import server


def test_session_history_projects_persisted_message_ids(monkeypatch):
    class FakeDB:
        def get_messages_as_conversation(self, session_id, **kwargs):
            assert session_id == "persisted-session"
            assert kwargs["include_ancestors"] is True
            assert kwargs["include_ids"] is True
            return [
                {
                    "id": 101,
                    "session_id": session_id,
                    "role": "user",
                    "content": "persisted prompt",
                },
                {
                    "id": 102,
                    "session_id": session_id,
                    "role": "assistant",
                    "content": "persisted answer",
                },
            ]

    @contextlib.contextmanager
    def fake_session_db(_session):
        yield FakeDB()

    sid = "history-id-test-live"
    server._sessions[sid] = {
        "session_key": "persisted-session",
        "history": [],
        "history_lock": threading.Lock(),
        "running": False,
    }
    monkeypatch.setattr(server, "_session_db", fake_session_db)

    try:
        response = server._methods["session.history"](
            "request-1", {"session_id": sid}
        )
    finally:
        server._sessions.pop(sid, None)

    assert response["result"]["messages"] == [
        {
            "db_id": 101,
            "db_session_id": "persisted-session",
            "role": "user",
            "text": "persisted prompt",
        },
        {
            "db_id": 102,
            "db_session_id": "persisted-session",
            "role": "assistant",
            "text": "persisted answer",
        },
    ]
