"""Tests for the Fix A native-Postgres pre-flight reader (fail-soft I/O glue)."""
from __future__ import annotations

import pytest

from intent_applier.job_state_reader import NativePgJobStateReader, build_default_reader


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        return None

    def fetchone(self):
        return self._row


class _FakeConn:
    closed = False

    def __init__(self, row):
        self._row = row

    def cursor(self):
        return _FakeCursor(self._row)


def test_returns_business_state_on_success():
    r = NativePgJobStateReader(dsn="x")
    r._connect = lambda: _FakeConn(("materials_ready",))
    assert r("job-uuid") == "materials_ready"


def test_unknown_job_returns_none():
    r = NativePgJobStateReader(dsn="x")
    r._connect = lambda: _FakeConn(None)  # no row
    assert r("missing") is None


def test_connect_failure_is_fail_soft_and_arms_cooldown():
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise RuntimeError("pg down")

    r = NativePgJobStateReader(dsn="x", down_cooldown_seconds=999)
    r._connect = boom
    # First call fails soft -> None, and one connect attempt was made.
    assert r("job") is None
    assert calls["n"] == 1
    # Second call within cooldown short-circuits WITHOUT another connect attempt
    # (so a Postgres outage can't add a connect-timeout to every intent).
    assert r("job") is None
    assert calls["n"] == 1


def test_query_failure_resets_connection():
    class _BadConn:
        closed = False

        def cursor(self):
            raise RuntimeError("query blew up")

    r = NativePgJobStateReader(dsn="x", down_cooldown_seconds=0)
    r._connect = lambda: _BadConn()
    assert r("job") is None
    assert r._conn is None  # reset so the next call reconnects


def test_build_default_reader_does_not_raise():
    # Present-or-absent psycopg: must return a reader or None, never raise.
    reader = build_default_reader()
    assert reader is None or isinstance(reader, NativePgJobStateReader)
