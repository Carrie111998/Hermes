"""Programme-wide task admission and cooperative halt signalling."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from hermes_constants import get_default_hermes_root
from hermes_cli.programme import init as programme_init
from hermes_cli.programme import log as programme_log
from hermes_cli.sqlite_util import retrying_write_txn


VALID_STATES = frozenset({"RUNNING", "PAUSED", "DRAINING", "HALTED"})
HALT_SIGNAL_PATH = get_default_hermes_root() / "signals" / "halt"


@dataclass(frozen=True)
class ProgrammeState:
    state: str
    reason: str | None
    changed_by: str | None
    changed_at: str
    task_count_at_change: int | None


def _row_to_state(row: sqlite3.Row) -> ProgrammeState:
    return ProgrammeState(
        state=str(row["state"]),
        reason=row["reason"],
        changed_by=row["changed_by"],
        changed_at=str(row["changed_at"]),
        task_count_at_change=row["task_count_at_change"],
    )


def _read_state(conn: sqlite3.Connection) -> ProgrammeState:
    row = conn.execute(
        """
        SELECT state, reason, changed_by, changed_at, task_count_at_change
          FROM programme_state
         WHERE id = 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("programme_state singleton row is missing")
    return _row_to_state(row)


def _inflight_count(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
              FROM tasks
             WHERE status IN ('claiming', 'running')
            """
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        return 0
    return int(row["count"] if row is not None else 0)


def get_state(
    db_path: Path | None = None,
    *,
    migrate_if_missing: bool = True,
) -> ProgrammeState:
    conn = programme_init.connect(db_path)
    missing_schema = False
    try:
        return _read_state(conn)
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        missing_schema = True
    finally:
        conn.close()
    if not missing_schema:  # pragma: no cover - defensive
        raise RuntimeError("programme state read failed")
    if not migrate_if_missing:
        raise RuntimeError("programme_state table is missing")
    programme_init.migrate(db_path)
    conn = programme_init.connect(db_path)
    try:
        return _read_state(conn)
    finally:
        conn.close()


def inflight_count() -> int:
    """Count committed or transient worker claims."""
    conn = programme_init.connect()
    try:
        return _inflight_count(conn)
    finally:
        conn.close()


_DEFAULT_INFLIGHT_COUNT_READER = inflight_count


def signal_in_flight_stop() -> None:
    """Create the cooperative halt signal with the time it was raised."""
    path = Path(HALT_SIGNAL_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(programme_init.utc_now() + "\n", encoding="utf-8")


def clear_halt_signal() -> None:
    """Remove the cooperative halt signal if it exists."""
    Path(HALT_SIGNAL_PATH).unlink(missing_ok=True)


def is_halt_signalled() -> bool:
    """Return whether leaves should stop before beginning another attempt."""
    return Path(HALT_SIGNAL_PATH).is_file()


def _write_state(
    conn: sqlite3.Connection,
    *,
    state: str,
    reason: str | None,
    changed_by: str | None,
    task_count_at_change: int,
) -> ProgrammeState:
    changed_at = programme_init.utc_now()
    cursor = conn.execute(
        """
        UPDATE programme_state
           SET state = ?, reason = ?, changed_by = ?, changed_at = ?,
               task_count_at_change = ?
         WHERE id = 1
        """,
        (state, reason, changed_by, changed_at, task_count_at_change),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("programme_state singleton row is missing")
    programme_log.append_state_log(
        conn,
        state=state,
        reason=reason,
        changed_by=changed_by,
        changed_at=changed_at,
        task_count_at_change=task_count_at_change,
    )
    return ProgrammeState(
        state=state,
        reason=reason,
        changed_by=changed_by,
        changed_at=changed_at,
        task_count_at_change=task_count_at_change,
    )


def set_state(
    state: str,
    reason: str | None = None,
    changed_by: str | None = None,
) -> ProgrammeState:
    normalized = str(state).strip().upper()
    if normalized not in VALID_STATES:
        raise ValueError(
            f"invalid programme state {state!r}; expected one of {sorted(VALID_STATES)}"
        )

    conn = programme_init.connect()
    try:
        with retrying_write_txn(conn):
            result = _write_state(
                conn,
                state=normalized,
                reason=reason,
                changed_by=changed_by,
                task_count_at_change=_inflight_count(conn),
            )
    finally:
        conn.close()
    if normalized == "HALTED":
        signal_in_flight_stop()
    elif normalized == "RUNNING":
        clear_halt_signal()
    return result


def admit_task(task_id: str) -> tuple[bool, str]:
    """Admit a new attempt only while the programme is running."""
    del task_id  # The gate is programme-wide; the id is carried by the caller's audit event.
    current = get_state()
    if current.state == "RUNNING":
        return True, "admitted"
    reason = current.reason or "no reason provided"
    return False, f"programme {current.state.lower()}: {reason}"


def check_drain() -> ProgrammeState:
    """Atomically transition DRAINING to PAUSED once no workers remain."""
    conn = programme_init.connect()
    try:
        with retrying_write_txn(conn):
            current = _read_state(conn)
            if current.state != "DRAINING":
                return current

            # Tests/deployments may replace the public reader. The built-in reader
            # uses this transaction's connection so the count and transition share
            # one BEGIN IMMEDIATE snapshot without recursively acquiring a lock.
            count = (
                _inflight_count(conn)
                if inflight_count is _DEFAULT_INFLIGHT_COUNT_READER
                else inflight_count()
            )
            if count != 0:
                return current

            return _write_state(
                conn,
                state="PAUSED",
                reason="drain complete",
                changed_by="system",
                task_count_at_change=0,
            )
    finally:
        conn.close()


__all__ = [
    "HALT_SIGNAL_PATH",
    "ProgrammeState",
    "admit_task",
    "check_drain",
    "clear_halt_signal",
    "get_state",
    "inflight_count",
    "is_halt_signalled",
    "set_state",
    "signal_in_flight_stop",
]
