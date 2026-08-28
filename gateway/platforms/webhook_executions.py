"""Authenticated execution control projected from the webhook operation ledger.

The operation ledger remains the only owner of webhook lifecycle state.  This
module adds a small, operation-owned capability record to the *same* SQLite
database and projects status by joining that record back to
``webhook_operations``/``webhook_targets``.  It deliberately does not maintain
an independent accepted/running/completed state machine.

Cancellation is also a request, not a claimed outcome.  A caller can durably
request cancellation for one exact operation generation; the runtime must
observe that request and perform the existing ledger-owned transition.  Once a
target attempt has started, cancellation is too late and this module never
pretends otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import hmac
import math
import secrets
import sqlite3
import time
from typing import Any, Optional

from gateway.platforms.webhook_ledger import (
    OperationAuthority,
    OperationState,
    TargetState,
    WebhookLedgerCorruptionError,
    WebhookLedgerError,
    WebhookOperationLedger,
)


_SCHEMA_NAME = "webhook_execution_projection"
_SCHEMA_VERSION = 1
_CAPABILITY_TABLE = "webhook_execution_capabilities"
_META_TABLE = "webhook_execution_projection_meta"

DEFAULT_CAPABILITY_TTL_SECONDS = 60 * 60
MINIMUM_CAPABILITY_TTL_SECONDS = 60
MAXIMUM_CAPABILITY_TTL_SECONDS = 30 * 24 * 60 * 60
DEFAULT_AUTH_WINDOW_SECONDS = 60
DEFAULT_AUTH_MAX_ATTEMPTS = 60
MAXIMUM_AUTH_MAX_ATTEMPTS = 10_000
MAXIMUM_PRUNE_BATCH = 128


class ExecutionPhase(str, Enum):
    """Stable public phase derived from the durable operation state."""

    ACCEPTED = "accepted"
    RUNNING = "running"
    DELIVERY = "delivery"
    SETTLED = "settled"
    INDETERMINATE = "indeterminate"


class CancellationState(str, Enum):
    """State of the remote cancellation *request*, not the operation."""

    NONE = "none"
    REQUESTED = "requested"
    OBSERVED = "observed"
    STALE = "stale"


class CancelDisposition(str, Enum):
    """Result of an authenticated cancellation request."""

    REQUESTED = "requested"
    ALREADY_REQUESTED = "already_requested"
    OBSERVED = "observed"
    TOO_LATE = "too_late"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class IssuedExecutionCapability:
    """One public execution handle.

    ``access_token`` is returned only for the first successful issuance.  A
    repeated call returns the existing execution ID with ``access_token=None``;
    plaintext capability recovery is intentionally impossible.
    """

    execution_id: str
    access_token: Optional[str]
    created: bool
    expires_at: float


@dataclass(frozen=True)
class ExecutionStatus:
    """Redacted public projection of one operation-owned execution."""

    execution_id: str
    phase: ExecutionPhase
    operation_state: OperationState
    target_state: Optional[TargetState]
    generation: int
    cancellation: CancellationState
    can_cancel: bool
    effects_possible: bool
    created_at: float
    updated_at: float
    settled_at: Optional[float]
    needs_attention: bool


@dataclass(frozen=True)
class CancelRequestResult:
    """Authenticated cancellation decision plus the current public status."""

    disposition: CancelDisposition
    status: ExecutionStatus


def _finite_time(value: Any, *, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise WebhookLedgerCorruptionError(f"{label} is invalid") from exc
    if not math.isfinite(result):
        raise WebhookLedgerCorruptionError(f"{label} is invalid")
    return result


def _phase_for_state(state: OperationState) -> ExecutionPhase:
    if state in {OperationState.PREPARING, OperationState.READY}:
        return ExecutionPhase.ACCEPTED
    if state is OperationState.RUNNING:
        return ExecutionPhase.RUNNING
    if state in {OperationState.DELIVERY_READY, OperationState.DELIVERING}:
        return ExecutionPhase.DELIVERY
    if state is OperationState.SETTLED:
        return ExecutionPhase.SETTLED
    return ExecutionPhase.INDETERMINATE


def _is_cancellable(state: OperationState) -> bool:
    return state in {
        OperationState.PREPARING,
        OperationState.READY,
        OperationState.RUNNING,
        OperationState.DELIVERY_READY,
    }


class WebhookExecutionProjection:
    """Capability/status/cancel projection over ``WebhookOperationLedger``.

    All reads and writes share the ledger's lock and SQLite transaction
    boundary.  The added table contains only capability and cancellation
    request material; operation/target state is always read from the canonical
    ledger tables.
    """

    def __init__(
        self,
        ledger: WebhookOperationLedger,
        *,
        capability_ttl_seconds: int = DEFAULT_CAPABILITY_TTL_SECONDS,
        auth_window_seconds: int = DEFAULT_AUTH_WINDOW_SECONDS,
        auth_max_attempts: int = DEFAULT_AUTH_MAX_ATTEMPTS,
    ) -> None:
        if not isinstance(ledger, WebhookOperationLedger):
            raise TypeError("ledger must be a WebhookOperationLedger")
        if (
            not isinstance(capability_ttl_seconds, int)
            or isinstance(capability_ttl_seconds, bool)
            or capability_ttl_seconds < MINIMUM_CAPABILITY_TTL_SECONDS
            or capability_ttl_seconds > MAXIMUM_CAPABILITY_TTL_SECONDS
        ):
            raise ValueError("capability_ttl_seconds is outside the supported range")
        if (
            not isinstance(auth_window_seconds, int)
            or isinstance(auth_window_seconds, bool)
            or auth_window_seconds < 1
        ):
            raise ValueError("auth_window_seconds must be positive")
        if (
            not isinstance(auth_max_attempts, int)
            or isinstance(auth_max_attempts, bool)
            or auth_max_attempts < 1
            or auth_max_attempts > MAXIMUM_AUTH_MAX_ATTEMPTS
        ):
            raise ValueError("auth_max_attempts is outside the supported range")
        self.ledger = ledger
        self.capability_ttl_seconds = capability_ttl_seconds
        self.auth_window_seconds = auth_window_seconds
        self.auth_max_attempts = auth_max_attempts
        self.migrate_schema()

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_public_text(value: Any, *, label: str, maximum: int = 1024) -> str:
        if not isinstance(value, str):
            raise WebhookLedgerError(f"{label} must be text")
        normalized = value.strip()
        if not normalized or len(normalized.encode("utf-8")) > maximum:
            raise WebhookLedgerError(f"{label} is outside the supported range")
        return normalized

    def migrate_schema(self) -> None:
        """Install the v1 projection schema or validate an existing one.

        The method is intentionally idempotent and fail-closed.  A partially
        present or unknown-version schema is treated as corruption rather than
        silently shadowed with a second authority table.
        """

        with self.ledger._lock, self.ledger._transaction() as conn:
            tables = {
                str(row["name"])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            has_capabilities = _CAPABILITY_TABLE in tables
            has_meta = _META_TABLE in tables
            if has_capabilities != has_meta:
                raise WebhookLedgerCorruptionError(
                    "webhook execution projection schema is incomplete"
                )
            if has_meta:
                rows = conn.execute(
                    f"SELECT schema_name, schema_version FROM {_META_TABLE}"
                ).fetchall()
                try:
                    schema_is_current = (
                        len(rows) == 1
                        and rows[0]["schema_name"] == _SCHEMA_NAME
                        and int(rows[0]["schema_version"]) == _SCHEMA_VERSION
                    )
                except (TypeError, ValueError, OverflowError):
                    schema_is_current = False
                if not schema_is_current:
                    raise WebhookLedgerCorruptionError(
                        "webhook execution projection schema version is unsupported"
                    )

            conn.execute(
                f"""CREATE TABLE IF NOT EXISTS {_META_TABLE} (
                    schema_name TEXT PRIMARY KEY CHECK (
                        schema_name='{_SCHEMA_NAME}'
                    ),
                    schema_version INTEGER NOT NULL CHECK (
                        schema_version={_SCHEMA_VERSION}
                    )
                )"""
            )
            conn.execute(
                f"""INSERT OR IGNORE INTO {_META_TABLE}
                    (schema_name, schema_version) VALUES (?, ?)""",
                (_SCHEMA_NAME, _SCHEMA_VERSION),
            )
            conn.execute(
                f"""CREATE TABLE IF NOT EXISTS {_CAPABILITY_TABLE} (
                    execution_id TEXT PRIMARY KEY CHECK (
                        length(CAST(execution_id AS BLOB)) BETWEEN 20 AND 128
                    ),
                    operation_id TEXT NOT NULL UNIQUE CHECK (
                        length(CAST(operation_id AS BLOB)) BETWEEN 1 AND 1024
                    ),
                    issued_generation INTEGER NOT NULL CHECK (
                        issued_generation >= 1
                    ),
                    token_hash TEXT NOT NULL UNIQUE CHECK (
                        length(CAST(token_hash AS BLOB))=64
                    ),
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL CHECK (expires_at > created_at),
                    cancel_requested_at REAL,
                    cancel_requested_generation INTEGER CHECK (
                        cancel_requested_generation IS NULL OR
                        cancel_requested_generation >= 1
                    ),
                    cancel_observed_at REAL,
                    auth_window_started_at REAL NOT NULL,
                    auth_attempts INTEGER NOT NULL DEFAULT 0 CHECK (
                        auth_attempts >= 0
                    ),
                    CHECK (
                        (cancel_requested_at IS NULL AND
                         cancel_requested_generation IS NULL AND
                         cancel_observed_at IS NULL)
                        OR
                        (cancel_requested_at IS NOT NULL AND
                         cancel_requested_generation IS NOT NULL)
                    ),
                    CHECK (
                        cancel_observed_at IS NULL OR
                        cancel_requested_at IS NOT NULL
                    ),
                    FOREIGN KEY(operation_id)
                        REFERENCES webhook_operations(operation_id)
                        ON DELETE CASCADE
                )"""
            )
            conn.execute(
                f"""CREATE INDEX IF NOT EXISTS
                    idx_webhook_execution_capabilities_expires
                    ON {_CAPABILITY_TABLE}(expires_at, execution_id)"""
            )
            conn.execute(
                f"""CREATE TRIGGER IF NOT EXISTS
                    trg_webhook_execution_capability_authority_immutable
                    BEFORE UPDATE OF execution_id, operation_id,
                        issued_generation, token_hash, created_at, expires_at
                    ON {_CAPABILITY_TABLE}
                    BEGIN
                        SELECT RAISE(
                            ABORT,
                            'webhook_execution_capability_authority_immutable'
                        );
                    END"""
            )
            self._validate_schema(conn)

    @staticmethod
    def _validate_schema(conn: sqlite3.Connection) -> None:
        capability_info = conn.execute(
            f"PRAGMA table_info({_CAPABILITY_TABLE})"
        ).fetchall()
        expected_columns = (
            "execution_id",
            "operation_id",
            "issued_generation",
            "token_hash",
            "created_at",
            "expires_at",
            "cancel_requested_at",
            "cancel_requested_generation",
            "cancel_observed_at",
            "auth_window_started_at",
            "auth_attempts",
        )
        if tuple(row["name"] for row in capability_info) != expected_columns:
            raise WebhookLedgerCorruptionError(
                "webhook execution projection schema is incompatible"
            )
        if [int(row["pk"]) for row in capability_info] != [1] + [0] * 10:
            raise WebhookLedgerCorruptionError(
                "webhook execution projection primary key is incompatible"
            )
        meta_info = conn.execute(f"PRAGMA table_info({_META_TABLE})").fetchall()
        if tuple(row["name"] for row in meta_info) != (
            "schema_name",
            "schema_version",
        ) or [int(row["pk"]) for row in meta_info] != [1, 0]:
            raise WebhookLedgerCorruptionError(
                "webhook execution projection metadata is incompatible"
            )
        foreign_keys = conn.execute(
            f"PRAGMA foreign_key_list({_CAPABILITY_TABLE})"
        ).fetchall()
        if not any(
            row["table"] == "webhook_operations"
            and row["from"] == "operation_id"
            and row["to"] == "operation_id"
            and str(row["on_delete"]).upper() == "CASCADE"
            for row in foreign_keys
        ):
            raise WebhookLedgerCorruptionError(
                "webhook execution projection ownership constraint is unavailable"
            )
        indexes = {
            row["name"]: row
            for row in conn.execute(f"PRAGMA index_list({_CAPABILITY_TABLE})")
        }
        expiry = indexes.get("idx_webhook_execution_capabilities_expires")
        if (
            expiry is None
            or bool(expiry["unique"])
            or bool(expiry["partial"])
            or tuple(
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM pragma_index_info(?)",
                    ("idx_webhook_execution_capabilities_expires",),
                )
            )
            != ("expires_at", "execution_id")
        ):
            raise WebhookLedgerCorruptionError(
                "webhook execution expiry index is unavailable"
            )
        unique_columns = {
            tuple(
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM pragma_index_info(?)", (name,)
                )
            )
            for name, index in indexes.items()
            if bool(index["unique"]) and not bool(index["partial"])
        }
        if ("operation_id",) not in unique_columns or (
            "token_hash",
        ) not in unique_columns:
            raise WebhookLedgerCorruptionError(
                "webhook execution capability uniqueness is unavailable"
            )
        trigger = conn.execute(
            """SELECT tbl_name FROM sqlite_master
                 WHERE type='trigger'
                   AND name='trg_webhook_execution_capability_authority_immutable'"""
        ).fetchone()
        if trigger is None or trigger["tbl_name"] != _CAPABILITY_TABLE:
            raise WebhookLedgerCorruptionError(
                "webhook execution capability immutability guard is unavailable"
            )

    @staticmethod
    def _operation_join(
        conn: sqlite3.Connection, *, execution_id: str
    ) -> Optional[sqlite3.Row]:
        return conn.execute(
            f"""SELECT
                    c.execution_id, c.operation_id, c.issued_generation,
                    c.token_hash, c.created_at AS capability_created_at,
                    c.expires_at, c.cancel_requested_at,
                    c.cancel_requested_generation, c.cancel_observed_at,
                    c.auth_window_started_at, c.auth_attempts,
                    o.profile, o.route, o.state, o.generation,
                    o.script_started, o.created_at, o.updated_at,
                    o.settled_at, o.last_error,
                    t.state AS target_state, t.last_error AS target_error
               FROM {_CAPABILITY_TABLE} AS c
               JOIN webhook_operations AS o
                 ON o.operation_id=c.operation_id
               LEFT JOIN webhook_targets AS t
                 ON t.operation_id=o.operation_id
              WHERE c.execution_id=?""",
            (execution_id,),
        ).fetchone()

    @staticmethod
    def _status_from_row(row: sqlite3.Row) -> ExecutionStatus:
        try:
            operation_state = OperationState(row["state"])
            target_state = (
                TargetState(row["target_state"])
                if row["target_state"] is not None
                else None
            )
            generation = int(row["generation"])
            cancel_generation = (
                int(row["cancel_requested_generation"])
                if row["cancel_requested_generation"] is not None
                else None
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise WebhookLedgerCorruptionError(
                "webhook execution projection contains invalid state"
            ) from exc
        if generation < 1:
            raise WebhookLedgerCorruptionError(
                "webhook execution projection contains invalid generation"
            )
        if row["cancel_requested_at"] is None:
            cancellation = CancellationState.NONE
        elif cancel_generation != generation:
            cancellation = CancellationState.STALE
        elif row["cancel_observed_at"] is not None:
            cancellation = CancellationState.OBSERVED
        else:
            cancellation = CancellationState.REQUESTED
        created_at = _finite_time(row["created_at"], label="operation created_at")
        updated_at = _finite_time(row["updated_at"], label="operation updated_at")
        settled_at = (
            _finite_time(row["settled_at"], label="operation settled_at")
            if row["settled_at"] is not None
            else None
        )
        effects_possible = bool(row["script_started"]) or operation_state in {
            OperationState.RUNNING,
            OperationState.DELIVERY_READY,
            OperationState.DELIVERING,
            OperationState.SETTLED,
            OperationState.INDETERMINATE,
        }
        return ExecutionStatus(
            execution_id=str(row["execution_id"]),
            phase=_phase_for_state(operation_state),
            operation_state=operation_state,
            target_state=target_state,
            generation=generation,
            cancellation=cancellation,
            can_cancel=_is_cancellable(operation_state),
            effects_possible=effects_possible,
            created_at=created_at,
            updated_at=updated_at,
            settled_at=settled_at,
            needs_attention=(
                operation_state is OperationState.INDETERMINATE
                or row["last_error"] is not None
                or row["target_error"] is not None
            ),
        )

    def _prune_expired_in_transaction(
        self, conn: sqlite3.Connection, *, now: float, limit: int
    ) -> int:
        cursor = conn.execute(
            f"""DELETE FROM {_CAPABILITY_TABLE}
                 WHERE execution_id IN (
                    SELECT execution_id FROM {_CAPABILITY_TABLE}
                     WHERE expires_at <= ?
                     ORDER BY expires_at, execution_id
                     LIMIT ?
                 )""",
            (now, limit),
        )
        return max(0, int(cursor.rowcount))

    def prune_expired(
        self, *, now: Optional[float] = None, limit: int = MAXIMUM_PRUNE_BATCH
    ) -> int:
        """Delete a bounded page of expired capabilities, never operations."""

        timestamp = (
            time.time() if now is None else _finite_time(now, label="prune time")
        )
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= MAXIMUM_PRUNE_BATCH
        ):
            raise ValueError("limit is outside the supported range")
        with self.ledger._lock, self.ledger._transaction() as conn:
            return self._prune_expired_in_transaction(conn, now=timestamp, limit=limit)

    def issue(
        self,
        authority: OperationAuthority,
        *,
        now: Optional[float] = None,
    ) -> IssuedExecutionCapability:
        """Issue one non-recoverable bearer capability for an operation."""

        if not isinstance(authority, OperationAuthority):
            raise TypeError("authority must be an OperationAuthority")
        timestamp = (
            time.time() if now is None else _finite_time(now, label="issue time")
        )
        expires_at = _finite_time(
            timestamp + self.capability_ttl_seconds,
            label="capability expires_at",
        )
        with self.ledger._lock, self.ledger._transaction() as conn:
            self._prune_expired_in_transaction(
                conn,
                now=timestamp,
                limit=MAXIMUM_PRUNE_BATCH,
            )
            operation = conn.execute(
                """SELECT operation_id, profile, route, generation
                     FROM webhook_operations WHERE operation_id=?""",
                (authority.operation_id,),
            ).fetchone()
            if operation is None:
                raise WebhookLedgerError("execution operation does not exist")
            if (
                operation["profile"] != authority.profile
                or operation["route"] != authority.route
                or int(operation["generation"]) != authority.generation
            ):
                raise WebhookLedgerError("execution authority is stale")
            existing = conn.execute(
                f"""SELECT execution_id, expires_at
                      FROM {_CAPABILITY_TABLE} WHERE operation_id=?""",
                (authority.operation_id,),
            ).fetchone()
            if existing is not None:
                return IssuedExecutionCapability(
                    execution_id=str(existing["execution_id"]),
                    access_token=None,
                    created=False,
                    expires_at=_finite_time(
                        existing["expires_at"], label="capability expires_at"
                    ),
                )

            for _attempt in range(8):
                execution_id = secrets.token_urlsafe(24)
                access_token = secrets.token_urlsafe(32)
                try:
                    conn.execute(
                        f"""INSERT INTO {_CAPABILITY_TABLE} (
                                execution_id, operation_id, issued_generation,
                                token_hash, created_at, expires_at,
                                auth_window_started_at, auth_attempts
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
                        (
                            execution_id,
                            authority.operation_id,
                            authority.generation,
                            self._token_hash(access_token),
                            timestamp,
                            expires_at,
                            timestamp,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    message = str(exc)
                    if "execution_id" in message or "token_hash" in message:
                        continue
                    raise
                return IssuedExecutionCapability(
                    execution_id=execution_id,
                    access_token=access_token,
                    created=True,
                    expires_at=expires_at,
                )
        raise WebhookLedgerError("could not allocate a unique execution capability")

    def _authorize_row(
        self,
        conn: sqlite3.Connection,
        *,
        execution_id: str,
        token: str,
        profile: str,
        route: str,
        now: float,
    ) -> Optional[sqlite3.Row]:
        row = self._operation_join(conn, execution_id=execution_id)
        if row is None:
            return None
        expires_at = _finite_time(row["expires_at"], label="capability expires_at")
        if expires_at <= now:
            return None
        window_started = _finite_time(
            row["auth_window_started_at"], label="authorization window"
        )
        try:
            attempts = int(row["auth_attempts"])
        except (TypeError, ValueError, OverflowError) as exc:
            raise WebhookLedgerCorruptionError(
                "webhook execution authorization counter is invalid"
            ) from exc
        if attempts < 0:
            raise WebhookLedgerCorruptionError(
                "webhook execution authorization counter is invalid"
            )
        if now - window_started >= self.auth_window_seconds:
            window_started = now
            attempts = 0
        if attempts >= self.auth_max_attempts:
            return None
        updated = conn.execute(
            f"""UPDATE {_CAPABILITY_TABLE}
                   SET auth_window_started_at=?, auth_attempts=?
                 WHERE execution_id=?""",
            (window_started, attempts + 1, execution_id),
        )
        if updated.rowcount != 1:
            raise WebhookLedgerError("execution capability disappeared")
        supplied_hash = self._token_hash(token)
        token_ok = hmac.compare_digest(supplied_hash, str(row["token_hash"]))
        scope_ok = row["profile"] == profile and row["route"] == route
        return row if token_ok and scope_ok else None

    def status(
        self,
        execution_id: str,
        token: str,
        *,
        profile: str,
        route: str,
        now: Optional[float] = None,
    ) -> Optional[ExecutionStatus]:
        """Return an authenticated, redacted status projection.

        Unknown IDs, wrong scopes, expired/rate-limited capabilities, and bad
        tokens all return ``None`` so an HTTP facade can map them to the same
        non-enumerable 404 response.
        """

        try:
            execution = self._normalize_public_text(
                execution_id, label="execution_id", maximum=128
            )
            supplied_token = self._normalize_public_text(
                token, label="execution token", maximum=1024
            )
            scope_profile = self._normalize_public_text(profile, label="profile")
            scope_route = self._normalize_public_text(route, label="route")
        except WebhookLedgerError:
            return None
        timestamp = (
            time.time() if now is None else _finite_time(now, label="status time")
        )
        with self.ledger._lock, self.ledger._transaction() as conn:
            row = self._authorize_row(
                conn,
                execution_id=execution,
                token=supplied_token,
                profile=scope_profile,
                route=scope_route,
                now=timestamp,
            )
            return self._status_from_row(row) if row is not None else None

    def request_cancel(
        self,
        execution_id: str,
        token: str,
        *,
        profile: str,
        route: str,
        now: Optional[float] = None,
    ) -> Optional[CancelRequestResult]:
        """Durably request cancellation for the operation's current generation."""

        try:
            execution = self._normalize_public_text(
                execution_id, label="execution_id", maximum=128
            )
            supplied_token = self._normalize_public_text(
                token, label="execution token", maximum=1024
            )
            scope_profile = self._normalize_public_text(profile, label="profile")
            scope_route = self._normalize_public_text(route, label="route")
        except WebhookLedgerError:
            return None
        timestamp = (
            time.time() if now is None else _finite_time(now, label="cancel time")
        )
        with self.ledger._lock, self.ledger._transaction() as conn:
            row = self._authorize_row(
                conn,
                execution_id=execution,
                token=supplied_token,
                profile=scope_profile,
                route=scope_route,
                now=timestamp,
            )
            if row is None:
                return None
            state = OperationState(row["state"])
            generation = int(row["generation"])
            if state is OperationState.DELIVERING:
                disposition = CancelDisposition.TOO_LATE
            elif state in {OperationState.SETTLED, OperationState.INDETERMINATE}:
                disposition = CancelDisposition.TERMINAL
            else:
                requested_generation = (
                    int(row["cancel_requested_generation"])
                    if row["cancel_requested_generation"] is not None
                    else None
                )
                if requested_generation == generation:
                    disposition = (
                        CancelDisposition.OBSERVED
                        if row["cancel_observed_at"] is not None
                        else CancelDisposition.ALREADY_REQUESTED
                    )
                else:
                    updated = conn.execute(
                        f"""UPDATE {_CAPABILITY_TABLE}
                               SET cancel_requested_at=?,
                                   cancel_requested_generation=?,
                                   cancel_observed_at=NULL
                             WHERE execution_id=?""",
                        (timestamp, generation, execution),
                    )
                    if updated.rowcount != 1:
                        raise WebhookLedgerError(
                            "execution cancellation request disappeared"
                        )
                    disposition = CancelDisposition.REQUESTED
                    row = self._operation_join(conn, execution_id=execution)
                    if row is None:  # pragma: no cover - FK + same transaction
                        raise WebhookLedgerError("execution operation disappeared")
            return CancelRequestResult(
                disposition=disposition,
                status=self._status_from_row(row),
            )

    def claim_cancel(
        self,
        operation_id: str,
        generation: int,
        *,
        now: Optional[float] = None,
    ) -> bool:
        """Mark an exact-generation cancellation request observed by runtime.

        This trusted runtime seam is what closes the pre-bind race: if a remote
        request arrives before the real processing task exists, task creation
        calls ``claim_cancel`` and cancels that exact task.  Stale generations
        and post-target-attempt operations are never claimed.
        """

        operation = self._normalize_public_text(operation_id, label="operation_id")
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 1
        ):
            raise ValueError("generation must be a positive integer")
        timestamp = (
            time.time() if now is None else _finite_time(now, label="observe time")
        )
        with self.ledger._lock, self.ledger._transaction() as conn:
            row = conn.execute(
                f"""SELECT c.cancel_requested_generation,
                            c.cancel_observed_at, o.state, o.generation
                       FROM {_CAPABILITY_TABLE} AS c
                       JOIN webhook_operations AS o
                         ON o.operation_id=c.operation_id
                      WHERE c.operation_id=?""",
                (operation,),
            ).fetchone()
            if row is None:
                return False
            if (
                int(row["generation"]) != generation
                or row["cancel_requested_generation"] is None
                or int(row["cancel_requested_generation"]) != generation
                or not _is_cancellable(OperationState(row["state"]))
            ):
                return False
            if row["cancel_observed_at"] is not None:
                return True
            cursor = conn.execute(
                f"""UPDATE {_CAPABILITY_TABLE}
                       SET cancel_observed_at=?
                     WHERE operation_id=?
                       AND cancel_requested_generation=?
                       AND cancel_observed_at IS NULL""",
                (timestamp, operation, generation),
            )
            return cursor.rowcount == 1
