"""Durable rolling failure circuit breakers for external action classes."""

from __future__ import annotations

import sqlite3
import time


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS operation_circuit_breakers (
    operation_key TEXT PRIMARY KEY, consecutive_failures INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'closed', opened_at INTEGER, retry_after INTEGER,
    last_error TEXT, updated_at INTEGER NOT NULL
);
"""


class CircuitOpenError(RuntimeError):
    pass


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def assert_admissible(conn: sqlite3.Connection, operation_key: str) -> None:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT state, retry_after FROM operation_circuit_breakers WHERE operation_key = ?",
        (operation_key,),
    ).fetchone()
    if row is None or row["state"] == "closed":
        return
    now = int(time.time())
    if row["retry_after"] is not None and int(row["retry_after"]) <= now:
        with conn:
            conn.execute(
                """UPDATE operation_circuit_breakers
                   SET state = 'half_open', updated_at = ? WHERE operation_key = ?""",
                (now, operation_key),
            )
        return
    raise CircuitOpenError(f"circuit is open for {operation_key}")


def record_outcome(
    conn: sqlite3.Connection,
    *,
    operation_key: str,
    succeeded: bool,
    failure_threshold: int,
    cooldown_seconds: int,
    error: str = "",
) -> str:
    if failure_threshold < 1 or cooldown_seconds < 1:
        raise ValueError("circuit breaker threshold and cooldown must be positive")
    ensure_schema(conn)
    now = int(time.time())
    row = conn.execute(
        "SELECT consecutive_failures FROM operation_circuit_breakers WHERE operation_key = ?",
        (operation_key,),
    ).fetchone()
    failures = 0 if succeeded else (int(row["consecutive_failures"]) if row else 0) + 1
    state = "closed" if succeeded or failures < failure_threshold else "open"
    retry_after = now + cooldown_seconds if state == "open" else None
    with conn:
        conn.execute(
            """INSERT INTO operation_circuit_breakers
               (operation_key, consecutive_failures, state, opened_at,
                retry_after, last_error, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(operation_key) DO UPDATE SET
                 consecutive_failures=excluded.consecutive_failures,
                 state=excluded.state, opened_at=excluded.opened_at,
                 retry_after=excluded.retry_after, last_error=excluded.last_error,
                 updated_at=excluded.updated_at""",
            (
                operation_key, failures, state, now if state == "open" else None,
                retry_after, error or None, now,
            ),
        )
    return state
