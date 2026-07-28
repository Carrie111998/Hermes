"""ACP session mapping — persist Hermes↔ACP session bindings.

A Hermes session may drive one or more ACP server sessions (e.g. a primary
agent plus a reviewer).  To let a resumed Hermes session reattach to the
same ACP session, that (hermes_session_id, provider) → acp_session_id
mapping must survive process restarts.  This module owns that mapping.

Design:
    * ``ACPSessionBinding`` — an immutable record of one binding.
    * ``ACPSessionMapper`` — the storage-agnostic interface callers depend
      on (so an in-memory fake can satisfy tests).
    * ``SQLiteACPSessionMapper`` — the persistent implementation, backed by
      the shared ``state.db`` (WAL mode, one writer serialized via a lock).

``get_hermes_home`` is imported lazily inside methods to avoid pulling the
constants module (and its transitive imports) at module load time, which
would create an import cycle for callers that import this module early.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ACPSessionBinding:
    """One persisted mapping between a Hermes session and an ACP session.

    The ``(hermes_session_id, provider)`` pair is the natural identity: a
    single Hermes session may be bound to several ACP providers at once.
    ``status`` lets the runtime retire a binding without deleting it
    immediately, which keeps diagnostics available and lets ``list_stale``
    reap old bindings lazily.
    """

    hermes_session_id: str
    acp_session_id: str
    provider: str
    created_at: float = field(default_factory=time.time)
    last_active_at: float = field(default_factory=time.time)
    cwd: Optional[str] = None
    model: Optional[str] = None
    permission_mode: Optional[str] = None
    status: str = "active"


class ACPSessionMapper(Protocol):
    """Storage-agnostic interface for Hermes↔ACP session bindings.

    A persistent implementation (e.g. :class:`SQLiteACPSessionMapper`) keeps
    the mapping across restarts; an in-memory fake can implement the same
    surface for tests.
    """

    def bind(self, binding: ACPSessionBinding) -> None:
        """Persist (or replace) a binding."""
        ...

    def lookup(
        self,
        hermes_session_id: str,
        provider: Optional[str] = None,
    ) -> Optional[ACPSessionBinding]:
        """Return the binding for ``hermes_session_id``.

        When ``provider`` is given, the exact ``(hermes_session_id, provider)``
        row is returned (or ``None``).  When ``provider`` is ``None``, the
        most recently active binding is returned (preferring ``status``
        ``"active"``), or ``None`` if no row exists.
        """
        ...

    def unbind(self, hermes_session_id: str) -> None:
        """Remove every binding for ``hermes_session_id``."""
        ...

    def mark_stale(self, hermes_session_id: str) -> None:
        """Flag every binding for ``hermes_session_id`` as ``"stale"``."""
        ...

    def update_activity(self, hermes_session_id: str) -> None:
        """Bump ``last_active_at`` to now for every matching binding."""
        ...

    def list_stale(self, older_than: float) -> list[ACPSessionBinding]:
        """Return all stale bindings older than ``older_than`` (epoch seconds)."""
        ...


class SQLiteACPSessionMapper:
    """Persistent :class:`ACPSessionMapper` backed by SQLite (``state.db``).

    The connection is opened lazily on first use (WAL mode, ``busy_timeout``
    set) and all access is serialized through a :class:`threading.Lock`, so
    the same instance is safe to share across threads.  The connection is
    opened with ``check_same_thread=False``; the lock — not SQLite's
    thread-affinity check — provides the safety guarantee.

    The table lives in the shared ``state.db`` so ACP bindings are co-located
    with the rest of Hermes' persistent state and benefit from the same WAL
    concurrency story.
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        if db_path is None:
            # Lazy import: avoids pulling hermes_constants (and its
            # transitive deps) at module import time, which would create an
            # import cycle for early importers of this module.
            from hermes_constants import get_hermes_home

            db_path = get_hermes_home() / "state.db"
        self._db_path: Path = Path(db_path)
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Connection / schema
    # ------------------------------------------------------------------ #

    def _connect(self) -> sqlite3.Connection:
        """Open the SQLite connection lazily and ensure the schema exists."""
        if self._conn is not None:
            return self._conn

        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        self._ensure_schema(conn)
        self._conn = conn
        return conn

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS acp_session_bindings (
                hermes_session_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                acp_session_id TEXT NOT NULL,
                cwd TEXT,
                model TEXT,
                permission_mode TEXT,
                created_at REAL NOT NULL,
                last_active_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                PRIMARY KEY (hermes_session_id, provider)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_acp_bindings_status "
            "ON acp_session_bindings(status)"
        )
        conn.commit()

    @staticmethod
    def _row_to_binding(row: Optional[sqlite3.Row]) -> Optional[ACPSessionBinding]:
        if row is None:
            return None
        return ACPSessionBinding(
            hermes_session_id=row["hermes_session_id"],
            acp_session_id=row["acp_session_id"],
            provider=row["provider"],
            cwd=row["cwd"],
            model=row["model"],
            permission_mode=row["permission_mode"],
            created_at=row["created_at"],
            last_active_at=row["last_active_at"],
            status=row["status"],
        )

    # ------------------------------------------------------------------ #
    # ACPSessionMapper implementation
    # ------------------------------------------------------------------ #

    def bind(self, binding: ACPSessionBinding) -> None:
        """Insert or replace the row for ``(hermes_session_id, provider)``."""
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT OR REPLACE INTO acp_session_bindings
                    (hermes_session_id, acp_session_id, provider, cwd, model,
                     permission_mode, created_at, last_active_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    binding.hermes_session_id,
                    binding.acp_session_id,
                    binding.provider,
                    binding.cwd,
                    binding.model,
                    binding.permission_mode,
                    binding.created_at,
                    binding.last_active_at,
                    binding.status,
                ),
            )
            conn.commit()

    def lookup(
        self,
        hermes_session_id: str,
        provider: Optional[str] = None,
    ) -> Optional[ACPSessionBinding]:
        """Return the binding for ``hermes_session_id``.

        With ``provider`` set, the exact row is returned (or ``None``).
        Without it, the most recently active binding is returned, preferring
        ``status`` ``"active"`` over ``"stale"`` so a caller resumes a live
        session before falling back to a retired one.
        """
        with self._lock:
            conn = self._connect()
            if provider is not None:
                cur = conn.execute(
                    """
                    SELECT * FROM acp_session_bindings
                    WHERE hermes_session_id = ? AND provider = ?
                    """,
                    (hermes_session_id, provider),
                )
            else:
                # Active rows first ((status='active') evaluates to 1 for
                # active, 0 otherwise → DESC puts active first), then most
                # recently active.
                cur = conn.execute(
                    """
                    SELECT * FROM acp_session_bindings
                    WHERE hermes_session_id = ?
                    ORDER BY (status = 'active') DESC, last_active_at DESC
                    LIMIT 1
                    """,
                    (hermes_session_id,),
                )
            row = cur.fetchone()
        return self._row_to_binding(row)

    def unbind(self, hermes_session_id: str) -> None:
        """Delete every binding for ``hermes_session_id``."""
        with self._lock:
            conn = self._connect()
            conn.execute(
                "DELETE FROM acp_session_bindings WHERE hermes_session_id = ?",
                (hermes_session_id,),
            )
            conn.commit()

    def mark_stale(self, hermes_session_id: str) -> None:
        """Flag every binding for ``hermes_session_id`` as ``"stale"``."""
        with self._lock:
            conn = self._connect()
            conn.execute(
                "UPDATE acp_session_bindings SET status = 'stale' "
                "WHERE hermes_session_id = ?",
                (hermes_session_id,),
            )
            conn.commit()

    def update_activity(self, hermes_session_id: str) -> None:
        """Bump ``last_active_at`` to now for every matching binding."""
        with self._lock:
            conn = self._connect()
            conn.execute(
                "UPDATE acp_session_bindings SET last_active_at = ? "
                "WHERE hermes_session_id = ?",
                (time.time(), hermes_session_id),
            )
            conn.commit()

    def list_stale(self, older_than: float) -> list[ACPSessionBinding]:
        """Return all stale bindings whose ``last_active_at`` < ``older_than``."""
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                """
                SELECT * FROM acp_session_bindings
                WHERE status = 'stale' AND last_active_at < ?
                ORDER BY last_active_at ASC
                """,
                (older_than,),
            )
            rows = cur.fetchall()
        return [self._row_to_binding(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        """Close the underlying connection if it was opened. Safe to call
        repeatedly and before any DB access (no-op in that case)."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
