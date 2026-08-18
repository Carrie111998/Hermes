"""Tests for the Fix A native-Postgres pre-flight reader (fail-soft I/O glue)."""
from __future__ import annotations


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


def test_connect_bounds_both_phases(monkeypatch):
    """Both psycopg phases must be bounded, not just connect.

    psycopg has no default timeout on either phase: connect() waits on the OS
    TCP timeout and, once connected, a query blocks forever. This reader runs
    inside the gateway on the tracker-intent-applier subscriber, so an
    unbounded statement phase would stall the subscriber against a container
    that accepts the socket then stops answering -- and would silently falsify
    the module's fail-soft contract. Pin both so removing either fails here.
    """
    import intent_applier.job_state_reader as mod

    seen = {}

    class _FakePsycopg:
        @staticmethod
        def connect(dsn, **kwargs):
            seen.update(kwargs)
            seen["dsn"] = dsn
            return _FakeConn(None)

    monkeypatch.setitem(__import__("sys").modules, "psycopg", _FakePsycopg)

    r = mod.NativePgJobStateReader(dsn="x", connect_timeout=2.0)
    r._connect()

    assert seen["connect_timeout"] == 2.0
    assert seen["options"] == f"-c statement_timeout={mod._STATEMENT_TIMEOUT_MS}"
    assert seen["autocommit"] is True
