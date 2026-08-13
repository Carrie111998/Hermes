from __future__ import annotations

import contextlib
import threading
import types

from agent.session_contracts import SessionAuthorization
from hermes_state import SessionDB
from tui_gateway import server


def _live_session(session_key: str) -> dict:
    ready = threading.Event()
    ready.set()
    return {
        "session_key": session_key,
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "transport": None,
        "attached_images": [],
        "agent": types.SimpleNamespace(),
        "agent_ready": ready,
        "last_active": 0.0,
    }


def _request(*, revision: int = 0, content: str = "Remember OLIVE-42.") -> dict:
    return {
        "session_id": "canonical-session",
        "turn_id": "desktop-turn-1",
        "idempotency_key": "desktop-delivery-1",
        "expected_revision": revision,
        "user_event": {"role": "user", "content": content},
    }


def test_native_append_returns_running_replay_without_duplicate_user_event(
    tmp_path, monkeypatch
) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("canonical-session", "desktop")
    session = _live_session("canonical-session")
    server._sessions["live-session"] = session
    dispatched = threading.Event()
    captured = {}

    @contextlib.contextmanager
    def session_db(_session):
        yield db

    def run_prompt(_rid, _sid, _session, _text, **kwargs):
        captured.update(kwargs)
        dispatched.set()

    monkeypatch.setattr(server, "_session_db", session_db)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda _session: None)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda _session: None)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *_args: None)
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *_args: False)
    monkeypatch.setattr(server, "_start_agent_build", lambda *_args: None)
    monkeypatch.setattr(server, "_wait_agent_for_prompt", lambda *_args: None)
    monkeypatch.setattr(server, "_run_prompt_submit", run_prompt)
    monkeypatch.setattr(server, "current_transport", lambda: None)

    try:
        first = server._methods["session.turn.append.v1"]("request-1", _request())
        assert first["result"]["status"] == "streaming"
        assert first["result"]["turn"]["state"] == "running"
        assert first["result"]["turn"]["event_id"].startswith("db:")
        assert dispatched.wait(timeout=2)

        replay = server._methods["session.turn.append.v1"]("request-2", _request())
        assert replay["result"]["status"] == "running"
        assert replay["result"]["replayed"] is True

        snapshot_response = server._methods["session.snapshot.v1"](
            "snapshot-1", {"session_id": "canonical-session"}
        )
        assert snapshot_response["result"]["session_contract_version"] == 1
        assert snapshot_response["result"]["revision"] == first["result"]["turn"][
            "event_revision"
        ]
        assert snapshot_response["result"]["events"][0]["event_id"] == first[
            "result"
        ]["turn"]["event_id"]

        authorization = SessionAuthorization(
            principal="test",
            allowed_session_ids=frozenset({"canonical-session"}),
        )
        snapshot = db.read_session_snapshot(
            "canonical-session", authorization=authorization
        )
        assert [event.message["content"] for event in snapshot.events] == [
            "Remember OLIVE-42."
        ]
        assert captured["accepted_turn"].receipt.event_id == snapshot.events[0].event_id
        assert captured["turn_lease"].attempt == 1
    finally:
        server._sessions.pop("live-session", None)
        db.close()


def test_native_append_rejects_stale_revision_without_provider_dispatch(
    tmp_path, monkeypatch
) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("canonical-session", "desktop")
    db.append_message("canonical-session", "assistant", "Existing history.")
    session = _live_session("canonical-session")
    server._sessions["live-session"] = session

    @contextlib.contextmanager
    def session_db(_session):
        yield db

    monkeypatch.setattr(server, "_session_db", session_db)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda _session: None)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda _session: None)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *_args: None)
    monkeypatch.setattr(server, "current_transport", lambda: None)

    try:
        response = server._methods["session.turn.append.v1"](
            "request-stale", _request(revision=0)
        )
        assert response["error"]["code"] == 4093
        messages = db.get_messages_as_conversation("canonical-session")
        assert len(messages) == 1
        assert messages[0]["role"] == "assistant"
        assert messages[0]["content"] == "Existing history."
    finally:
        server._sessions.pop("live-session", None)
        db.close()


def test_native_append_reclaims_expired_running_turn(tmp_path, monkeypatch) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("canonical-session", "desktop")
    session = _live_session("canonical-session")
    server._sessions["live-session"] = session
    attempts = []

    @contextlib.contextmanager
    def session_db(_session):
        yield db

    def run_prompt(_rid, _sid, _session, _text, **kwargs):
        attempts.append(kwargs["turn_lease"].attempt)

    monkeypatch.setattr(server, "_session_db", session_db)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda _session: None)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda _session: None)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *_args: None)
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *_args: False)
    monkeypatch.setattr(server, "_start_agent_build", lambda *_args: None)
    monkeypatch.setattr(server, "_wait_agent_for_prompt", lambda *_args: None)
    monkeypatch.setattr(server, "_run_prompt_submit", run_prompt)
    monkeypatch.setattr(server, "current_transport", lambda: None)

    try:
        first = server._methods["session.turn.append.v1"]("request-1", _request())
        first_thread = session["_run_thread"]
        first_thread.join(timeout=2)
        assert first["result"]["turn"]["attempt"] == 1
        assert attempts == [1]

        # Model a coordinator process dying after dispatch: its in-memory flag
        # is gone and the durable lease has expired, while the command remains
        # running and its canonical user event remains intact.
        session["running"] = False
        db._conn.execute(
            "UPDATE session_turn_commands SET lease_expires_at = 0 "
            "WHERE session_id = ? AND turn_id = ?",
            ("canonical-session", "desktop-turn-1"),
        )
        recovered = server._methods["session.turn.append.v1"](
            "request-2", _request()
        )
        session["_run_thread"].join(timeout=2)

        assert recovered["result"]["status"] == "streaming"
        assert recovered["result"]["turn"]["attempt"] == 2
        assert attempts == [1, 2]
        assert len(db.get_messages_as_conversation("canonical-session")) == 1
    finally:
        server._sessions.pop("live-session", None)
        db.close()
