"""Evidence-only routing metadata backfill tests."""

import json
import sqlite3
import time
from pathlib import Path

from hermes_cli import kanban_db as kb


def _insert_run(conn, task_id, task_status, *, ended=True, profile="coder", session_id=None):
    """Insert one legacy run and return its id."""
    now = int(time.time())
    conn.execute(
        "INSERT INTO tasks (id,title,status,created_at) VALUES (?,?,?,?)",
        (task_id, task_id, task_status, now),
    )
    metadata = json.dumps({"worker_session_id": session_id}) if session_id else None
    cur = conn.execute(
        "INSERT INTO task_runs (task_id,profile,status,started_at,ended_at,metadata) "
        "VALUES (?,?,?,?,?,?)",
        (task_id, profile, "completed", now - 10, now if ended else None, metadata),
    )
    return cur.lastrowid


def _state_db(home: Path, profile: str, rows):
    """Create a profile state DB containing usage evidence rows."""
    path = home / "profiles" / profile / "state.db"
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE session_model_usage (session_id TEXT, task TEXT NOT NULL DEFAULT '', "
        "model TEXT, billing_provider TEXT, api_call_count INTEGER)"
    )
    conn.executemany(
        "INSERT INTO session_model_usage VALUES (?,?,?,?,?)", rows
    )
    conn.commit()
    conn.close()


def test_backfill_respects_cutoff_terminality_and_evidence(tmp_path, monkeypatch):
    """Only terminal pre-cutoff runs use unambiguous main-loop evidence."""
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    db = tmp_path / "kanban.db"
    kb.init_db(db)
    with kb.connect_closing(db) as conn:
        evidence_id = _insert_run(conn, "evidence", "done", session_id="s1")
        unknown_id = _insert_run(conn, "unknown", "archived", session_id="missing")
        active_id = _insert_run(conn, "active", "running", session_id="s1")
        no_end_id = _insert_run(conn, "no-end", "done", ended=False, session_id="s1")
        conn.execute(
            "UPDATE kanban_metadata SET value=? WHERE key='migration_cutoff_id'",
            (str(no_end_id),),
        )
        post_id = _insert_run(conn, "post", "done", session_id="s1")
        _state_db(home, "coder", [
            ("s1", "approval", "wrong", "wrong-p", 999),
            ("s1", "", "dominant", "actual-p", 7),
            ("s1", "", "minor", "minor-p", 2),
        ])

        result = kb.backfill_routing_metadata(conn, hermes_home=home)
        rows = {
            row["id"]: row for row in conn.execute(
                "SELECT id,routing_source,routing_model,routing_provider FROM task_runs"
            )
        }

    assert result.errors == 0
    assert rows[evidence_id]["routing_source"] == "inferred_evidence"
    assert rows[evidence_id]["routing_model"] == "dominant"
    assert rows[evidence_id]["routing_provider"] == "actual-p"
    assert rows[unknown_id]["routing_source"] == "legacy_unknown"
    assert rows[active_id]["routing_source"] is None
    assert rows[no_end_id]["routing_source"] is None
    assert rows[post_id]["routing_source"] is None


def test_backfill_tied_evidence_is_legacy_unknown(tmp_path, monkeypatch):
    """Ambiguous dominant usage is never guessed from current configuration."""
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    db = tmp_path / "kanban.db"
    kb.init_db(db)
    with kb.connect_closing(db) as conn:
        run_id = _insert_run(conn, "tie", "done", session_id="s2")
        conn.execute(
            "UPDATE kanban_metadata SET value=? WHERE key='migration_cutoff_id'",
            (str(run_id),),
        )
        _state_db(home, "coder", [
            ("s2", "", "m1", "p1", 4),
            ("s2", "", "m2", "p2", 4),
        ])
        result = kb.backfill_routing_metadata(conn, hermes_home=home)
        row = conn.execute(
            "SELECT routing_source,routing_model,routing_provider FROM task_runs WHERE id=?",
            (run_id,),
        ).fetchone()
    assert result.errors == 0
    assert tuple(row) == ("legacy_unknown", None, None)
