"""Process-kill crash matrix for durable gateway lifecycle transitions.

Each worker uses the real SessionStore and SessionDB with an explicit fresh
state.db. The only seam is a private callback which exits after a named,
durable post-condition; SQLite is never mocked.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from gateway.config import GatewayConfig, Platform, SessionResetPolicy
from gateway.session import SessionSource, SessionStore, build_session_key
from hermes_state import SessionDB

from tests.gateway.lifecycle_crash_worker import EXIT_CODE


REPO = Path(__file__).resolve().parents[2]
SOURCE = SessionSource(
    platform=Platform.TELEGRAM, chat_id="lifecycle-crash-chat",
    user_id="lifecycle-crash-user", chat_type="dm", thread_id="lifecycle-crash-thread",
)
KEY = build_session_key(SOURCE)
PEER = {
    "source": "telegram", "user_id": SOURCE.user_id, "session_key": KEY,
    "chat_id": SOURCE.chat_id, "chat_type": SOURCE.chat_type,
    "thread_id": SOURCE.thread_id,
}

CASES = [
    ("reset", "gateway.reset.after_marker_persisted", True),
    ("reset", "gateway.reset.after_old_promoted", False),
    ("reset", "gateway.reset.after_replacement_created", False),
    ("reset", "gateway.reset.after_peer_recorded", False),
    ("reset", "gateway.reset.after_final_route_published", False),
    ("auto-reset", "gateway.auto_reset.after_marker_persisted", True),
    ("auto-reset", "gateway.auto_reset.after_old_promoted", False),
    ("auto-reset", "gateway.auto_reset.after_replacement_created", False),
    ("auto-reset", "gateway.auto_reset.after_peer_recorded", False),
    ("auto-reset", "gateway.auto_reset.after_final_route_published", False),
    ("switch", "gateway.switch.after_marker_persisted", True),
    ("switch", "gateway.switch.after_old_promoted", False),
    ("switch", "gateway.switch.after_target_reopened", False),
    ("switch", "gateway.switch.after_peer_recorded", False),
    ("switch", "gateway.switch.after_final_route_published", False),
    ("compression-advance", "gateway.compression_advance.after_marker_persisted", True),
    ("compression-advance", "gateway.compression_advance.after_final_route_published", False),
    ("prune", "gateway.prune.after_old_closed", True),
    ("prune", "gateway.prune.after_route_absence_published", False),
]
DB_COMPRESSION_CASES = [
    "db.compression.after_child_insert", "db.compression.after_handoff_messages",
    "db.compression.after_child_counts", "db.compression.after_parent_close",
]


def _env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    return {**os.environ, "HERMES_HOME": str(home), "LIFECYCLE_DB": str(home / "state.db"),
            "LIFECYCLE_SESSIONS_DIR": str(tmp_path / "sessions"), "PYTHONDONTWRITEBYTECODE": "1"}


def _run_worker(tmp_path: Path, case: str, stage: str) -> dict[str, str]:
    env = _env(tmp_path)
    result = subprocess.run([sys.executable, "-m", "tests.gateway.lifecycle_crash_worker", case, stage],
                            cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == EXIT_CODE, result.stderr
    return env


def _fresh_store(env: dict[str, str], *, delete_mirror: bool) -> SessionStore:
    if delete_mirror:
        (Path(env["LIFECYCLE_SESSIONS_DIR"]) / "sessions.json").unlink(missing_ok=True)
    store = SessionStore(sessions_dir=Path(env["LIFECYCLE_SESSIONS_DIR"]),
                         config=GatewayConfig(default_reset_policy=SessionResetPolicy(mode="none")))
    if store._db is not None:
        store._db.close()
    store._db = SessionDB(db_path=Path(env["LIFECYCLE_DB"]))
    return store


def _route(db: SessionDB) -> dict | None:
    row = db._conn.execute("SELECT entry_json FROM gateway_routing WHERE session_key = ?", (KEY,)).fetchone()
    return json.loads(row["entry_json"]) if row else None


def _session(db: SessionDB, session_id: str):
    return db._conn.execute(
        """SELECT id, parent_session_id, ended_at, end_reason, source, user_id,
                  session_key, chat_id, chat_type, thread_id, origin_json
           FROM sessions WHERE id = ?""", (session_id,)
    ).fetchone()


def _assert_exact_peer(row, *, parent_session_id: str | None = None) -> None:
    assert row is not None
    assert {field: row[field] for field in PEER} == PEER
    assert json.loads(row["origin_json"]) == SOURCE.to_dict()
    if parent_session_id is not None:
        assert row["parent_session_id"] == parent_session_id


def _assert_one_live_head(db: SessionDB, expected_id: str) -> None:
    live = db._conn.execute("SELECT id FROM sessions WHERE ended_at IS NULL ORDER BY id").fetchall()
    assert [row["id"] for row in live] == [expected_id]


def _assert_session_count(db: SessionDB, expected: int) -> None:
    assert db._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == expected


@pytest.mark.parametrize(("case", "stage", "delete_mirror"), CASES)
def test_gateway_lifecycle_kill_recovers_exact_durable_target(
    tmp_path, case, stage, delete_mirror,
):
    """Every gateway failpoint restarts through new Store/DB objects only."""
    env = _run_worker(tmp_path, case, stage)
    db_path = Path(env["LIFECYCLE_DB"])
    before = SessionDB(db_path=db_path)
    try:
        route_before = _route(before)
        assert before._conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        if case == "prune" and stage == "gateway.prune.after_route_absence_published":
            assert route_before is None
            marker_target = None
        else:
            assert route_before is not None
            marker = route_before["metadata"].get("terminal_transition")
            marker_target = marker.get("target_session_id") if marker else None
            if case in {"reset", "auto-reset", "switch", "compression-advance"} and marker:
                assert marker_target
            if case in {"reset", "auto-reset", "switch", "compression-advance"} and not marker:
                marker_target = route_before["session_id"]
    finally:
        before.close()

    store = _fresh_store(env, delete_mirror=delete_mirror)
    try:
        recovered = store.get_or_create_session(SOURCE)
        db = store._db
        assert db is not None
        route_after = _route(db)
        assert route_after is not None
        assert route_after["session_id"] == recovered.session_id
        assert "terminal_transition" not in route_after["metadata"]

        if case in {"reset", "auto-reset"}:
            assert recovered.session_id == marker_target
            old = db._conn.execute("SELECT id FROM sessions WHERE id != ?", (recovered.session_id,)).fetchone()
            assert old is not None
            expected_reason = "idle" if case == "auto-reset" else "session_reset"
            assert _session(db, old["id"])["end_reason"] == expected_reason
            _assert_exact_peer(_session(db, recovered.session_id))
            _assert_one_live_head(db, recovered.session_id)
            _assert_session_count(db, 2)
        elif case == "switch":
            assert marker_target == "resume-target"
            assert recovered.session_id == marker_target
            old = db._conn.execute(
                "SELECT id FROM sessions WHERE end_reason = 'session_switch'"
            ).fetchone()
            assert old is not None
            assert _session(db, old["id"])["end_reason"] == "session_switch"
            _assert_exact_peer(_session(db, recovered.session_id))
            ancestor = db._conn.execute(
                "SELECT id FROM sessions WHERE id = 'resume-parent'"
            ).fetchone()
            assert ancestor is not None
            assert _session(db, ancestor["id"])["end_reason"] == "compression"
            _assert_exact_peer(_session(db, ancestor["id"]))
            _assert_one_live_head(db, recovered.session_id)
            _assert_session_count(db, 3)
        elif case == "compression-advance":
            assert marker_target == "compression-child"
            assert recovered.session_id == marker_target
            parent = db._conn.execute("SELECT id FROM sessions WHERE id != ?", (recovered.session_id,)).fetchone()
            assert parent is not None
            assert _session(db, parent["id"])["end_reason"] == "compression"
            _assert_exact_peer(_session(db, recovered.session_id), parent_session_id=parent["id"])
            _assert_one_live_head(db, recovered.session_id)
            _assert_session_count(db, 2)
        else:
            # A prune marker must publish route absence before a resolver can
            # allocate a new ID; a post-publication kill starts from absence.
            old = db._conn.execute(
                "SELECT id FROM sessions WHERE id != ?", (recovered.session_id,)
            ).fetchone()
            assert old is not None and _session(db, old["id"])["end_reason"] == "session_prune"
            assert recovered.session_id != old["id"]
            _assert_exact_peer(_session(db, recovered.session_id))
            _assert_one_live_head(db, recovered.session_id)
            _assert_session_count(db, 2)

    finally:
        store._db.close()


@pytest.mark.parametrize("stage", DB_COMPRESSION_CASES)
def test_db_compression_kill_rolls_back_child_row_messages_and_peer_visible_state(tmp_path, stage):
    """Each pure SQLite failpoint is atomic, including handoff messages/routes."""
    env = _run_worker(tmp_path, "db-compression", stage)
    db = SessionDB(db_path=Path(env["LIFECYCLE_DB"]))
    try:
        parent = _session(db, "parent")
        assert parent is not None and parent["ended_at"] is None and parent["end_reason"] is None
        assert db._conn.execute("SELECT 1 FROM sessions WHERE id = 'child'").fetchone() is None
        assert db._conn.execute("SELECT 1 FROM messages WHERE session_id = 'child'").fetchone() is None
        assert db._conn.execute("SELECT 1 FROM gateway_routing WHERE entry_json LIKE '%child%'").fetchone() is None
        assert db._conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        db.close()
