"""Bot Mode canonical-session rollover invariants.

The exact title ``Bot Chat`` is the canonical registry inside one profile's
state.db.  Rollover transfers that title to a fresh, unrelated row in one
transaction and preserves the retired lineage as readable history.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_state import SessionDB, SessionTurnLeaseLostError
import tui_gateway.server as srv
from tui_gateway.turn_marker import read_turn_marker, record_turn_start


BOT_TITLE = "Bot Chat"


def _seed_bot_session(
    db: SessionDB,
    sid: str,
    *,
    title: str = BOT_TITLE,
    parent_session_id: str | None = None,
    source: str = "desktop",
    model: str = "openai:test-model",
    cwd: str = "/tmp/bot-workspace",
    profile_name: str = "ops",
    with_messages: bool = True,
) -> None:
    db.create_session(
        sid,
        source=source,
        model=model,
        model_config={
            "model": model,
            "provider": "openai",
            "enabled_toolsets": ["web", "memory"],
            "reasoning_config": {"effort": "high"},
            "service_tier": "priority",
            "durable_memory": {"enabled": True},
        },
        system_prompt="You are the same durable bot.",
        cwd=cwd,
        profile_name=profile_name,
        git_repo_root="/tmp/bot-workspace",
        parent_session_id=parent_session_id,
    )
    if with_messages:
        db.append_message(sid, "user", f"old question in {sid}")
        db.append_message(sid, "assistant", f"old answer in {sid}")
    assert db.set_session_title(sid, title)
    db.set_session_hidden(sid, True)


def _row(db: SessionDB, sid: str) -> dict:
    row = db.get_session(sid)
    assert row is not None
    return row


def _messages(db: SessionDB, sid: str) -> list[dict]:
    return db.get_messages_as_conversation(sid)


def _turn_lease_rows(db: SessionDB) -> list[dict]:
    with db._lock:
        return [dict(row) for row in db._conn.execute("SELECT * FROM session_turn_leases")]


def test_rollover_creates_unrelated_empty_canonical_row_and_preserves_bot_identity(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    _seed_bot_session(db, "old-chat")
    with db._lock:
        db._conn.execute(
            "UPDATE sessions SET git_branch = 'main', last_activity_at = ?, "
            "last_activity_description = 'compressing', last_activity_provenance = 'tool', "
            "compression_failure_cooldown_until = ?, compression_failure_error = 'old', "
            "compression_fallback_streak = 3, compression_ineffective_count = 2 "
            "WHERE id = 'old-chat'",
            (time.time(), time.time() + 100),
        )

    result = db.rollover_bot_session(
        new_session_id="new-chat",
        expected_current_session_id="old-chat",
    )

    assert result["created"] is True
    assert result["previous_session_id"] == "old-chat"
    assert result["previous_resolved_id"] == "old-chat"
    assert result["current_session_id"] == "new-chat"

    old = _row(db, "old-chat")
    new = _row(db, "new-chat")
    assert old["end_reason"] == "bot_rollover"
    assert old["ended_at"] is not None
    assert old["title"].startswith("Bot Chat (retired ")
    assert old["hidden"] == 0, "retired history is visible/readable"
    assert len(_messages(db, "old-chat")) == 2

    assert new["title"] == BOT_TITLE
    assert new["hidden"] == 1, "only the current Bot Chat stays plugin-owned"
    assert new["title_source"] == SessionDB.TITLE_SOURCE_USER
    assert db.set_auto_title(
        "new-chat", "First-turn generated title", source=SessionDB.TITLE_SOURCE_LLM
    ) is False
    assert _row(db, "new-chat")["title"] == BOT_TITLE
    assert new["parent_session_id"] is None
    assert _messages(db, "new-chat") == []
    assert new["message_count"] == 0
    assert new["source"] == old["source"]
    assert new["model"] == old["model"]
    model_config = json.loads(new["model_config"])
    assert model_config["enabled_toolsets"] == ["web", "memory"]
    assert model_config["durable_memory"] == {"enabled": True}
    assert new["system_prompt"] == "You are the same durable bot."
    assert new["cwd"] == old["cwd"]
    assert new["profile_name"] == old["profile_name"]
    assert new["git_repo_root"] == old["git_repo_root"]
    assert new["git_branch"] == old["git_branch"]

    for field in (
        "user_id",
        "session_key",
        "chat_id",
        "chat_type",
        "thread_id",
        "display_name",
        "origin_json",
        "last_activity_at",
        "last_activity_description",
        "last_activity_provenance",
        "compression_failure_cooldown_until",
        "compression_failure_error",
    ):
        assert new[field] is None
    assert new["compression_fallback_streak"] == 0
    assert new["compression_ineffective_count"] == 0


@pytest.mark.parametrize("expected", ["root-chat", "tip-chat"])
def test_rollover_accepts_compressed_root_or_tip_and_retires_logical_lineage(tmp_path, expected):
    db = SessionDB(tmp_path / "state.db")
    _seed_bot_session(db, "root-chat")
    db.end_session("root-chat", "compression")
    _seed_bot_session(
        db,
        "tip-chat",
        title="Bot Chat (continued)",
        parent_session_id="root-chat",
    )

    result = db.rollover_bot_session(
        new_session_id="new-chat",
        expected_current_session_id=expected,
    )

    assert result["previous_session_id"] == "root-chat"
    assert result["previous_resolved_id"] == "tip-chat"
    assert _row(db, "root-chat")["end_reason"] == "compression"
    assert _row(db, "tip-chat")["end_reason"] == "bot_rollover"
    assert _row(db, "root-chat")["hidden"] == 0
    assert _row(db, "tip-chat")["hidden"] == 0
    assert _row(db, "new-chat")["parent_session_id"] is None
    assert _row(db, "new-chat")["title"] == BOT_TITLE
    assert db.resolve_resume_session_id("root-chat") == "tip-chat"


@pytest.mark.parametrize("expected", ["root-chat", "live-child"])
def test_rollover_uses_canonical_tip_when_stale_closed_sibling_exists(tmp_path, monkeypatch, expected):
    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(srv, "_get_db", lambda: db)
    _seed_bot_session(db, "root-chat")
    db.end_session("root-chat", "compression")
    _seed_bot_session(
        db,
        "stale-child",
        title="Bot Chat (stale continuation)",
        parent_session_id="root-chat",
    )
    db.end_session("stale-child", "ws_orphan_reap")
    _seed_bot_session(
        db,
        "live-child",
        title="Bot Chat (live continuation)",
        parent_session_id="root-chat",
    )

    with db._lock:
        db._conn.execute(
            "UPDATE sessions SET started_at = ? WHERE id = 'stale-child'",
            (time.time() - 100,),
        )
        db._conn.execute(
            "UPDATE sessions SET started_at = ? WHERE id = 'live-child'",
            (time.time(),),
        )
        db._conn.commit()

    assert db.resolve_resume_session_id("root-chat") == "live-child"

    result = db.rollover_bot_session(
        new_session_id="new-chat",
        expected_current_session_id=expected,
    )

    assert result["created"] is True
    assert result["previous_session_id"] == "root-chat"
    assert result["previous_resolved_id"] == "live-child"
    assert _row(db, "live-child")["end_reason"] == "bot_rollover"
    assert _row(db, "stale-child")["end_reason"] == "ws_orphan_reap"
    assert _row(db, "new-chat")["title"] == BOT_TITLE

    listed = srv._methods["session.bot_history"]("list", {})
    assert [row["id"] for row in listed["result"]["sessions"]] == ["root-chat"]
    assert listed["result"]["sessions"][0]["resolved_id"] == "live-child"
    opened = srv._methods["session.bot_history"](
        "open", {"session_id": "root-chat"}
    )
    assert "error" not in opened, opened
    assert opened["result"]["session"]["resolved_id"] == "live-child"


def test_active_lock_or_turn_lease_probes_without_mutation_then_force_fences_both(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    _seed_bot_session(db, "old-chat")
    compression_holder = f"pid={os.getpid()}:compress"
    turn_holder = f"pid={os.getpid()}:turn"
    assert db.try_acquire_compression_lock("old-chat", compression_holder, ttl_seconds=60)
    assert db.try_acquire_session_turn_lease("old-chat", turn_holder, ttl_seconds=60)

    probe = db.rollover_bot_session(
        new_session_id="new-chat",
        expected_current_session_id="old-chat",
        force=False,
    )
    assert probe["confirmation_required"] is True
    assert set(probe["active_reasons"]) == {"compression", "turn"}
    assert _row(db, "old-chat")["title"] == BOT_TITLE
    assert db.get_session("new-chat") is None

    result = db.rollover_bot_session(
        new_session_id="new-chat",
        expected_current_session_id="old-chat",
        force=True,
    )
    assert result["created"] is True
    assert db.get_compression_lock_holder("old-chat") is None
    assert _turn_lease_rows(db) == []
    assert not db.refresh_session_turn_lease("old-chat", turn_holder, ttl_seconds=60)
    with pytest.raises(SessionTurnLeaseLostError):
        db.append_message(
            "old-chat",
            "assistant",
            "late result from the interrupted turn",
            turn_lease_holder=turn_holder,
        )
    assert _messages(db, "new-chat") == []


def test_stale_compression_lock_and_turn_lease_clear_without_confirmation(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    _seed_bot_session(db, "old-chat")
    now = time.time()
    with db._lock:
        db._conn.execute(
            "INSERT INTO compression_locks(session_id, holder, acquired_at, expires_at) "
            "VALUES ('old-chat', 'pid=999999999:dead', ?, ?)",
            (now - 10, now - 1),
        )
        db._conn.execute(
            "INSERT INTO session_turn_leases(conversation_id, holder, acquired_at, expires_at) "
            "VALUES ('old-chat', 'pid=999999999:dead', ?, ?)",
            (now - 10, now - 1),
        )

    result = db.rollover_bot_session(
        new_session_id="new-chat",
        expected_current_session_id="old-chat",
    )
    assert result["created"] is True
    assert result["confirmation_required"] is False
    assert db.get_compression_lock_holder("old-chat") is None
    assert _turn_lease_rows(db) == []


def test_concurrent_rollovers_have_one_winner_and_one_canonical_row(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(path)
    _seed_bot_session(db, "old-chat")
    db.close()
    barrier = threading.Barrier(2)

    def rollover(sid: str) -> dict:
        local = SessionDB(path)
        try:
            barrier.wait()
            return local.rollover_bot_session(
                new_session_id=sid,
                expected_current_session_id="old-chat",
            )
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(rollover, ["new-a", "new-b"]))

    winners = [row for row in results if row["created"]]
    followers = [row for row in results if not row["created"]]
    assert len(winners) == 1
    assert len(followers) == 1
    assert followers[0]["current_session_id"] == winners[0]["current_session_id"]

    check = SessionDB(path)
    try:
        canonical = check.get_session_by_title(BOT_TITLE)
        assert canonical["id"] == winners[0]["current_session_id"]
        assert sum(bool(check.get_session(sid)) for sid in ("new-a", "new-b")) == 1
    finally:
        check.close()


def test_insert_failure_rolls_back_title_retirement_locks_and_new_row(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    _seed_bot_session(db, "old-chat")
    holder = f"pid={os.getpid()}:compress"
    assert db.try_acquire_compression_lock("old-chat", holder, ttl_seconds=60)
    with db._lock:
        db._conn.execute(
            "CREATE TRIGGER fail_rollover BEFORE INSERT ON sessions "
            "WHEN NEW.id = 'new-chat' BEGIN SELECT RAISE(ABORT, 'synthetic rollover failure'); END"
        )

    with pytest.raises(sqlite3.DatabaseError, match="synthetic rollover failure"):
        db.rollover_bot_session(
            new_session_id="new-chat",
            expected_current_session_id="old-chat",
            force=True,
        )

    assert _row(db, "old-chat")["title"] == BOT_TITLE
    assert _row(db, "old-chat")["end_reason"] is None
    assert _row(db, "old-chat")["hidden"] == 1
    assert db.get_session("new-chat") is None
    assert db.get_compression_lock_holder("old-chat") == holder


def test_rpc_probe_interrupts_only_exact_live_owner_on_force_and_returns_history(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    _seed_bot_session(db, "old-chat")
    monkeypatch.setattr(srv, "_get_db", lambda: db)
    old_agent = type("Agent", (), {"session_id": "old-chat"})()
    other_agent = type("Agent", (), {"session_id": "other-chat"})()
    old_live = {
        "agent": old_agent,
        "history": [{"role": "user", "content": "working"}],
        "history_lock": threading.Lock(),
        "running": True,
        "session_key": "old-chat",
        "profile_home": str(tmp_path),
    }
    other_live = {
        "agent": other_agent,
        "history": [],
        "history_lock": threading.Lock(),
        "running": True,
        "session_key": "other-chat",
    }
    monkeypatch.setattr(srv, "_sessions", {"ui-old": old_live, "ui-other": other_live})
    interrupted: list[str] = []
    monkeypatch.setattr(
        srv,
        "_interrupt_session_turn",
        lambda sid, session, **_kwargs: interrupted.append(sid) or False,
    )

    probe = srv._methods["session.bot_rollover"](
        "probe",
        {"expected_current_session_id": "old-chat", "new_session_id": "new-chat"},
    )
    assert probe["result"]["confirmation_required"] is True
    assert interrupted == []
    assert db.get_session("new-chat") is None

    forced = srv._methods["session.bot_rollover"](
        "force",
        {
            "expected_current_session_id": "old-chat",
            "new_session_id": "new-chat",
            "force": True,
        },
    )
    assert "error" not in forced, forced
    result = forced["result"]
    assert interrupted == ["ui-old"]
    assert other_live.get("_bot_rollover") is None
    assert old_live["_bot_rollover"] is True
    assert result["current_session"]["id"] == "new-chat"
    assert result["current_session"]["title"] == BOT_TITLE
    assert result["current_session"]["message_count"] == 0
    assert result["previous_session"]["id"] == "old-chat"
    assert result["previous_session"]["message_count"] == 2


def test_rpc_concurrent_follower_does_not_close_the_winning_new_runtime(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    _seed_bot_session(db, "old-chat")
    db.rollover_bot_session(
        new_session_id="winner-chat",
        expected_current_session_id="old-chat",
    )
    monkeypatch.setattr(srv, "_get_db", lambda: db)
    winner_live = {
        "agent": type("Agent", (), {"session_id": "winner-chat"})(),
        "history": [],
        "history_lock": threading.Lock(),
        "running": False,
        "session_key": "winner-chat",
    }
    monkeypatch.setattr(srv, "_sessions", {"ui-winner": winner_live})

    follower = srv._methods["session.bot_rollover"](
        "follower",
        {
            "expected_current_session_id": "old-chat",
            "new_session_id": "loser-chat",
        },
    )

    assert "error" not in follower, follower
    assert follower["result"]["created"] is False
    assert follower["result"]["current_session_id"] == "winner-chat"
    assert winner_live.get("_closing") is None
    assert winner_live.get("_bot_rollover") is None
    assert db.get_session("loser-chat") is None


def test_rpc_force_db_failure_leaves_live_runtime_and_canonical_row_untouched(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    _seed_bot_session(db, "old-chat")
    with db._lock:
        db._conn.execute(
            "CREATE TRIGGER fail_rpc_rollover BEFORE INSERT ON sessions "
            "WHEN NEW.id = 'new-chat' BEGIN SELECT RAISE(ABORT, 'rpc insert failure'); END"
        )
    monkeypatch.setattr(srv, "_get_db", lambda: db)
    live = {
        "agent": type("Agent", (), {"session_id": "old-chat"})(),
        "history": [],
        "history_lock": threading.Lock(),
        "running": True,
        "session_key": "old-chat",
    }
    monkeypatch.setattr(srv, "_sessions", {"ui-old": live})
    interrupted: list[str] = []
    monkeypatch.setattr(
        srv,
        "_interrupt_session_turn",
        lambda sid, _session, **_kwargs: interrupted.append(sid) or False,
    )

    failed = srv._methods["session.bot_rollover"](
        "force-failure",
        {
            "expected_current_session_id": "old-chat",
            "new_session_id": "new-chat",
            "force": True,
        },
    )

    assert failed["error"]["code"] == 5006
    assert interrupted == []
    assert live["running"] is True
    assert live.get("_closing") is None
    assert live.get("_bot_rollover") is None
    assert _row(db, "old-chat")["title"] == BOT_TITLE
    assert _row(db, "old-chat")["end_reason"] is None
    assert db.get_session("new-chat") is None


def test_retired_history_lists_normally_while_relaunch_registry_resolves_only_new(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(tmp_path / "state.db")
    _seed_bot_session(db, "old-chat")
    monkeypatch.setattr(srv, "_get_db", lambda: db)

    response = srv._methods["session.bot_rollover"](
        "roll",
        {"expected_current_session_id": "old-chat", "new_session_id": "new-chat"},
    )
    assert "error" not in response, response

    canonical = srv._methods["session.list"](
        "canonical", {"title": BOT_TITLE, "include_hidden": True}
    )["result"]["sessions"]
    assert [row["id"] for row in canonical] == ["new-chat"]

    visible = srv._methods["session.list"]("history", {})["result"]["sessions"]
    assert any(row["id"] == "old-chat" for row in visible)
    assert all(row["id"] != "new-chat" for row in visible)

    roster = srv._methods["profiles.list"]("roster", {})["result"]["profiles"]
    default = next(row for row in roster if row["name"] == "default")
    assert default["canonical_session"]["id"] == "new-chat"
    assert default["canonical_session"]["resolved_id"] == "new-chat"


def test_retired_turn_marker_cannot_auto_continue_after_relaunch(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    _seed_bot_session(db, "old-chat")
    db.rollover_bot_session(
        new_session_id="new-chat",
        expected_current_session_id="old-chat",
    )
    record_turn_start(tmp_path, "old-chat", "work that must stay retired")
    assert read_turn_marker(tmp_path, "old-chat") is not None

    session = {
        "agent": None,
        "history": [],
        "history_lock": threading.Lock(),
        "profile_home": str(tmp_path),
        "running": False,
        "session_key": "old-chat",
    }
    result = srv._maybe_schedule_auto_continue("ui-old", session, "old-chat")

    assert result is None
    assert read_turn_marker(tmp_path, "old-chat") is None
    assert read_turn_marker(tmp_path, "new-chat") is None


def test_compressed_retired_root_marker_cannot_schedule_auto_continue(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    _seed_bot_session(db, "root-chat")
    db.end_session("root-chat", "compression")
    _seed_bot_session(
        db,
        "tip-chat",
        title="Bot Chat (continued)",
        parent_session_id="root-chat",
    )
    db.rollover_bot_session(
        new_session_id="new-chat",
        expected_current_session_id="tip-chat",
    )
    record_turn_start(tmp_path, "root-chat", "work that must stay retired")

    scheduled = []

    class InertThread:
        def __init__(self, *, target, daemon):
            self.target = target
            self.daemon = daemon

        def start(self):
            scheduled.append(True)

    monkeypatch.setattr(srv.threading, "Thread", InertThread)
    session = {
        "agent": None,
        "history": [],
        "history_lock": threading.Lock(),
        "profile_home": str(tmp_path),
        "running": False,
        "session_key": "root-chat",
    }

    result = srv._maybe_schedule_auto_continue("ui-old", session, "root-chat")

    assert result is None
    assert scheduled == []
    assert read_turn_marker(tmp_path, "root-chat") is None


def test_bot_history_rpc_lists_and_reads_only_retired_bot_lineages(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    _seed_bot_session(db, "old-chat")
    _seed_bot_session(db, "ordinary", title="Ordinary session")
    monkeypatch.setattr(srv, "_get_db", lambda: db)
    db.rollover_bot_session(
        new_session_id="new-chat",
        expected_current_session_id="old-chat",
    )

    listed = srv._methods["session.bot_history"]("list", {})
    assert "error" not in listed, listed
    assert [row["id"] for row in listed["result"]["sessions"]] == ["old-chat"]

    opened = srv._methods["session.bot_history"](
        "open", {"session_id": "old-chat"}
    )
    assert "error" not in opened, opened
    assert opened["result"]["session"]["id"] == "old-chat"
    assert [message["role"] for message in opened["result"]["messages"]] == [
        "user",
        "assistant",
    ]

    current = srv._methods["session.bot_history"](
        "current", {"session_id": "new-chat"}
    )
    assert current["error"]["code"] == 4007


def test_bot_history_lists_and_opens_compressed_retired_root(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    _seed_bot_session(db, "root-chat")
    db.end_session("root-chat", "compression")
    _seed_bot_session(
        db,
        "tip-chat",
        title="Bot Chat (continued)",
        parent_session_id="root-chat",
    )
    monkeypatch.setattr(srv, "_get_db", lambda: db)
    db.rollover_bot_session(
        new_session_id="new-chat",
        expected_current_session_id="tip-chat",
    )

    listed = srv._methods["session.bot_history"]("list", {})

    assert "error" not in listed, listed
    assert [row["id"] for row in listed["result"]["sessions"]] == ["root-chat"]
    assert listed["result"]["sessions"][0]["resolved_id"] == "tip-chat"

    opened = srv._methods["session.bot_history"](
        "open", {"session_id": listed["result"]["sessions"][0]["id"]}
    )
    assert "error" not in opened, opened
    assert opened["result"]["session"]["id"] == "root-chat"
    assert opened["result"]["session"]["resolved_id"] == "tip-chat"
    assert [message["role"] for message in opened["result"]["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]


def test_rollover_does_not_touch_other_profile_database(tmp_path):
    default = SessionDB(tmp_path / "default.db")
    other = SessionDB(tmp_path / "other.db")
    _seed_bot_session(default, "default-old", profile_name="default")
    _seed_bot_session(other, "other-old", profile_name="other")

    default.rollover_bot_session(
        new_session_id="default-new",
        expected_current_session_id="default-old",
    )

    assert _row(other, "other-old")["title"] == BOT_TITLE
    assert _row(other, "other-old")["end_reason"] is None
    assert other.get_session("default-new") is None
