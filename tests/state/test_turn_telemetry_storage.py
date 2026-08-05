"""RED regressions for durable, bounded, read-only turn telemetry storage."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
import time
import uuid

import pytest

from hermes_state import SessionDB
from hermes_state_common import SCHEMA_VERSION


SENSITIVE_VALUE = uuid.uuid4().hex
BASE_TIME = time.time()


def _row(index: int, **overrides):
    payload = {
        "event_type": "turn_terminal",
        "turn_id": f"turn-{index}",
        "correlation_id": f"correlation-{index}",
        "session_id": "session-1",
        "parent_session_id": "",
        "parent_turn_id": "",
        "profile_name": "forge",
        "requested_profile": "forge",
        "effective_profile": "forge",
        "source": "cli",
        "platform": "cli",
        "task_class": "interactive",
        "route_type": "primary",
        "disposition": "completed",
        "is_delegated": False,
        "started_at": BASE_TIME + float(index),
        "ended_at": BASE_TIME + float(index) + 0.1,
        "duration_ms": 100,
        "requested_provider": "openai-codex",
        "requested_model": "gpt-5.5",
        "effective_provider": "openai-codex",
        "effective_model": "gpt-5.5",
        "attempt_count": 1,
        "retry_count": 0,
        "fallback_count": 0,
        "auxiliary_attempt_count": 0,
        "input_tokens": index + 1,
        "output_tokens": 2,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": index + 3,
        "estimated_cost_usd": 0.01,
        "cost_status": "estimated",
        "outcome": "success",
        "failure_class": "",
        "record_version": 1,
        "recorded_at": BASE_TIME + float(index) + 0.1,
    }
    payload.update(overrides)
    return payload


def test_fresh_schema_is_content_free_queryable_and_idempotent(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        columns = {
            row["name"]
            for row in db._conn.execute("PRAGMA table_info(turn_telemetry)").fetchall()
        }
        forbidden = {
            "content",
            "prompt",
            "messages",
            "request",
            "response",
            "tool_args",
            "tool_result",
            "error_message",
            "headers",
            "api_key",
        }
        assert not (columns & forbidden)

        db.record_turn_telemetry(**_row(1))
        db.record_turn_telemetry(**_row(1, input_tokens=99, total_tokens=101))
        rows = db.list_turn_telemetry(session_id="session-1", limit=10)
        assert len(rows) == 1
        assert rows[0]["input_tokens"] == 99
        assert rows[0]["event_type"] == "turn_terminal"
        assert rows[0]["route_type"] == "primary"
        assert rows[0]["estimated_cost_usd"] == pytest.approx(0.01)
        assert rows[0]["record_version"] == 1
        assert SENSITIVE_VALUE not in json.dumps(rows, sort_keys=True)

        with pytest.raises(TypeError):
            db.record_turn_telemetry(**_row(2), user_message=SENSITIVE_VALUE)
        with pytest.raises(ValueError):
            db.record_turn_telemetry(**_row(2, route_type=SENSITIVE_VALUE))
    finally:
        db.close()


def test_existing_database_migrates_additively_without_losing_rows(tmp_path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO schema_version(version) VALUES (25)")
    conn.execute("CREATE TABLE legacy_marker (value TEXT NOT NULL)")
    conn.execute("INSERT INTO legacy_marker(value) VALUES ('preserve-me')")
    conn.commit()
    conn.close()

    db = SessionDB(db_path=db_path)
    try:
        assert SCHEMA_VERSION >= 26
        assert db._conn.execute("SELECT value FROM legacy_marker").fetchone()[0] == "preserve-me"
        assert db._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='turn_telemetry'"
        ).fetchone()
        db.record_turn_telemetry(**_row(1))
        assert len(db.list_turn_telemetry()) == 1
    finally:
        db.close()


def test_retention_is_strictly_row_bounded(monkeypatch, tmp_path):
    monkeypatch.setattr(SessionDB, "_TURN_TELEMETRY_MAX_ROWS", 3)
    monkeypatch.setattr(SessionDB, "_TURN_TELEMETRY_RETENTION_S", 10_000_000)
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        for index in range(5):
            db.record_turn_telemetry(**_row(index))
        rows = db.list_turn_telemetry(limit=100)
        assert [row["turn_id"] for row in rows] == ["turn-4", "turn-3", "turn-2"]
    finally:
        db.close()


def test_high_row_retention_plan_uses_recorded_at_id_covering_index(tmp_path):
    """The per-write bounded prune must not build a temporary full-table sort."""
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    try:
        indexes = {
            row["name"]
            for row in db._conn.execute("PRAGMA index_list(turn_telemetry)").fetchall()
        }
        assert "idx_turn_telemetry_recorded_id" in indexes

        db._conn.executemany(
            """INSERT INTO turn_telemetry (
                   turn_id, session_id, started_at, ended_at, outcome, recorded_at
               ) VALUES (?, ?, ?, ?, 'success', ?)""",
            (
                (
                    f"plan-turn-{index}",
                    f"plan-session-{index % 8}",
                    BASE_TIME + index,
                    BASE_TIME + index + 0.01,
                    BASE_TIME + index + 0.01,
                )
                for index in range(20_000)
            ),
        )
        db._conn.commit()
        db._conn.execute("ANALYZE turn_telemetry")
        plan = [
            row["detail"]
            for row in db._conn.execute(
                """EXPLAIN QUERY PLAN
                   SELECT id FROM turn_telemetry
                   ORDER BY recorded_at DESC, id DESC
                   LIMIT -1 OFFSET ?""",
                (10_000,),
            ).fetchall()
        ]
        normalized = "\n".join(plan).upper()
        assert "USING COVERING INDEX IDX_TURN_TELEMETRY_RECORDED_ID" in normalized
        assert "USE TEMP B-TREE FOR ORDER BY" not in normalized
    finally:
        db.close()

    # Reopening reruns additive IF NOT EXISTS DDL and must remain idempotent.
    reopened = SessionDB(db_path=db_path)
    try:
        matching = reopened._conn.execute(
            """SELECT COUNT(*) FROM sqlite_master
               WHERE type = 'index' AND name = 'idx_turn_telemetry_recorded_id'"""
        ).fetchone()[0]
        assert matching == 1
    finally:
        reopened.close()


def test_query_filters_and_read_only_attach_do_not_write(tmp_path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    try:
        db.record_turn_telemetry(**_row(1, profile_name="forge", outcome="success"))
        db.record_turn_telemetry(
            **_row(
                2,
                session_id="session-2",
                profile_name="quinn",
                outcome="failed",
                disposition="failed",
                failure_class="timeout",
            )
        )
    finally:
        db.close()

    before = db_path.stat().st_mtime_ns
    read_only = SessionDB(db_path=db_path, read_only=True)
    try:
        rows = read_only.list_turn_telemetry(
            profile_name="quinn",
            outcome="failed",
            since=BASE_TIME + 1.5,
            limit=5000,
        )
        assert len(rows) == 1
        assert rows[0]["session_id"] == "session-2"
    finally:
        read_only.close()
    assert db_path.stat().st_mtime_ns == before


def test_concurrent_writers_remain_queryable(tmp_path):
    db_path = tmp_path / "state.db"
    seed = SessionDB(db_path=db_path)
    seed.close()

    def write(index: int):
        db = SessionDB(db_path=db_path)
        try:
            db.record_turn_telemetry(
                **_row(index, session_id=f"session-{index % 4}")
            )
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(20)))

    db = SessionDB(db_path=db_path, read_only=True)
    try:
        rows = db.list_turn_telemetry(limit=100)
        assert len(rows) == 20
        assert len({(row["session_id"], row["turn_id"]) for row in rows}) == 20
    finally:
        db.close()
