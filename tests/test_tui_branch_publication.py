import threading
import types
from typing import Any

from tui_gateway import server


def test_branch_agent_failure_reports_committed_child(monkeypatch, tmp_path):
    profile_home = tmp_path / "profiles" / "failure"
    profile_home.mkdir(parents=True)
    seen: dict[str, Any] = {"closed": 0}

    class ProfileDB:
        def __init__(self, db_path=None):
            seen["db_path"] = db_path

        def get_session_title(self, _key):
            return "Parent"

        def get_next_title_in_lineage(self, current):
            return current + " branch"

        def create_session_fork(self, **kwargs):
            seen["child"] = kwargs["child_session_id"]
            seen["parent"] = kwargs["parent_session_id"]
            seen["messages"] = list(kwargs["messages"])

        def get_session(self, key):
            return {"id": key, "cwd": str(tmp_path)}

        def update_session_cwd(self, *_args, **_kwargs):
            return None

        def close(self):
            seen["closed"] += 1

    parent = {
        "session_key": "parent-key",
        "history": [{"role": "user", "content": "keep"}],
        "history_lock": threading.Lock(),
        "running": False,
        "cols": 80,
        "profile_home": str(profile_home),
        "source": "tui",
        "agent": types.SimpleNamespace(model="test-model"),
        "created_at": 1.0,
        "last_active": 1.0,
        "cwd": str(tmp_path),
    }
    monkeypatch.setattr("hermes_state.SessionDB", ProfileDB)
    monkeypatch.setattr(
        server,
        "_make_agent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("agent failed")),
    )
    monkeypatch.setattr(server, "_session_cwd", lambda _session: str(tmp_path))
    monkeypatch.setattr(server, "_resolve_model", lambda: "test-model")

    old_sessions = dict(server._sessions)
    server._sessions.clear()
    server._sessions["parent"] = parent
    try:
        response = server.handle_request(
            {
                "id": "branch-failure",
                "method": "session.branch",
                "params": {"session_id": "parent", "name": "Child"},
            }
        )
        assert response is not None
        result = response["result"]
        assert result["committed"] is True
        assert result["stored_session_id"] == seen["child"]
        assert result.get("session_id") is None
        assert result["warning"]
        assert set(server._sessions) == {"parent"}
        assert seen["parent"] == "parent-key"
        assert seen["messages"][0]["content"] == "keep"
        assert seen["closed"] >= 2
    finally:
        server._sessions.clear()
        server._sessions.update(old_sessions)
