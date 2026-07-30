"""Durable hard-budget accounting for user-turn routes.

This module intentionally owns storage only.  Route selection cannot mint an
authorization: a caller must first obtain an allowed reservation from this
ledger and attach its opaque provenance to the immutable route decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid
from typing import Callable, Optional, TypeVar

from hermes_constants import get_hermes_home

_SCHEMA_VERSION = 2
_DEFAULT_BUSY_TIMEOUT_SECONDS = 5.0
_DEFAULT_LEASE_SECONDS = 300.0
_BUSY_RETRIES = 3
_T = TypeVar("_T")


class BudgetInvariantError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class BudgetReservation:
    allowed: bool
    reservation_id: Optional[str]
    week_key: str
    turn_id: str
    route_id: str
    slots: int
    state: str
    reason_code: str
    idempotent: bool = False
    expires_at: Optional[float] = None


@dataclass(frozen=True)
class BudgetStatus:
    week_key: str
    weekly_limit: int
    reserved_slots: int
    committed_slots: int
    available_slots: int
    cooldown_scope: Optional[str] = None
    cooldown_reason_code: Optional[str] = None
    cooldown_until_at: Optional[float] = None


@dataclass(frozen=True)
class BudgetAuditRow:
    audit_id: int
    reservation_id: str
    week_key: str
    turn_id: str
    route_id: str
    slots: int
    state: str
    reason_code: str
    provider_submission_id: Optional[str]
    created_at: float


_SAFE_REASON_CODE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_SAFE_SUBMISSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_CREDENTIAL_LIKE_PREFIXES = ("bearer", "ghp_", "github_pat_", "sk-", "sk_", "xox")
_ALLOWED_REFUND_REASONS = frozenset({"provider_explicitly_not_billed"})


def _validate_reason_code(reason_code: str) -> str:
    value = str(reason_code or "")
    if not _SAFE_REASON_CODE.fullmatch(value):
        raise ValueError("reason_code must be a safe reason code")
    return value


def _validate_submission_id(provider_submission_id: str) -> str:
    value = str(provider_submission_id or "")
    if (
        not _SAFE_SUBMISSION_ID.fullmatch(value)
        or value.lower().startswith(_CREDENTIAL_LIKE_PREFIXES)
    ):
        raise ValueError("provider_submission_id must be a safe identifier")
    return value


def normalize_provider_submission_id(provider_submission_id: object) -> str:
    """Return a safe audit identifier without retaining unsafe provider data."""

    try:
        return _validate_submission_id(str(provider_submission_id or ""))
    except ValueError:
        return f"attempt:{uuid.uuid4().hex}"


def utc_week_key(timestamp: Optional[float] = None) -> str:
    """Return the ISO date of Monday 00:00 for a UTC timestamp."""

    instant = datetime.fromtimestamp(
        time.time() if timestamp is None else timestamp,
        tz=timezone.utc,
    )
    monday = (instant - timedelta(days=instant.weekday())).date()
    return monday.isoformat()


class TurnRouterBudgetLedger:
    """Profile-local SQLite ledger with atomic weekly reservations."""

    def __init__(
        self,
        *,
        weekly_limit: int,
        db_path: Optional[Path] = None,
        owner_id: Optional[str] = None,
        busy_timeout_seconds: float = _DEFAULT_BUSY_TIMEOUT_SECONDS,
        lease_seconds: float = _DEFAULT_LEASE_SECONDS,
    ) -> None:
        if weekly_limit < 0:
            raise ValueError("weekly_limit must be non-negative")
        if busy_timeout_seconds <= 0:
            raise ValueError("busy_timeout_seconds must be positive")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")

        self.db_path = Path(db_path) if db_path is not None else get_hermes_home() / "turn_router_budget.db"
        self.weekly_limit = int(weekly_limit)
        self.owner_id = owner_id or f"{os.getpid()}:{uuid.uuid4().hex}"
        self.busy_timeout_seconds = float(busy_timeout_seconds)
        self.lease_seconds = float(lease_seconds)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.db_path),
            timeout=self.busy_timeout_seconds,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_seconds * 1000)}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _is_busy(exc: sqlite3.OperationalError) -> bool:
        message = str(exc).lower()
        return "locked" in message or "busy" in message

    def _with_retry(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        last_error: Optional[sqlite3.OperationalError] = None
        for attempt in range(_BUSY_RETRIES):
            connection = self._connect()
            try:
                return operation(connection)
            except sqlite3.OperationalError as exc:
                if not self._is_busy(exc) or attempt + 1 >= _BUSY_RETRIES:
                    raise
                last_error = exc
                time.sleep(0.025 * (2**attempt))
            finally:
                connection.close()
        assert last_error is not None
        raise last_error

    @staticmethod
    def _validate_existing_schema(connection: sqlite3.Connection) -> None:
        table_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name LIKE 'budget_%'"
            ).fetchall()
        }
        if not table_names:
            return
        if "budget_meta" not in table_names:
            raise BudgetInvariantError("budget_schema_version_missing")
        try:
            row = connection.execute(
                "SELECT value FROM budget_meta WHERE key='schema_version'"
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise BudgetInvariantError("budget_schema_invalid") from exc
        if row is None:
            raise BudgetInvariantError("budget_schema_version_missing")
        try:
            version = int(row[0])
        except (TypeError, ValueError) as exc:
            raise BudgetInvariantError("budget_schema_invalid") from exc
        if version < _SCHEMA_VERSION:
            raise BudgetInvariantError("budget_schema_migration_required")
        if version > _SCHEMA_VERSION:
            raise BudgetInvariantError("budget_schema_newer_than_runtime")

    def _ensure_schema(self) -> None:
        def initialize(connection: sqlite3.Connection) -> None:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._validate_existing_schema(connection)
                for statement in (
                    """
                    CREATE TABLE IF NOT EXISTS budget_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                    """,
                    """
                    CREATE TABLE IF NOT EXISTS budget_reservations (
                        reservation_id TEXT PRIMARY KEY,
                        week_key TEXT NOT NULL,
                        turn_id TEXT NOT NULL,
                        route_id TEXT NOT NULL,
                        slots INTEGER NOT NULL CHECK (slots > 0),
                        state TEXT NOT NULL CHECK (
                            state IN ('reserved', 'committed', 'released', 'refunded', 'expired')
                        ),
                        owner_id TEXT NOT NULL,
                        expires_at REAL,
                        provider_submission_id TEXT UNIQUE,
                        reason_code TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        UNIQUE (week_key, turn_id, route_id)
                    )
                    """,
                    """
                    CREATE INDEX IF NOT EXISTS idx_budget_reservations_capacity
                        ON budget_reservations (week_key, state)
                    """,
                    """
                    CREATE INDEX IF NOT EXISTS idx_budget_reservations_expiry
                        ON budget_reservations (state, expires_at)
                    """,
                    """
                    CREATE TABLE IF NOT EXISTS budget_audit_events (
                        audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        reservation_id TEXT NOT NULL,
                        week_key TEXT NOT NULL,
                        turn_id TEXT NOT NULL,
                        route_id TEXT NOT NULL,
                        slots INTEGER NOT NULL CHECK (slots > 0),
                        state TEXT NOT NULL CHECK (
                            state IN ('reserved', 'committed', 'released', 'refunded', 'expired')
                        ),
                        reason_code TEXT NOT NULL,
                        provider_submission_id TEXT,
                        created_at REAL NOT NULL,
                        FOREIGN KEY (reservation_id)
                            REFERENCES budget_reservations(reservation_id)
                    )
                    """,
                    """
                    CREATE INDEX IF NOT EXISTS idx_budget_audit_reservation
                        ON budget_audit_events (reservation_id, audit_id)
                    """,
                    """
                    CREATE INDEX IF NOT EXISTS idx_budget_audit_week
                        ON budget_audit_events (week_key, audit_id)
                    """,
                    """
                    CREATE TABLE IF NOT EXISTS budget_cooldowns (
                        scope TEXT PRIMARY KEY,
                        reason_code TEXT NOT NULL,
                        until_at REAL NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """,
                ):
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO budget_meta(key, value) VALUES('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(_SCHEMA_VERSION),),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        self._with_retry(initialize)

    @staticmethod
    def _row_to_reservation(row: sqlite3.Row, *, idempotent: bool) -> BudgetReservation:
        state = str(row["state"])
        return BudgetReservation(
            allowed=state in {"reserved", "committed"},
            reservation_id=str(row["reservation_id"]),
            week_key=str(row["week_key"]),
            turn_id=str(row["turn_id"]),
            route_id=str(row["route_id"]),
            slots=int(row["slots"]),
            state=state,
            reason_code=str(row["reason_code"]),
            idempotent=idempotent,
            expires_at=float(row["expires_at"]) if row["expires_at"] is not None else None,
        )

    @staticmethod
    def _append_audit_event(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        state: Optional[str] = None,
        reason_code: Optional[str] = None,
        created_at: float,
    ) -> None:
        connection.execute(
            "INSERT INTO budget_audit_events "
            "(reservation_id, week_key, turn_id, route_id, slots, state, "
            "reason_code, provider_submission_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(row["reservation_id"]),
                str(row["week_key"]),
                str(row["turn_id"]),
                str(row["route_id"]),
                int(row["slots"]),
                state or str(row["state"]),
                reason_code or str(row["reason_code"]),
                (
                    str(row["provider_submission_id"])
                    if row["provider_submission_id"] is not None
                    else None
                ),
                float(created_at),
            ),
        )

    @staticmethod
    def _reap_expired(connection: sqlite3.Connection, *, now: float) -> None:
        rows = connection.execute(
            "SELECT * FROM budget_reservations "
            "WHERE state='reserved' AND expires_at IS NOT NULL AND expires_at <= ?",
            (now,),
        ).fetchall()
        for row in rows:
            connection.execute(
                "UPDATE budget_reservations "
                "SET state='expired', reason_code='lease_expired', updated_at=? "
                "WHERE reservation_id=? AND state='reserved'",
                (now, str(row["reservation_id"])),
            )
            TurnRouterBudgetLedger._append_audit_event(
                connection,
                row,
                state="expired",
                reason_code="lease_expired",
                created_at=now,
            )

    def reserve(
        self,
        *,
        turn_id: str,
        route_id: str,
        slots: int = 1,
        cooldown_scope: Optional[str] = None,
        now: Optional[float] = None,
    ) -> BudgetReservation:
        if not turn_id or not route_id:
            raise ValueError("turn_id and route_id are required")
        if slots <= 0:
            raise ValueError("slots must be positive")

        current = time.time() if now is None else float(now)
        week = utc_week_key(current)

        def transaction(connection: sqlite3.Connection) -> BudgetReservation:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._reap_expired(connection, now=current)
                existing = connection.execute(
                    "SELECT * FROM budget_reservations "
                    "WHERE week_key=? AND turn_id=? AND route_id=?",
                    (week, turn_id, route_id),
                ).fetchone()
                if existing is not None:
                    result = self._row_to_reservation(existing, idempotent=True)
                    connection.commit()
                    return result

                if cooldown_scope:
                    connection.execute(
                        "DELETE FROM budget_cooldowns WHERE scope=? AND until_at <= ?",
                        (cooldown_scope, current),
                    )
                    cooldown = connection.execute(
                        "SELECT reason_code FROM budget_cooldowns "
                        "WHERE scope=? AND until_at > ?",
                        (cooldown_scope, current),
                    ).fetchone()
                    if cooldown is not None:
                        connection.commit()
                        return BudgetReservation(
                            allowed=False,
                            reservation_id=None,
                            week_key=week,
                            turn_id=turn_id,
                            route_id=route_id,
                            slots=slots,
                            state="denied",
                            reason_code=str(cooldown["reason_code"]),
                        )

                used = int(
                    connection.execute(
                        "SELECT COALESCE(SUM(slots), 0) FROM budget_reservations "
                        "WHERE week_key=? AND state IN ('reserved', 'committed')",
                        (week,),
                    ).fetchone()[0]
                )
                if used + slots > self.weekly_limit:
                    connection.commit()
                    return BudgetReservation(
                        allowed=False,
                        reservation_id=None,
                        week_key=week,
                        turn_id=turn_id,
                        route_id=route_id,
                        slots=slots,
                        state="denied",
                        reason_code="weekly_budget_exhausted",
                    )

                reservation_id = uuid.uuid4().hex
                expires_at = current + self.lease_seconds
                connection.execute(
                    "INSERT INTO budget_reservations "
                    "(reservation_id, week_key, turn_id, route_id, slots, state, "
                    "owner_id, expires_at, reason_code, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 'reserved', ?, ?, 'reserved', ?, ?)",
                    (
                        reservation_id,
                        week,
                        turn_id,
                        route_id,
                        int(slots),
                        self.owner_id,
                        expires_at,
                        current,
                        current,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM budget_reservations WHERE reservation_id=?",
                    (reservation_id,),
                ).fetchone()
                assert row is not None
                self._append_audit_event(connection, row, created_at=current)
                connection.commit()
                return self._row_to_reservation(row, idempotent=False)
            except Exception:
                connection.rollback()
                raise

        return self._with_retry(transaction)

    def get(self, reservation_id: str) -> BudgetReservation:
        def read(connection: sqlite3.Connection) -> BudgetReservation:
            row = connection.execute(
                "SELECT * FROM budget_reservations WHERE reservation_id=?",
                (reservation_id,),
            ).fetchone()
            if row is None:
                raise BudgetInvariantError("reservation_not_found")
            return self._row_to_reservation(row, idempotent=False)

        return self._with_retry(read)

    def audit_rows(
        self,
        *,
        reservation_id: Optional[str] = None,
    ) -> tuple[BudgetAuditRow, ...]:
        """Return safe append-only transition records in insertion order."""

        def read(connection: sqlite3.Connection) -> tuple[BudgetAuditRow, ...]:
            if reservation_id is None:
                rows = connection.execute(
                    "SELECT * FROM budget_audit_events ORDER BY audit_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM budget_audit_events "
                    "WHERE reservation_id=? ORDER BY audit_id",
                    (reservation_id,),
                ).fetchall()
            return tuple(
                BudgetAuditRow(
                    audit_id=int(row["audit_id"]),
                    reservation_id=str(row["reservation_id"]),
                    week_key=str(row["week_key"]),
                    turn_id=str(row["turn_id"]),
                    route_id=str(row["route_id"]),
                    slots=int(row["slots"]),
                    state=str(row["state"]),
                    reason_code=str(row["reason_code"]),
                    provider_submission_id=(
                        str(row["provider_submission_id"])
                        if row["provider_submission_id"] is not None
                        else None
                    ),
                    created_at=float(row["created_at"]),
                )
                for row in rows
            )

        return self._with_retry(read)

    def commit(
        self,
        reservation_id: str,
        *,
        provider_submission_id: str,
        now: Optional[float] = None,
    ) -> BudgetReservation:
        provider_submission_id = _validate_submission_id(provider_submission_id)
        current = time.time() if now is None else float(now)

        def transaction(connection: sqlite3.Connection) -> BudgetReservation:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM budget_reservations WHERE reservation_id=?",
                    (reservation_id,),
                ).fetchone()
                if row is None:
                    raise BudgetInvariantError("reservation_not_found")
                if row["state"] == "committed":
                    if row["provider_submission_id"] != provider_submission_id:
                        raise BudgetInvariantError("provider_submission_conflict")
                    connection.commit()
                    return self._row_to_reservation(row, idempotent=True)
                if row["state"] != "reserved":
                    raise BudgetInvariantError("invalid_commit_transition")
                try:
                    connection.execute(
                        "UPDATE budget_reservations SET state='committed', "
                        "provider_submission_id=?, expires_at=NULL, "
                        "reason_code='provider_accepted', updated_at=? "
                        "WHERE reservation_id=?",
                        (provider_submission_id, current, reservation_id),
                    )
                except sqlite3.IntegrityError as exc:
                    raise BudgetInvariantError("provider_submission_conflict") from exc
                updated = connection.execute(
                    "SELECT * FROM budget_reservations WHERE reservation_id=?",
                    (reservation_id,),
                ).fetchone()
                assert updated is not None
                self._append_audit_event(connection, updated, created_at=current)
                connection.commit()
                return self._row_to_reservation(updated, idempotent=False)
            except Exception:
                connection.rollback()
                raise

        return self._with_retry(transaction)

    def _terminal_transition(
        self,
        reservation_id: str,
        *,
        from_state: str,
        to_state: str,
        reason_code: str,
        now: Optional[float],
    ) -> BudgetReservation:
        reason_code = _validate_reason_code(reason_code)
        current = time.time() if now is None else float(now)

        def transaction(connection: sqlite3.Connection) -> BudgetReservation:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM budget_reservations WHERE reservation_id=?",
                    (reservation_id,),
                ).fetchone()
                if row is None:
                    raise BudgetInvariantError("reservation_not_found")
                if row["state"] == to_state:
                    connection.commit()
                    return self._row_to_reservation(row, idempotent=True)
                if row["state"] != from_state:
                    raise BudgetInvariantError(f"invalid_{to_state}_transition")
                connection.execute(
                    "UPDATE budget_reservations SET state=?, expires_at=NULL, "
                    "reason_code=?, updated_at=? WHERE reservation_id=?",
                    (to_state, reason_code, current, reservation_id),
                )
                updated = connection.execute(
                    "SELECT * FROM budget_reservations WHERE reservation_id=?",
                    (reservation_id,),
                ).fetchone()
                assert updated is not None
                self._append_audit_event(connection, updated, created_at=current)
                connection.commit()
                return self._row_to_reservation(updated, idempotent=False)
            except Exception:
                connection.rollback()
                raise

        return self._with_retry(transaction)

    def release(
        self,
        reservation_id: str,
        *,
        reason_code: str,
        now: Optional[float] = None,
    ) -> BudgetReservation:
        return self._terminal_transition(
            reservation_id,
            from_state="reserved",
            to_state="released",
            reason_code=reason_code,
            now=now,
        )

    def refund(
        self,
        reservation_id: str,
        *,
        reason_code: str,
        now: Optional[float] = None,
    ) -> BudgetReservation:
        if reason_code not in _ALLOWED_REFUND_REASONS:
            raise BudgetInvariantError("refund_reason_not_allowed")
        return self._terminal_transition(
            reservation_id,
            from_state="committed",
            to_state="refunded",
            reason_code=reason_code,
            now=now,
        )

    def set_cooldown(
        self,
        *,
        scope: str,
        reason_code: str,
        until_at: float,
        now: Optional[float] = None,
    ) -> None:
        if not scope:
            raise ValueError("scope is required")
        reason_code = _validate_reason_code(reason_code)
        current = time.time() if now is None else float(now)
        if until_at <= current:
            raise ValueError("until_at must be in the future")

        def transaction(connection: sqlite3.Connection) -> None:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO budget_cooldowns "
                    "(scope, reason_code, until_at, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(scope) DO UPDATE SET "
                    "reason_code=excluded.reason_code, "
                    "until_at=MAX(budget_cooldowns.until_at, excluded.until_at), "
                    "updated_at=excluded.updated_at",
                    (scope, reason_code, float(until_at), current, current),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        self._with_retry(transaction)

    def status(
        self,
        *,
        now: Optional[float] = None,
        cooldown_scope: Optional[str] = None,
    ) -> BudgetStatus:
        current = time.time() if now is None else float(now)
        week = utc_week_key(current)
        scope = str(cooldown_scope or "").strip() or None

        def transaction(connection: sqlite3.Connection) -> BudgetStatus:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._reap_expired(connection, now=current)
                row = connection.execute(
                    "SELECT "
                    "COALESCE(SUM(CASE WHEN state='reserved' THEN slots ELSE 0 END), 0), "
                    "COALESCE(SUM(CASE WHEN state='committed' THEN slots ELSE 0 END), 0) "
                    "FROM budget_reservations WHERE week_key=?",
                    (week,),
                ).fetchone()
                reserved = int(row[0])
                committed = int(row[1])
                cooldown_reason_code = None
                cooldown_until_at = None
                if scope is not None:
                    connection.execute(
                        "DELETE FROM budget_cooldowns WHERE scope=? AND until_at <= ?",
                        (scope, current),
                    )
                    cooldown = connection.execute(
                        "SELECT reason_code, until_at FROM budget_cooldowns "
                        "WHERE scope=? AND until_at > ?",
                        (scope, current),
                    ).fetchone()
                    if cooldown is not None:
                        cooldown_reason_code = str(cooldown[0])
                        cooldown_until_at = float(cooldown[1])
                connection.commit()
                return BudgetStatus(
                    week_key=week,
                    weekly_limit=self.weekly_limit,
                    reserved_slots=reserved,
                    committed_slots=committed,
                    available_slots=max(0, self.weekly_limit - reserved - committed),
                    cooldown_scope=scope,
                    cooldown_reason_code=cooldown_reason_code,
                    cooldown_until_at=cooldown_until_at,
                )
            except Exception:
                connection.rollback()
                raise

        return self._with_retry(transaction)
