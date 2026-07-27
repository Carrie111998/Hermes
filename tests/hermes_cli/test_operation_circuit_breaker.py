import sqlite3

import pytest

from hermes_cli import operation_circuit_breaker as breaker


def test_repetitive_failures_open_circuit():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    assert breaker.record_outcome(
        conn, operation_key="stripe:charge", succeeded=False,
        failure_threshold=2, cooldown_seconds=60,
    ) == "closed"
    assert breaker.record_outcome(
        conn, operation_key="stripe:charge", succeeded=False,
        failure_threshold=2, cooldown_seconds=60,
    ) == "open"
    with pytest.raises(breaker.CircuitOpenError):
        breaker.assert_admissible(conn, "stripe:charge")


def test_cooldown_admits_only_one_recovery_probe():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    breaker.ensure_schema(conn)
    now = 1_700_000_000
    conn.execute(
        """INSERT INTO operation_circuit_breakers
           (operation_key, consecutive_failures, state, retry_after, updated_at)
           VALUES (?, 2, 'open', ?, ?)""",
        ("stripe:charge", now - 1, now - 1),
    )

    # Freeze the clock so the probe lease can be inspected deterministically.
    original_time = breaker.time.time
    breaker.time.time = lambda: now
    try:
        breaker.assert_admissible(conn, "stripe:charge")
        with pytest.raises(breaker.CircuitOpenError, match="probe is already"):
            breaker.assert_admissible(conn, "stripe:charge")
    finally:
        breaker.time.time = original_time

    state = conn.execute(
        "SELECT state, retry_after FROM operation_circuit_breakers WHERE operation_key=?",
        ("stripe:charge",),
    ).fetchone()
    assert state["state"] == "half_open"
    assert state["retry_after"] == now + breaker.HALF_OPEN_PROBE_LEASE_SECONDS


def test_expired_recovery_probe_can_be_reclaimed():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    breaker.ensure_schema(conn)
    now = 1_700_000_000
    conn.execute(
        """INSERT INTO operation_circuit_breakers
           (operation_key, consecutive_failures, state, retry_after, updated_at)
           VALUES (?, 2, 'half_open', ?, ?)""",
        ("stripe:charge", now - 1, now - 1),
    )
    original_time = breaker.time.time
    breaker.time.time = lambda: now
    try:
        breaker.assert_admissible(conn, "stripe:charge")
    finally:
        breaker.time.time = original_time
    state = conn.execute(
        "SELECT retry_after FROM operation_circuit_breakers WHERE operation_key=?",
        ("stripe:charge",),
    ).fetchone()
    assert state["retry_after"] == now + breaker.HALF_OPEN_PROBE_LEASE_SECONDS


def test_schema_check_does_not_commit_active_authority_transaction():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE authority_sentinel (value TEXT NOT NULL)")
    breaker.ensure_schema(conn)
    conn.execute("BEGIN")
    conn.execute("INSERT INTO authority_sentinel(value) VALUES ('uncommitted')")
    breaker.ensure_schema(conn)
    conn.rollback()
    assert conn.execute("SELECT * FROM authority_sentinel").fetchall() == []
