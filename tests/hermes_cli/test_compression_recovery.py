from __future__ import annotations

import json
import os
import time

from hermes_cli.compression_recovery import (
    inspect_stuck_compression,
    recover_stuck_compression,
)
from hermes_state import SessionDB


def _db(tmp_path):
    return SessionDB(db_path=tmp_path / "state.db")


def test_inspect_stuck_compression_reports_expired_state(tmp_path):
    db = _db(tmp_path)
    try:
        db.create_session("s1", "telegram", session_key="agent:telegram:chat")
        db.append_message("s1", "user", "hello")
        db.record_compression_failure_cooldown("s1", 900.0, "summary timed out")
        db._conn.execute(
            "INSERT INTO compression_locks "
            "(session_id, holder, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
            ("s1", "old-holder", 800.0, 850.0),
        )
        db.save_gateway_routing_entry(
            "agent:telegram:chat",
            json.dumps({
                "session_key": "agent:telegram:chat",
                "session_id": "s1",
                "last_prompt_tokens": 123456,
            }),
        )

        report = inspect_stuck_compression(db, "s1", now=1000.0)

        assert report["session_exists"] is True
        assert report["compression_lock_state"] == "expired"
        assert report["compression_failure_cooldown_state"] == "expired"
        assert report["recoverable"] is True
        assert report["messages"]["active_messages"] == 1
        assert report["gateway_routing_matches"][0]["last_prompt_tokens"] == 123456
    finally:
        db.close()


def test_recover_stuck_compression_clears_only_stale_state(tmp_path):
    db = _db(tmp_path)
    try:
        db.create_session("s1", "telegram", session_key="agent:telegram:chat")
        db.append_message("s1", "user", "hello")
        db.record_compression_failure_cooldown("s1", 900.0, "summary timed out")
        db._conn.execute(
            "INSERT INTO compression_locks "
            "(session_id, holder, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
            ("s1", "old-holder", 800.0, 850.0),
        )
        db._conn.commit()
        db.save_gateway_routing_entry(
            "agent:telegram:chat",
            json.dumps({
                "session_key": "agent:telegram:chat",
                "session_id": "s1",
                "last_prompt_tokens": 123456,
                "input_tokens": 99,
            }),
        )

        report = recover_stuck_compression(
            db,
            "s1",
            apply=True,
            backup=False,
            now=1000.0,
        )

        assert "error" not in report
        assert report["compression_locks_removed"] == 1
        assert report["cooldown_cleared"] is True
        assert report["gateway_routing_entries_reset"] == 1
        assert report["after"]["compression_lock_state"] == "missing"
        assert report["after"]["compression_failure_cooldown_state"] == "missing"
        assert db.get_compression_failure_cooldown_row("s1")["cooldown_until"] is None
        entry = json.loads(db.load_gateway_routing_entries()["agent:telegram:chat"])
        assert entry["last_prompt_tokens"] == 0
        assert entry["input_tokens"] == 99
        assert [m["content"] for m in db.get_messages("s1")] == ["hello"]
    finally:
        db.close()


def test_recover_stuck_compression_refuses_live_lock(tmp_path):
    db = _db(tmp_path)
    try:
        db.create_session("s1", "cli")
        assert db.try_acquire_compression_lock("s1", f"pid={os.getpid()}:test", ttl_seconds=60)

        report = recover_stuck_compression(
            db,
            "s1",
            apply=True,
            backup=False,
            now=time.time(),
        )

        assert report["error"] == "active compression lock is still owned; refusing recovery"
        assert db.get_compression_lock_holder("s1") is not None
    finally:
        db.close()
