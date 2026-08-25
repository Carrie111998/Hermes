"""``hermes sessions list --after/--before`` date-window filters (#91900).

Non-mutating metadata listing bounded by session START time: ``--after``
is the inclusive lower bound, ``--before`` the exclusive upper bound —
the same vocabulary and parsing the archive/prune/export commands
already use (``session_filters.parse_point_in_time``).

Drives a real SessionDB against a temp HERMES_HOME; the CLI handler is
exercised through its public entry with an argparse-style namespace.
"""

import types

import pytest

from hermes_cli.session_filters import parse_point_in_time
from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    database = SessionDB(db_path=tmp_path / "state.db")
    return database


def _add_session(database, sid, started_at):
    database.create_session(sid, source="cli")
    # Backdate started_at (same sanctioned pattern as test_hermes_state).
    database._conn.execute(
        "UPDATE sessions SET started_at = ? WHERE id = ?", (started_at, sid)
    )
    database._conn.commit()


def test_list_sessions_rich_started_window_bounds(db):
    base = 1_700_000_000.0
    _add_session(db, "old", base - 100)
    _add_session(db, "in1", base)
    _add_session(db, "in2", base + 50)
    _add_session(db, "new", base + 10_000)

    rows = db.list_sessions_rich(
        limit=50, started_after=base, started_before=base + 10_000
    )
    ids = {r["id"] for r in rows}
    # --after inclusive, --before exclusive.
    assert ids == {"in1", "in2"}


def test_list_sessions_rich_open_ended_bounds(db):
    base = 1_700_000_000.0
    _add_session(db, "old", base - 100)
    _add_session(db, "new", base + 5)

    only_after = {
        r["id"] for r in db.list_sessions_rich(limit=50, started_after=base)
    }
    assert only_after == {"new"}

    only_before = {
        r["id"] for r in db.list_sessions_rich(limit=50, started_before=base)
    }
    assert only_before == {"old"}


# ─────────────────────────────────────────────────────────────────────
# CLI handler wiring
# ─────────────────────────────────────────────────────────────────────


def _run_list(db, monkeypatch, capsys, **extra):
    from hermes_cli.sessions_cmd import cmd_sessions

    ns = types.SimpleNamespace(
        sessions_action="list",
        source=None,
        limit=20,
        workspace=None,
        after=None,
        before=None,
        **extra,
    )
    monkeypatch.setattr(
        "hermes_state.SessionDB", lambda *a, **k: db, raising=False
    )
    # cmd_sessions opens its own SessionDB via get_hermes_home; the env var
    # fixture above already redirects it.
    import hermes_state as hs

    monkeypatch.setattr(hs.SessionDB, "__init__", lambda self, *a, **k: None) if False else None
    rc = cmd_sessions(ns)
    out = capsys.readouterr().out
    return rc, out


def test_cli_list_filters_by_date_window(db, tmp_path, monkeypatch, capsys):
    from datetime import datetime, timezone as dt_timezone

    aug5 = datetime(2026, 8, 5, 12, 0, tzinfo=dt_timezone.utc).timestamp()
    _add_session(db, "aug1", aug5)
    _add_session(db, "aug20", aug5 + 15 * 86400)

    ns = types.SimpleNamespace(
        sessions_action="list",
        source=None,
        limit=20,
        workspace=None,
        after="2026-08-01",
        before="2026-08-10",
    )
    # cmd_sessions imports SessionDB inside the function; patch the
    # module-level symbol it resolves at call time.
    import hermes_state as hs

    monkeypatch.setattr(hs, "SessionDB", lambda *a, **k: db)

    from hermes_cli.sessions_cmd import cmd_sessions

    cmd_sessions(ns)
    out = capsys.readouterr().out
    assert "aug1" in out
    assert "aug20" not in out, "--before must be exclusive"


def test_cli_list_rejects_inverted_window(db, tmp_path, monkeypatch, capsys):
    import hermes_state as hs

    monkeypatch.setattr(hs, "SessionDB", lambda *a, **k: db)
    ns = types.SimpleNamespace(
        sessions_action="list",
        source=None,
        limit=20,
        workspace=None,
        after="2026-08-08",
        before="2026-08-01",
    )
    from hermes_cli.sessions_cmd import cmd_sessions

    cmd_sessions(ns)
    out = capsys.readouterr().out + capsys.readouterr().err
    assert "not earlier than" in out


def test_parse_point_in_time_rejects_garbage():
    with pytest.raises(ValueError):
        parse_point_in_time("not-a-date", "--after")
