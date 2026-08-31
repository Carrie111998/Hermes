"""Session end/reopen writes must identify their caller (#98495).

A live gateway session row was transiently marked ended (end_reason set on a
row still serving turns) and silently reopened by the #54878 stale-route
self-heal — four times in one day across two profiles. Because
``end_session()`` / ``reopen_session()`` logged nothing, the writer could
not be identified from logs at all; only the downstream routing warning was
visible. These tests pin the audit trail: every end write records
session/reason/caller, the first-reason-wins no-op stays at DEBUG, and only
the ended -> open transition of a reopen is worth an INFO line.
"""
import logging

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def test_end_session_logs_reason_and_caller(db, caplog):
    db.create_session("s1", source="desktop")
    with caplog.at_level(logging.INFO, logger="hermes_state"):
        db.end_session("s1", "agent_close")
    matches = [r for r in caplog.records if "Session ended" in r.message]
    assert matches, f"expected an end audit line, got {[r.message for r in caplog.records]}"
    line = matches[0].getMessage()
    assert "session=s1" in line
    assert "reason=agent_close" in line
    # The caller is what makes the trail diagnostic: it must name the module
    # and function that performed the write, not just the row that changed.
    assert "test_end_session_logs_reason_and_caller" in line
    assert __name__ in line


def test_already_ended_row_logs_noop_at_debug(db, caplog):
    db.create_session("s1", source="desktop")
    db.end_session("s1", "compression")
    with caplog.at_level(logging.DEBUG, logger="hermes_state"):
        db.end_session("s1", "agent_close")
    matches = [
        r for r in caplog.records
        if "Session end no-op" in r.message and r.levelno == logging.DEBUG
    ]
    assert matches
    line = matches[0].getMessage()
    assert "session=s1" in line
    assert "reason=agent_close" in line
    # First reason wins: the row was not re-ended under the new reason.
    assert db.get_session("s1")["end_reason"] == "compression"


def test_reopen_session_logs_the_ended_to_open_transition(db, caplog):
    db.create_session("s1", source="desktop")
    db.end_session("s1", "agent_close")
    with caplog.at_level(logging.INFO, logger="hermes_state"):
        db.reopen_session("s1")
    matches = [r for r in caplog.records if "Session reopened" in r.message]
    assert matches, f"expected a reopen audit line, got {[r.message for r in caplog.records]}"
    line = matches[0].getMessage()
    assert "session=s1" in line
    assert "test_reopen_session_logs_the_ended_to_open_transition" in line


def test_reopen_of_a_live_session_is_not_logged(db, caplog):
    # A live row has nothing to recover; the routing code paths that call
    # reopen defensively must not spam INFO on every check.
    db.create_session("s1", source="desktop")
    with caplog.at_level(logging.INFO, logger="hermes_state"):
        db.reopen_session("s1")
    assert not [r for r in caplog.records if "Session reopened" in r.message]
    row = db.get_session("s1")
    assert row["ended_at"] is None and row["end_reason"] is None
