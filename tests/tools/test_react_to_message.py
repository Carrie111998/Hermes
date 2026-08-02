from tools import react_to_message_tool as reactions


def test_owned_session_db_closes_on_early_return(monkeypatch):
    class DB:
        closed = 0

        def latest_message_row_id(self, *args, **kwargs):
            return None

        def close(self):
            self.closed += 1

    db = DB()
    monkeypatch.setattr(reactions, "get_session_env", lambda *args: "session-1")
    monkeypatch.setattr(reactions, "_open_session_db", lambda: db)

    reactions.react_to_message_tool("👍")

    assert db.closed == 1
