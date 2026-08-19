"""Regression tests for TUI incognito session persistence isolation."""

import hashlib
import io
from unittest.mock import Mock

import hermes_state
from hermes_state import SessionDB
from tui_gateway.compute_host import ComputeHost
from tui_gateway import server


def _db_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_tui_incognito_session_row_is_not_created(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    db_path = home / "state.db"

    db = SessionDB(db_path=db_path)
    db.create_session("existing-session", source="tui", model="test-model")
    db.append_message("existing-session", role="user", content="baseline")
    db.close()
    before = _db_hash(db_path)

    session = {
        "incognito": True,
        "session_key": "incognito-session",
        "profile_home": str(home),
        "model_override": None,
    }
    server._ensure_session_db_row(session)

    after = _db_hash(db_path)
    db = SessionDB(db_path=db_path)
    try:
        assert db.get_session("incognito-session") is None
        assert db.get_session("existing-session") is not None
    finally:
        db.close()
    assert after == before


def test_tui_incognito_full_session_lifecycle_leaves_no_artifacts(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    db_path = home / "state.db"
    sessions_dir = home / "sessions"

    db = SessionDB(db_path=db_path)
    db.create_session("existing-session", source="tui", model="test-model")
    db.append_message("existing-session", role="user", content="baseline")
    db.close()
    before = _db_hash(db_path)

    agent = Mock()
    agent.incognito = True
    agent.session_id = "incognito-session"
    agent._session_messages = [{"role": "user", "content": "temporary"}]

    server._init_session(
        "live-incognito-sid",
        "incognito-session",
        agent,
        [],
        profile_home=str(home),
        incognito=True,
    )
    session = server._sessions["live-incognito-sid"]
    assert session["incognito"] is True

    # This is the real first-turn persistence sequence used by prompt.submit.
    session["history"].append({"role": "user", "content": "temporary"})
    server._ensure_session_db_row(session)
    server._persist_branch_seed(session)

    closed = server._methods["session.close"](
        "close-incognito", {"session_id": "live-incognito-sid"}
    )
    assert closed["result"]["closed"] is True
    assert agent._persist_session.call_count == 0
    assert agent.commit_memory_session.call_count == 0

    after = _db_hash(db_path)
    db = SessionDB(db_path=db_path)
    try:
        assert db.get_session("incognito-session") is None
        assert db.get_session("existing-session") is not None
    finally:
        db.close()
    assert after == before
    assert not (sessions_dir / "incognito-session.json").exists()
    assert not (sessions_dir / "incognito-session.jsonl").exists()


def test_tui_session_create_stores_explicit_incognito_flag(monkeypatch):
    monkeypatch.setenv("HERMES_INCOGNITO", "1")
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)

    created = server._methods["session.create"]("create-incognito", {"cols": 80})
    sid = created["result"]["session_id"]
    try:
        assert server._sessions[sid]["incognito"] is True
    finally:
        server._sessions.pop(sid, None)


def test_compute_host_incognito_session_does_not_open_session_db(monkeypatch, tmp_path):
    agent = Mock(incognito=True)
    monkeypatch.setattr(server, "_make_agent", lambda *_a, **_k: agent)
    monkeypatch.setattr(server, "_transfer_db_to_agent", lambda *_a, **_k: False)

    def fail_if_opened(*_args, **_kwargs):
        raise AssertionError("incognito compute host opened SessionDB")

    monkeypatch.setattr(hermes_state, "SessionDB", fail_if_opened)
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    try:
        session = host._ensure_server_session(
            server,
            {
                "sid": "compute-incognito-sid",
                "session_key": "compute-incognito-session",
                "profile_home": str(tmp_path / "profile"),
                "incognito": True,
            },
        )
        assert session["incognito"] is True
    finally:
        server._sessions.pop("compute-incognito-sid", None)
        host.close()
