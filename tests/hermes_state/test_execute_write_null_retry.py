"""Regression tests for transient SQLite WAL append SystemErrors (#85079)."""

import pytest

from hermes_state import SessionDB


def test_execute_write_retries_transient_null_without_exception(tmp_path, monkeypatch):
    """The SQLite driver spelling is retried through the normal write budget."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        attempts = []
        sleeps = []

        def write(_conn):
            attempts.append(True)
            if len(attempts) == 1:
                raise SystemError("returned NULL without setting an exception")
            return "committed"

        def fake_sleep(deadline, patience_s):
            sleeps.append((deadline, patience_s))
            return True

        monkeypatch.setattr(db, "_sleep_before_write_retry", fake_sleep)

        assert db._execute_write(write, patience_s=1.0) == "committed"
        assert len(attempts) == 2
        assert len(sleeps) == 1
    finally:
        db.close()


def test_execute_write_does_not_swallow_unrelated_system_error(tmp_path, monkeypatch):
    """Only the known SQLite driver message is eligible for retry."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        sleeps = []

        def fail(_conn):
            raise SystemError("unrelated driver failure")

        monkeypatch.setattr(
            db,
            "_sleep_before_write_retry",
            lambda deadline, patience_s: sleeps.append(True),
        )

        with pytest.raises(SystemError, match="unrelated driver failure"):
            db._execute_write(fail, patience_s=1.0)

        assert sleeps == []
    finally:
        db.close()
