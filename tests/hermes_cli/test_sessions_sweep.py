"""Regression tests: `hermes sessions sweep` reaches stale-OPEN sessions.

`prune`/`archive` select through `_prune_filter_where`, which hard-pins
``s.ended_at IS NOT NULL`` — so sessions that never ended (hard kills,
crashes, pre-v0.20.4 one-shot ``-q`` exits) are structurally invisible to
both. ``sweep`` exposes ``SessionDB.archive_stale_sessions`` with a
``--dry-run`` preview, aging on real recency (freshest of
``last_activity_at`` / latest message timestamp / ``started_at``) instead of
end state.

Contract asserted here (see AGENTS.md: behavior contracts, not snapshots):
  * an open-but-idle row IS a sweep candidate — and is NOT a prune candidate
  * a recently-active open row is spared (recency, not end state, decides)
  * dry-run lists but archives nothing; the real run archives exactly the
    listed set; a repeat run is an idempotent no-op
  * pinned rows are spared unless ``--include-pinned``
"""

import time
from types import SimpleNamespace

import pytest

from hermes_cli.sessions_cmd import cmd_sessions
from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    d = SessionDB(tmp_path / "state.db")
    yield d
    try:
        d.close()
    except Exception:
        pass


def _mk_idle_open(db, sid, *, days_idle, msgs=1):
    """An OPEN (ended_at NULL) session last active ``days_idle`` days ago."""
    db.create_session(session_id=sid, source="cli")
    for i in range(msgs):
        db.append_message(session_id=sid, role="user", content=f"m{i}")
    old = time.time() - days_idle * 86400
    with db._lock:
        db._conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (old, sid))
        db._conn.execute(
            "UPDATE messages SET timestamp = ? WHERE session_id = ?", (old, sid)
        )
        db._conn.commit()
    row = db.get_session(sid)
    assert row["ended_at"] is None  # precondition: never ended


def _mk_active_open(db, sid):
    """An OPEN session with a message right now — must never be swept."""
    db.create_session(session_id=sid, source="cli")
    db.append_message(session_id=sid, role="user", content="now")
    assert db.get_session(sid)["ended_at"] is None


def _sweep_args(**kw):
    base = dict(
        sessions_action="sweep", idle_days=30.0, include_pinned=False,
        dry_run=False, yes=True,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_sweep_reaches_what_prune_cannot(db):
    """The same row is invisible to prune and visible to sweep."""
    _mk_idle_open(db, "leaked", days_idle=45)

    # prune's selector pins ended_at IS NOT NULL -> zero candidates
    assert db.list_prune_candidates(older_than_days=30) == []
    assert db.count_open_prune_matches(older_than_days=30) == 1  # it sees, skips

    # sweep's candidates include exactly that row
    ids = [c["id"] for c in db.list_stale_archive_candidates(30)]
    assert ids == ["leaked"]
    assert db.archive_stale_sessions(30) == 1
    assert db.get_session("leaked")["archived"] == 1


def test_sweep_spares_recently_active_open(db):
    """Recency, not end state, decides — a live conversation is never hidden."""
    _mk_idle_open(db, "old", days_idle=60)
    _mk_active_open(db, "live")

    ids = [c["id"] for c in db.list_stale_archive_candidates(30)]
    assert "live" not in ids
    assert ids == ["old"]

    assert db.archive_stale_sessions(30) == 1
    assert db.get_session("live")["archived"] == 0


def test_sweep_dry_run_archives_nothing_then_run_is_idempotent(db, capsys):
    _mk_idle_open(db, "a", days_idle=40)
    _mk_idle_open(db, "b", days_idle=50)

    cands = db.list_stale_archive_candidates(30)
    assert [c["id"] for c in cands] == ["b", "a"]  # oldest-first (50d before 40d)
    for c in cands:  # dry-run semantics: list, never write
        assert db.get_session(c["id"])["archived"] == 0

    assert db.archive_stale_sessions(30) == 2
    assert db.archive_stale_sessions(30) == 0  # idempotent repeat
    assert db.get_session("a")["archived"] == 1
    assert db.get_session("b")["archived"] == 1


def test_sweep_pin_guard_and_opt_in(db):
    _mk_idle_open(db, "keep", days_idle=90)
    db.set_session_pinned("keep", True)

    assert db.list_stale_archive_candidates(30) == []
    assert db.archive_stale_sessions(30) == 0
    assert db.get_session("keep")["archived"] == 0

    opt_in = db.list_stale_archive_candidates(30, exclude_pinned=False)
    assert [c["id"] for c in opt_in] == ["keep"]
    assert db.archive_stale_sessions(30, exclude_pinned=False) == 1


def test_null_last_activity_falls_back_to_started_at(db):
    """Older rows with NULL last_activity_at still age via started_at."""
    _mk_idle_open(db, "legacy", days_idle=100)
    with db._lock:
        db._conn.execute(
            "UPDATE sessions SET last_activity_at = NULL WHERE id = 'legacy'"
        )
        db._conn.commit()

    ids = [c["id"] for c in db.list_stale_archive_candidates(30)]
    assert "legacy" in ids
