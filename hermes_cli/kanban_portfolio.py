"""Reviewed portfolio-provider contracts for Hermes Kanban.

The inventory reader never opens the live database through SQLite. It copies a
stable, bounded DB+WAL byte snapshot into a private temporary directory, opens
only that copy, validates the migrated schema, and serves deterministic SQL
reads. This avoids schema setup, WAL checkpoints, shared-memory read-mark
writes, readiness recomputation, and directory creation in the board tree.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator, Optional

from hermes_cli import kanban_db

MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024
MAX_RESPONSE_BYTES = 3_500_000
# JSON strings can expand by up to 6x (for example one-byte control characters
# become ``\\u00xx``).  Source text is therefore capped before any row-backed
# object/list is materialized, in addition to the final encoded response cap.
MAX_PREMATERIALIZATION_SOURCE_BYTES = MAX_RESPONSE_BYTES // 6
MAX_BOARD_TASKS = 10_000
MAX_BOARD_AGGREGATE_ROWS = 50_000
MAX_BOARD_PAGE_SIZE = 250
MAX_TASK_EVIDENCE_ROWS = 10_000
MAX_OPERATION_KEY_CHARS = 256
MAX_REASON_CHARS = 4_096
_COPY_ATTEMPTS = 3
_COPY_CHUNK_BYTES = 1024 * 1024


class PortfolioSnapshotUnavailable(RuntimeError):
    """An exhaustive, stable, zero-write board snapshot cannot be proven."""


class PortfolioSnapshotTooLarge(PortfolioSnapshotUnavailable):
    """The board or one evidence collection exceeded its reviewed bound."""


def canonical_board_db_path(board: str) -> tuple[str, Path]:
    """Resolve an explicitly pinned board while ignoring ambient DB overrides."""
    slug = kanban_db._normalize_board_slug(board)
    if not slug:
        raise ValueError("an explicit board slug is required")
    if slug == kanban_db.DEFAULT_BOARD:
        path = kanban_db.kanban_home() / "kanban.db"
    else:
        path = kanban_db.board_dir(slug) / "kanban.db"
    return slug, path


def _trusted_stat(path: Path, *, directory: bool) -> os.stat_result:
    try:
        value = path.lstat()
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"initialized Kanban board is absent: {path}") from exc
    mode = value.st_mode
    if stat.S_ISLNK(mode) or (directory and not stat.S_ISDIR(mode)):
        raise PermissionError(f"Kanban path is not a trusted directory: {path}")
    if not directory and not stat.S_ISREG(mode):
        raise PermissionError(f"Kanban database is not a regular file: {path}")
    if not directory and value.st_nlink != 1:
        raise PermissionError("Kanban database hard links are not allowed")
    if hasattr(os, "getuid") and value.st_uid != os.getuid():
        raise PermissionError("Kanban path is not owned by the Hermes operator")
    if directory and mode & 0o022:
        raise PermissionError("Kanban database directory is group/world writable")
    return value


def validate_board_db_path(board: str) -> tuple[str, Path, tuple[int, int]]:
    slug, path = canonical_board_db_path(board)
    root = kanban_db.kanban_home()
    # The Hermes root is the documented trust boundary. Validate every
    # descendant component lexically so a symlinked ``kanban/boards`` cannot
    # redirect a pinned board into another database tree.
    _trusted_stat(root, directory=True)
    try:
        relative_parent = path.parent.relative_to(root)
    except ValueError as exc:  # pragma: no cover - canonical resolver invariant
        raise PermissionError("Kanban database escaped the Hermes root") from exc
    current = root
    for component in relative_parent.parts:
        current = current / component
        _trusted_stat(current, directory=True)
    value = _trusted_stat(path, directory=False)
    return slug, path, (value.st_dev, value.st_ino)


def _identity(path: Path) -> tuple[int, int, int, int]:
    value = _trusted_stat(path, directory=False)
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _read_regular_file(path: Path, *, remaining: int) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        expected = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or (before.st_dev, before.st_ino) != (expected.st_dev, expected.st_ino)
        ):
            raise PermissionError("Kanban file identity changed while opening")
        if before.st_size > remaining:
            raise PortfolioSnapshotTooLarge(
                "Kanban DB/WAL snapshot exceeded byte bound"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(_COPY_CHUNK_BYTES, remaining - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > remaining:
                raise PortfolioSnapshotTooLarge(
                    "Kanban DB/WAL snapshot exceeded byte bound"
                )
            chunks.append(chunk)
        after = os.fstat(fd)
        if (
            before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or total != before.st_size
        ):
            raise PortfolioSnapshotUnavailable("Kanban file changed during snapshot")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _capture_files(
    path: Path, wal: Path
) -> tuple[
    tuple[int, int, int, int],
    tuple[int, int, int, int] | None,
    bytes,
    bytes | None,
]:
    """Capture one identity-stable, existence-stable bounded DB+WAL pair."""
    db_before = _identity(path)
    try:
        wal_before = _identity(wal)
    except FileNotFoundError:
        wal_before = None
    db_bytes = _read_regular_file(path, remaining=MAX_SNAPSHOT_BYTES)
    remaining = MAX_SNAPSHOT_BYTES - len(db_bytes)
    wal_bytes = (
        _read_regular_file(wal, remaining=remaining) if wal_before is not None else None
    )
    if _identity(path) != db_before:
        raise PortfolioSnapshotUnavailable("Kanban DB changed during snapshot")
    try:
        wal_after = _identity(wal)
    except FileNotFoundError:
        wal_after = None
    if wal_after != wal_before:
        raise PortfolioSnapshotUnavailable("Kanban WAL changed during snapshot")
    return db_before, wal_before, db_bytes, wal_bytes


def validate_portfolio_schema(conn: sqlite3.Connection) -> None:
    """Validate the exact reviewed provider schema and behavior artifacts."""
    kanban_db.validate_portfolio_contract_schema(conn)


@contextlib.contextmanager
def zero_write_snapshot(board: str) -> Iterator[tuple[str, sqlite3.Connection]]:
    """Yield a validated SQLite connection to a private stable DB+WAL copy."""
    slug, path, original_inode = validate_board_db_path(board)
    wal = path.with_name(path.name + "-wal")
    last_error: Exception | None = None
    snapshot_dir: tempfile.TemporaryDirectory[str] | None = None
    conn: sqlite3.Connection | None = None
    for _attempt in range(_COPY_ATTEMPTS):
        try:
            first = _capture_files(path, wal)
            second = _capture_files(path, wal)
            if first != second:
                raise PortfolioSnapshotUnavailable(
                    "Kanban DB/WAL captures were not identity- and byte-equal"
                )
            _db_identity, _wal_identity, db_bytes, wal_bytes = second
            current = path.lstat()
            if (current.st_dev, current.st_ino) != original_inode:
                raise PermissionError("Kanban database path was replaced")

            snapshot_dir = tempfile.TemporaryDirectory(prefix="hermes-kanban-ro-")
            snapshot_path = Path(snapshot_dir.name) / "kanban.db"
            snapshot_path.write_bytes(db_bytes)
            if wal_bytes is not None:
                snapshot_path.with_name("kanban.db-wal").write_bytes(wal_bytes)
            conn = sqlite3.connect(str(snapshot_path), isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            integrity = conn.execute("PRAGMA quick_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise PortfolioSnapshotUnavailable(
                    "Kanban snapshot failed integrity validation"
                )
            validate_portfolio_schema(conn)
            break
        except (sqlite3.Error, OSError, PortfolioSnapshotUnavailable) as exc:
            if conn is not None:
                conn.close()
                conn = None
            if snapshot_dir is not None:
                snapshot_dir.cleanup()
                snapshot_dir = None
            last_error = exc
    if conn is None or snapshot_dir is None:
        raise PortfolioSnapshotUnavailable(
            "unable to capture a stable exhaustive Kanban snapshot"
        ) from last_error
    try:
        yield slug, conn
    finally:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        conn.close()
        snapshot_dir.cleanup()


def connect_existing_board_rw(
    board: str,
) -> tuple[str, Path, tuple[int, int], sqlite3.Connection]:
    """Open a pinned existing board without setup, pragmas, or migrations."""
    slug, path, inode = validate_board_db_path(board)
    conn = sqlite3.connect(f"file:{path}?mode=rw", uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        current = _trusted_stat(path, directory=False)
        if (current.st_dev, current.st_ino) != inode:
            raise PermissionError("Kanban database path was replaced while opening")
    except Exception:
        conn.close()
        raise
    return slug, path, inode, conn


def _binding_hash(binding: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def operation_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "contract": row["contract"],
        "operation_key": row["operation_key"],
        "board": row["board"],
        "card_id": row["card_id"],
        "prior_status": row["prior_status"],
        "prior_revision": str(row["prior_revision"]),
        "prior_event_watermark": int(row["prior_event_watermark"]),
        "reason": row["reason"],
        "status": row["status"],
        "post_revision": str(row["post_revision"]),
        "post_event_watermark": int(row["post_event_watermark"]),
        "event_id": row["event_id"],
        "created_at": int(row["created_at"]),
    }


def read_operation(
    conn: sqlite3.Connection,
    *,
    board: str,
    card_id: str,
    operation_key: str,
) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM conditional_archive_operations "
        "WHERE operation_key = ? AND board = ? AND card_id = ?",
        (operation_key, board, card_id),
    ).fetchone()
    return operation_from_row(row) if row is not None else None


def conditional_archive(
    conn: sqlite3.Connection,
    *,
    board: str,
    card_id: str,
    expected_status: str,
    expected_revision: int,
    expected_event_watermark: int,
    operation_key: str,
    reason: str,
) -> dict[str, Any]:
    """Perform one atomic conditional archive and immutable journal insert."""
    binding = {
        "contract": kanban_db.PORTFOLIO_KANBAN_CONDITIONAL_ARCHIVE_CONTRACT,
        "board": board,
        "card_id": card_id,
        "expected_status": expected_status,
        "expected_revision": expected_revision,
        "expected_event_watermark": expected_event_watermark,
        "operation_key": operation_key,
        "reason": reason,
    }
    digest = _binding_hash(binding)
    # Validate before BEGIN IMMEDIATE so an unmigrated/malformed board fails
    # without acquiring a write lock or creating journal/WAL state. Validate
    # again under the transaction to close a concurrent schema-change race.
    validate_portfolio_schema(conn)
    with kanban_db.write_txn(conn):
        validate_portfolio_schema(conn)
        replay = conn.execute(
            "SELECT * FROM conditional_archive_operations WHERE operation_key = ?",
            (operation_key,),
        ).fetchone()
        if replay is not None:
            if replay["binding_hash"] != digest:
                raise kanban_db.ConditionalArchiveConflict(
                    "operation key is already bound to different archive inputs"
                )
            return operation_from_row(replay)

        task = conn.execute(
            "SELECT status, revision, claim_lock, claim_expires, worker_pid, "
            "current_run_id FROM tasks WHERE id = ?",
            (card_id,),
        ).fetchone()
        if task is None:
            raise KeyError(card_id)
        watermark = int(
            conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS value FROM task_events"
            ).fetchone()["value"]
        )
        if (
            task["status"] != expected_status
            or int(task["revision"]) != expected_revision
            or watermark != expected_event_watermark
        ):
            raise kanban_db.ConditionalArchiveConflict(
                "task status, revision, or event watermark changed"
            )
        if expected_status in {"running", "archived"}:
            raise kanban_db.ConditionalArchiveConflict(
                "running or archived cards are not stale-archive eligible"
            )
        if any(
            task[name] is not None
            for name in ("claim_lock", "claim_expires", "worker_pid")
        ):
            raise kanban_db.ConditionalArchiveConflict(
                "task has a current claim or worker"
            )
        current_run_id = task["current_run_id"]
        if current_run_id is not None:
            current_run = conn.execute(
                "SELECT task_id, status, ended_at FROM task_runs WHERE id = ?",
                (current_run_id,),
            ).fetchone()
            if (
                current_run is None
                or current_run["task_id"] != card_id
                or not kanban_db.is_terminal_run_status(
                    current_run["status"], current_run["ended_at"]
                )
            ):
                raise kanban_db.ConditionalArchiveConflict(
                    "task current run is missing, live, or inconsistent"
                )
        run_rows = conn.execute(
            "SELECT status, ended_at FROM task_runs WHERE task_id = ?",
            (card_id,),
        ).fetchall()
        if any(
            not kanban_db.is_terminal_run_status(row["status"], row["ended_at"])
            for row in run_rows
        ):
            raise kanban_db.ConditionalArchiveConflict(
                "task has a live, unterminated, unknown, or contradictory run"
            )

        changed = conn.execute(
            "UPDATE tasks SET status = 'archived', claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL, current_run_id = NULL "
            "WHERE id = ? AND status = ? AND revision = ? "
            "AND claim_lock IS NULL AND claim_expires IS NULL "
            "AND worker_pid IS NULL AND current_run_id IS ?",
            (card_id, expected_status, expected_revision, current_run_id),
        )
        if changed.rowcount != 1:
            raise kanban_db.ConditionalArchiveConflict(
                "task changed during archive CAS"
            )
        created_at = int(time.time())
        event = conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'conditional_archived', ?, ?)",
            (
                card_id,
                json.dumps(
                    {"operation_key": operation_key, "reason": reason},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                created_at,
            ),
        )
        event_id = int(event.lastrowid or 0)
        post_revision = int(
            conn.execute(
                "SELECT revision FROM tasks WHERE id = ?", (card_id,)
            ).fetchone()["revision"]
        )
        conn.execute(
            "INSERT INTO conditional_archive_operations ("
            "operation_key, binding_hash, contract, board, card_id, "
            "prior_status, prior_revision, prior_event_watermark, reason, "
            "status, post_revision, post_event_watermark, event_id, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'archived', ?, ?, ?, ?)",
            (
                operation_key,
                digest,
                kanban_db.PORTFOLIO_KANBAN_CONDITIONAL_ARCHIVE_CONTRACT,
                board,
                card_id,
                expected_status,
                expected_revision,
                expected_event_watermark,
                reason,
                post_revision,
                event_id,
                event_id,
                created_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM conditional_archive_operations WHERE operation_key = ?",
            (operation_key,),
        ).fetchone()
        if row is None:  # pragma: no cover
            raise RuntimeError("conditional archive journal insert disappeared")
        return operation_from_row(row)
