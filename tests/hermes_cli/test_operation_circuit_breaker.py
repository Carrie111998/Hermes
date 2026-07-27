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
