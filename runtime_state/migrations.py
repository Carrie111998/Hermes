"""Fail-closed SQLite/WAL open and atomic ARCH-001 migrations."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Optional, Union

from runtime_state.retry_config import (
    DEFAULT_RETRY_CONFIG,
    RetryConfig,
    apply_busy_timeout,
)
from runtime_state.schema import (
    MIGRATION_METADATA,
    MIGRATION_1_CHECKSUM,
    MIN_COMPATIBLE_SCHEMA_VERSION,
    SCHEMA_DDL,
    SCHEMA_VERSION,
    STATE_TABLES,
)


class RuntimeStateSchemaError(RuntimeError):
    """The on-disk schema cannot be safely opened by this implementation."""


class RuntimeStatePreflightError(RuntimeError):
    """A required connection preflight invariant could not be established."""


class RuntimeStateMigrationError(RuntimeError):
    """A migration failed and was rolled back."""


def utc_timestamp() -> str:
    """Return the contract timestamp format with UTC millisecond precision."""

    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _split_sql(sql: str) -> list[str]:
    """Split the static ARCH-001 DDL into executable statements.

    The canonical schema contains no triggers or semicolons inside string
    literals, so this deliberately small splitter keeps execution explicit and
    avoids sqlite3 ``executescript``'s implicit transaction behavior.
    """

    return [statement.strip() for statement in sql.split(";") if statement.strip()]


class RuntimeStateDB:
    """Own one standalone runtime-state SQLite database.

    Existing files are inspected through a read-only URI before a read-write
    connection is opened. WAL, foreign-key enforcement, and the busy timeout
    are then established before any state mutation. The package is not wired
    into CLI, gateway, or production entry points by ARCH-001.
    """

    def __init__(
        self,
        db_path: Union[str, Path],
        retry_config: RetryConfig = DEFAULT_RETRY_CONFIG,
    ) -> None:
        self.db_path = Path(db_path)
        self.retry_config = retry_config
        self._conn: Optional[sqlite3.Connection] = None

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        on_disk = self._inspect_existing_read_only()
        try:
            self._conn = sqlite3.connect(
                str(self.db_path), isolation_level=None, check_same_thread=False
            )
            self._configure_connection()
            self._apply_or_validate(on_disk)
        except Exception:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            raise

    def _inspect_existing_read_only(self) -> Optional[int]:
        """Inspect compatibility without allowing SQLite to mutate the file."""

        if not self.db_path.exists() or self.db_path.stat().st_size == 0:
            return None

        uri = self.db_path.resolve().as_uri() + "?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True)
        except sqlite3.DatabaseError as exc:
            raise RuntimeStateSchemaError(
                f"runtime state database is not a readable SQLite file: {self.db_path}"
            ) from exc

        try:
            conn.execute("PRAGMA foreign_keys = ON")
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if "schema_version" not in tables:
                if tables:
                    raise RuntimeStateSchemaError(
                        "database contains tables but no runtime_state schema_version table"
                    )
                return None

            row = conn.execute(
                "SELECT schema_version, min_compatible_schema_version "
                "FROM schema_version WHERE id = 1"
            ).fetchone()
            if row is None:
                raise RuntimeStateSchemaError(
                    "schema_version singleton row is missing"
                )
            on_disk, minimum_on_disk = int(row[0]), int(row[1])
            if on_disk > SCHEMA_VERSION:
                raise RuntimeStateSchemaError(
                    f"database schema_version {on_disk} is newer than supported "
                    f"version {SCHEMA_VERSION}"
                )
            if on_disk < MIN_COMPATIBLE_SCHEMA_VERSION:
                raise RuntimeStateSchemaError(
                    f"database schema_version {on_disk} is older than minimum "
                    f"compatible version {MIN_COMPATIBLE_SCHEMA_VERSION}"
                )
            if minimum_on_disk > SCHEMA_VERSION:
                raise RuntimeStateSchemaError(
                    "database minimum-compatible version exceeds this reader"
                )

            self._validate_migration_history(conn)
            self._validate_row_versions(conn)
            return on_disk
        except RuntimeStateSchemaError:
            raise
        except sqlite3.DatabaseError as exc:
            raise RuntimeStateSchemaError(
                f"runtime state schema preflight failed for {self.db_path}"
            ) from exc
        finally:
            conn.close()

    def _validate_migration_history(self, conn: sqlite3.Connection) -> None:
        try:
            rows = conn.execute(
                "SELECT version, description, checksum_sha256 "
                "FROM runtime_state_migrations ORDER BY version"
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise RuntimeStateSchemaError(
                "runtime_state_migrations is missing or malformed"
            ) from exc

        expected = [
            (
                item["version"],
                item["description"],
                item["checksum_sha256"],
            )
            for item in MIGRATION_METADATA
            if item["version"] <= SCHEMA_VERSION
        ]
        if rows != expected:
            raise RuntimeStateSchemaError(
                "migration history is missing, reordered, or has a changed checksum"
            )

    def _validate_row_versions(self, conn: sqlite3.Connection) -> None:
        for table in STATE_TABLES:
            if table not in {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }:
                raise RuntimeStateSchemaError(f"required table {table!r} is missing")
            row = conn.execute(
                f"SELECT 1 FROM {table} WHERE schema_version < ? "
                "OR schema_version > ? LIMIT 1",
                (MIN_COMPATIBLE_SCHEMA_VERSION, SCHEMA_VERSION),
            ).fetchone()
            if row is not None:
                raise RuntimeStateSchemaError(
                    f"{table} contains a row outside the supported schema-version range"
                )

    def _configure_connection(self) -> None:
        assert self._conn is not None
        conn = self._conn
        conn.execute("PRAGMA foreign_keys = ON")
        if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise RuntimeStatePreflightError("SQLite foreign-key enforcement is disabled")
        apply_busy_timeout(conn, self.retry_config)
        journal_mode = str(conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
        if journal_mode != "wal":
            raise RuntimeStatePreflightError(
                f"runtime state requires WAL; SQLite selected {journal_mode!r}"
            )

    def _apply_or_validate(self, on_disk: Optional[int]) -> None:
        assert self._conn is not None
        if on_disk is not None:
            if on_disk != SCHEMA_VERSION:
                raise RuntimeStateSchemaError(
                    f"no registered migration path from schema version {on_disk}"
                )
            return

        conn = self._conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            for statement in _split_sql(SCHEMA_DDL):
                conn.execute(statement)
            now = utc_timestamp()
            conn.execute(
                "INSERT INTO schema_version "
                "(id, schema_version, min_compatible_schema_version, updated_at) "
                "VALUES (1, ?, ?, ?)",
                (SCHEMA_VERSION, MIN_COMPATIBLE_SCHEMA_VERSION, now),
            )
            conn.execute(
                "INSERT INTO runtime_state_migrations "
                "(version, description, checksum_sha256, applied_at) "
                "VALUES (?, ?, ?, ?)",
                (1, MIGRATION_METADATA[0]["description"], MIGRATION_1_CHECKSUM, now),
            )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            raise RuntimeStateMigrationError(
                "initial runtime-state migration rolled back"
            ) from exc

    @property
    def connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("runtime state database is closed")
        return self._conn

    @property
    def schema_version(self) -> int:
        row = self.connection.execute(
            "SELECT schema_version FROM schema_version WHERE id = 1"
        ).fetchone()
        if row is None:
            raise RuntimeStateSchemaError("schema_version singleton row is missing")
        return int(row[0])

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "RuntimeStateDB":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
