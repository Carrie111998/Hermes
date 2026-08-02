"""Tests for `hermes sessions import` — Claude Code JSONL → native sessions.

Covers the block mapping (text / thinking / tool_use / tool_result), session
id determinism, idempotency (skip when unchanged), and the replace path
(re-import after the source JSONL grew while Claude Code was still running).
"""

import json

import pytest

from hermes_cli.session_import import (
    MAX_FILE_BYTES,
    import_claude_sessions,
    parse_claude_jsonl,
)
from hermes_state import SessionDB


def _write_session(dirpath, stem="session-uuid-1234", lines=None):
    """Write a Claude Code JSONL file and return its Path."""
    p = dirpath / f"{stem}.jsonl"
    if lines is None:
        lines = [
            {
                "type": "user",
                "message": {"role": "user", "content": "check the podman services"},
                "timestamp": "2026-08-01T10:00:00Z",
                "cwd": "/root",
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "need to look at services"},
                        {"type": "text", "text": "Let me check the services."},
                        {
                            "type": "tool_use",
                            "id": "toolu_01",
                            "name": "terminal",
                            "input": {"command": "systemctl list-units"},
                        },
                    ],
                },
                "timestamp": "2026-08-01T10:00:05Z",
                "cwd": "/root",
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_01",
                            "content": "5 units running",
                        }
                    ],
                },
                "timestamp": "2026-08-01T10:00:06Z",
                "cwd": "/root",
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "All good."}],
                },
                "timestamp": "2026-08-01T10:00:07Z",
                "cwd": "/root",
            },
            {
                "type": "summary",
                "summary": "Checked podman services after power outage",
                "timestamp": "2026-08-01T10:01:00Z",
            },
        ]
    with open(p, "w", encoding="utf-8") as fh:
        for rec in lines:
            fh.write(json.dumps(rec) + "\n")
    return p


def test_parse_claude_jsonl_maps_blocks(tmp_path):
    p = _write_session(tmp_path)
    messages, title, ended, started = parse_claude_jsonl(p)

    assert title == "Checked podman services after power outage"
    assert ended is True
    assert started == "2026-08-01T10:00:00Z"

    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant", "tool", "assistant"]

    # user text message
    assert messages[0]["content"] == "check the podman services"
    # assistant with thinking + tool_use
    assert messages[1]["reasoning"] == "need to look at services"
    assert messages[1]["content"] == "Let me check the services."
    tc = messages[1]["tool_calls"]
    assert tc[0]["function"]["name"] == "terminal"
    assert json.loads(tc[0]["function"]["arguments"]) == {"command": "systemctl list-units"}
    # tool_result → role tool with matching id
    assert messages[2]["role"] == "tool"
    assert messages[2]["tool_call_id"] == "toolu_01"
    assert messages[2]["content"] == "5 units running"


def test_parse_skips_system_and_junk_lines(tmp_path):
    lines = [
        {"type": "system", "subtype": "init", "cwd": "/root"},
        {"type": "mode", "message": {"role": "assistant", "content": ""}, "cwd": "/root"},
        {"type": "user", "message": {"role": "user", "content": "hi"}, "timestamp": "t0"},
    ]
    p = _write_session(tmp_path, stem="junk-session", lines=lines)
    messages, _, _, _ = parse_claude_jsonl(p)
    assert [m["role"] for m in messages] == ["user"]
    assert messages[0]["content"] == "hi"


def test_import_writes_native_session(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    _write_session(projects)

    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    report = import_claude_sessions(str(projects), host="testhost", db=db,
                                    dry_run=False)
    db.close()

    assert report.imported == 1
    assert report.failed == 0
    assert report.summary().startswith("testhost: 1 imported")

    # Verify the native session: row exists, source tag, roles, FTS hit.
    db = SessionDB(db_path=db_path)
    try:
        sid = "claude_testhost_session-uuid-1234"
        s = db.get_session(sid)
        assert s is not None
        assert s["source"] == "claude_code"
        msgs = db.get_messages(sid)
        assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "assistant"]
        assert s["title"] == "Checked podman services after power outage"
        hits = db.search_messages("podman", limit=5)
        assert any(r["session_id"] == sid for r in (hits or []))
    finally:
        db.close()


def test_import_dry_run_writes_nothing(tmp_path):
    import hermes_state

    projects = tmp_path / "projects"
    projects.mkdir()
    _write_session(projects)

    # No --db override: exercises the default-db path, which the
    # `_hermetic_environment` fixture repoints at `hermes_state.DEFAULT_DB_PATH`
    # for this test (NOT tmp_path/"state.db" directly).
    report = import_claude_sessions(str(projects), host="testhost", dry_run=True)
    assert report.imported == 1  # counted as would-import
    # no db was created at the actual default path by dry-run
    assert not hermes_state.DEFAULT_DB_PATH.exists()


def test_import_dry_run_against_existing_db_writes_nothing(tmp_path):
    """--dry-run against an ALREADY-EXISTING default db must not mutate it.

    Regression: opening it via the normal writable SessionDB() path (even
    read-only-in-intent) still runs schema reconciliation — a write. This
    exercises the read-only open of the default db path.
    """
    import hermes_state

    projects = tmp_path / "projects"
    projects.mkdir()
    _write_session(projects)

    db_path = hermes_state.DEFAULT_DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    SessionDB(db_path=db_path).close()
    mtime_before = db_path.stat().st_mtime_ns

    report = import_claude_sessions(str(projects), host="testhost", dry_run=True)
    assert report.imported == 1
    assert db_path.stat().st_mtime_ns == mtime_before

    db = SessionDB(db_path=db_path)
    try:
        assert db.get_session("claude_testhost_session-uuid-1234") is None
    finally:
        db.close()


def test_parse_tolerates_torn_tail(tmp_path):
    p = _write_session(tmp_path)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write('{"type": "user", "message": {"role": "user", "content": "half')  # no newline
    messages, _, _, _ = parse_claude_jsonl(p)
    assert len(messages) == 4  # torn line ignored, rest intact


def test_import_idempotent_second_run_skips(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    _write_session(projects)

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        first = import_claude_sessions(str(projects), host="testhost", db=db)
        second = import_claude_sessions(str(projects), host="testhost", db=db)
        assert first.imported == 1
        assert second.imported == 0
        assert second.updated == 0
        assert second.skipped == 1
        sid = "claude_testhost_session-uuid-1234"
        row = db.get_session(sid)
        assert row is not None and row["message_count"] == 4
    finally:
        db.close()


def test_import_replace_on_source_growth(tmp_path):
    """Source JSONL grew while Claude was running → replace, no duplicates."""
    projects = tmp_path / "projects"
    projects.mkdir()
    p = _write_session(projects)

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        first = import_claude_sessions(str(projects), host="testhost", db=db)
        assert first.imported == 1

        # Simulate Claude appending one more assistant turn.
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "One more thing."}],
                },
                "timestamp": "2026-08-01T10:02:00Z",
            }) + "\n")

        second = import_claude_sessions(str(projects), host="testhost", db=db)
        assert second.imported == 0
        assert second.updated == 1

        sid = "claude_testhost_session-uuid-1234"
        stored = db.get_messages(sid)
        # 4 original + 1 appended, exactly once each — no duplicates.
        assert len(stored) == 5
        row = db.get_session(sid)
        assert row is not None and row["message_count"] == 5
    finally:
        db.close()


def test_import_backfills_title_and_end_after_summary_only_change(tmp_path):
    """A trailing `summary`/`ai-title` record appended later must still be
    picked up even though it doesn't change the message count."""
    projects = tmp_path / "projects"
    projects.mkdir()
    lines = [
        {
            "type": "user",
            "message": {"role": "user", "content": "check the podman services"},
            "timestamp": "2026-08-01T10:00:00Z",
        },
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "All good."}]},
            "timestamp": "2026-08-01T10:00:07Z",
        },
    ]
    p = _write_session(projects, lines=lines)

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        first = import_claude_sessions(str(projects), host="testhost", db=db)
        assert first.imported == 1
        sid = "claude_testhost_session-uuid-1234"
        row = db.get_session(sid)
        assert row is not None
        assert row["title"] is None
        assert row["ended_at"] is None
        assert row["message_count"] == 2

        # Claude closes the session: a `summary` line is appended, message
        # count is unchanged.
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "type": "summary",
                "summary": "Checked podman services",
                "timestamp": "2026-08-01T10:01:00Z",
            }) + "\n")

        second = import_claude_sessions(str(projects), host="testhost", db=db)
        assert second.updated == 1
        assert second.skipped == 0

        row = db.get_session(sid)
        assert row is not None
        assert row["title"] == "Checked podman services"
        assert row["ended_at"] is not None
        assert row["message_count"] == 2  # unchanged — no replace needed

        third = import_claude_sessions(str(projects), host="testhost", db=db)
        assert third.skipped == 1
        assert third.updated == 0
    finally:
        db.close()


def test_import_preserves_original_message_timestamps(tmp_path):
    """Message timestamps must come from the transcript, not from import time.

    Regression: the atomic create path feeds messages straight into
    SessionDB._insert_message_rows, which only accepts a float/epoch value
    and silently falls back to "now" for anything else (Claude's JSONL
    stores ISO-8601 strings) — a value close to `time.time()` here would
    mean that fallback fired instead of the parsed transcript timestamp.
    """
    import time as _time

    projects = tmp_path / "projects"
    projects.mkdir()
    _write_session(projects)

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        import_claude_sessions(str(projects), host="testhost", db=db)
        sid = "claude_testhost_session-uuid-1234"
        msgs = db.get_messages(sid)
        first_ts = msgs[0]["timestamp"]
        # "2026-08-01T10:00:00Z" — far from "now" and from each other.
        assert abs(first_ts - _time.time()) > 3600
        assert msgs[1]["timestamp"] > first_ts
    finally:
        db.close()


def test_import_does_not_use_profile_name_for_host(tmp_path):
    """--host is metadata, not Hermes profile ownership (profile_name)."""
    projects = tmp_path / "projects"
    projects.mkdir()
    _write_session(projects)

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        import_claude_sessions(str(projects), host="testhost", db=db)
        row = db.get_session("claude_testhost_session-uuid-1234")
        assert row is not None
        assert row["profile_name"] is None
        model_config = json.loads(row["model_config"] or "{}")
        assert model_config.get("_import_host") == "testhost"
    finally:
        db.close()


def test_import_permanent_title_conflict_settles_to_skipped(tmp_path):
    """A title that permanently collides with another session (e.g. Claude
    Code compaction recreating the same conversation under a new session id)
    must stop being reported as needing an update once the conflict is
    known — not retried forever."""
    projects = tmp_path / "projects"
    projects.mkdir()
    lines = [
        {
            "type": "user",
            "message": {"role": "user", "content": "hi"},
            "timestamp": "2026-08-01T10:00:00Z",
        },
        {"type": "ai-title", "aiTitle": "Duplicate Title"},
    ]
    _write_session(projects, stem="dup-session", lines=lines)

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("other-session", source="claude_code")
        db.set_session_title("other-session", "Duplicate Title")

        first = import_claude_sessions(str(projects), host="testhost", db=db)
        assert first.imported == 1
        sid = "claude_testhost_dup-session"
        row = db.get_session(sid)
        assert row is not None and row["title"] is None  # write collided, caught

        second = import_claude_sessions(str(projects), host="testhost", db=db)
        assert second.updated == 0
        assert second.skipped == 1  # settled — not retried as a perpetual update

        row = db.get_session(sid)
        assert row["title"] is None
    finally:
        db.close()
