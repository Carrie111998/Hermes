"""Regression coverage for SessionDB reads racing connection shutdown."""

from contextlib import contextmanager

import pytest

from hermes_state import SessionDB


class _Cursor:
    def __init__(self, rows):
        self._rows = [_Row(row) for row in rows]

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _Row(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


class _ManagedConnection:
    def __init__(self, rows):
        self._rows = rows
        self.executed = False

    def execute(self, _sql, _params=()):
        self.executed = True
        return _Cursor(self._rows)


class _UnsafeWriterConnection:
    def execute(self, *_args, **_kwargs):
        raise AssertionError("read bypassed SessionDB._read_ctx()")


@pytest.mark.parametrize(
    ("rows", "invoke", "expected"),
    [
        (
            [{"holder": "compressor"}],
            lambda db: db.get_compression_lock_holder("session-1"),
            "compressor",
        ),
        (
            [
                {
                    "last_activity_description": "",
                    "last_activity_provenance": "unknown",
                }
            ],
            lambda db: db.clear_session_activity_labels("session-1"),
            None,
        ),
        (
            [
                {
                    "handoff_state": "pending",
                    "handoff_platform": "telegram",
                    "handoff_error": None,
                }
            ],
            lambda db: db.get_handoff_state("session-1"),
            {"state": "pending", "platform": "telegram", "error": None},
        ),
        (
            [{"id": "session-1", "_system_prompt_resolved": "prompt"}],
            lambda db: db.list_pending_handoffs(),
            [{"id": "session-1"}],
        ),
    ],
    ids=[
        "compression-lock-holder",
        "activity-label-fast-path",
        "handoff-state",
        "pending-handoffs",
    ],
)
def test_shutdown_sensitive_reads_use_managed_connection(rows, invoke, expected):
    """Reads must not touch the writer connection outside its close lock."""
    db = object.__new__(SessionDB)
    db._conn = _UnsafeWriterConnection()
    managed = _ManagedConnection(rows)

    @contextmanager
    def managed_read():
        yield managed

    db._read_ctx = managed_read

    assert invoke(db) == expected
    assert managed.executed is True