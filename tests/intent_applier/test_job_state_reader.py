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


# --------------------------------------------------------------------------
# Identity resolution (2026-08-23 incident).
#
# An intent's job_id is jobs.id -- the value the applier POSTs to :4100 as
# /jobs/<id>. The reader used to match jobs.external_job_key instead. Those two
# columns disagree on every live row, so the lookup either found nothing or
# found a DIFFERENT job. Five operator approvals were discarded because a
# stranger's business state answered for them.
# --------------------------------------------------------------------------


class _RecordingCursor(_FakeCursor):
    def __init__(self, row, sink):
        super().__init__(row)
        self._sink = sink

    def execute(self, sql, params=None):
        self._sink.append((sql, params))
        return None


class _RecordingConn(_FakeConn):
    def __init__(self, row, sink):
        super().__init__(row)
        self._sink = sink

    def cursor(self):
        return _RecordingCursor(self._row, self._sink)


def test_query_matches_on_id_and_binds_job_id_to_every_placeholder():
    sink = []
    r = NativePgJobStateReader(dsn="x")
    r._connect = lambda: _RecordingConn(("scored", True), sink)
    r("job-uuid")
    sql, params = sink[0]
    assert "id::text = %s" in sql, "must resolve the way the write path resolves"
    assert params == ("job-uuid", "job-uuid", "job-uuid")


def test_an_id_match_outranks_an_external_key_match():
    """The tie-break is the whole fix: a twin must never answer for the target.

    53 live rows carry an external_job_key equal to ANOTHER row's id. Without the
    ORDER BY, Postgres may return either; with it the id row always wins.
    """
    from intent_applier.job_state_reader import _QUERY

    normalized = " ".join(_QUERY.split()).lower()
    assert "order by matched_by_id desc" in normalized
    assert normalized.index("id::text = %s") < normalized.index("external_job_key = %s")


def test_reader_returns_first_column_even_though_two_are_selected():
    r = NativePgJobStateReader(dsn="x")
    r._connect = lambda: _FakeConn(("approved_for_tailor", True))
    assert r("job-uuid") == "approved_for_tailor"


def test_query_runs_and_prefers_the_id_row_against_live_postgres():
    """READ-ONLY: proves the SQL parses and orders correctly in real Postgres.

    Every offline test above stubs the cursor, so none of them would catch a
    syntax error, a bad cast, or an ORDER BY that Postgres refuses. Skips when
    Postgres is unreachable or when no twinned row exists to discriminate on.
    """
    import os
    import pytest

    psycopg = pytest.importorskip("psycopg")
    dsn = os.environ.get(
        "HERMES_JOBFLOW_PG_DSN", "postgres://jobflow@127.0.0.1:5432/jobflow"
    )
    try:
        conn = psycopg.connect(dsn, connect_timeout=2.0, autocommit=True)
    except Exception:
        pytest.skip("jobflow Postgres unreachable")
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select t.id::text, t.current_business_state
                from jobs t
                where exists (select 1 from jobs u where u.external_job_key = t.id::text)
                limit 1
                """
            )
            row = cur.fetchone()
    if row is None:
        pytest.skip("no twinned row available to discriminate on")
    job_id, own_state = row

    r = NativePgJobStateReader(dsn=dsn)
    assert r(job_id) == own_state, "reader must report the target job, not its twin"


def test_default_dsn_carries_no_password():
    """The in-source default DSN must not embed a credential.

    `_DEFAULT_DSN` shipped with the same word as both user and password until
    2026-09-01, when a public-fork exposure sweep found it published on
    github.com/daragao3/hermes-agent. The password was decorative -- the loopback
    server authenticates this user without one -- so it bought nothing and had to
    be read, judged and cleared by a human during that sweep.

    Removing it is only safe while nothing depends on the password being present,
    so this pins the *shape*: a userinfo section with a colon in it. Deployments
    that genuinely need a password still supply a whole DSN through
    HERMES_JOBFLOW_PG_DSN, which this does not constrain.
    """
    from urllib.parse import urlsplit

    from intent_applier.job_state_reader import _DEFAULT_DSN

    userinfo = urlsplit(_DEFAULT_DSN).netloc.rpartition("@")[0]
    assert ":" not in userinfo, (
        f"_DEFAULT_DSN embeds a password in {userinfo!r}; put it in "
        "HERMES_JOBFLOW_PG_DSN instead -- this file is published to a public fork"
    )


def test_env_dsn_still_overrides_the_default(monkeypatch):
    """The override path is what deployments needing a password must use."""
    from intent_applier import job_state_reader

    monkeypatch.setenv("HERMES_JOBFLOW_PG_DSN", "postgres://u@example.invalid:5432/d")
    reader = job_state_reader.NativePgJobStateReader()
    assert reader._dsn == "postgres://u@example.invalid:5432/d"

    monkeypatch.delenv("HERMES_JOBFLOW_PG_DSN", raising=False)
    assert job_state_reader.NativePgJobStateReader()._dsn == job_state_reader._DEFAULT_DSN
