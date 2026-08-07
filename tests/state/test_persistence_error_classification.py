"""``classify_persistence_error`` and ``persistence_cause_message`` name the
real cause of a failed ``session_persistence_failed`` write so the turn
finalizer can stop blaming "disk full" when the real cause is a held
compression lock, a competing VACUUM, or a permission failure (#81227).

These tests intentionally exercise every cause bucket and the order
sensitivity (compression lock must be classified as ``compression_lock``,
not ``database_locked``, even though it looks like a generic busy error).
"""

from __future__ import annotations

import errno
import sqlite3

from hermes_state import (
    CompressionSessionBusyError,
    SessionCompressionInProgressError,
    classify_persistence_error,
    persistence_cause_message,
)
from hermes_state import (
    PERSISTENCE_CAUSE_COMPRESSION_LOCK,
    PERSISTENCE_CAUSE_DATABASE_LOCKED,
    PERSISTENCE_CAUSE_DB_CORRUPTION,
    PERSISTENCE_CAUSE_DISK_FULL,
    PERSISTENCE_CAUSE_PERMISSION_DENIED,
    PERSISTENCE_CAUSE_UNKNOWN,
)


def test_none_is_unknown():
    assert classify_persistence_error(None) == PERSISTENCE_CAUSE_UNKNOWN


def test_compression_lock_subclass_is_compression_lock():
    """A live foreign compression lock must be classified as ``compression_lock`` (#81227)."""
    exc = SessionCompressionInProgressError(
        "Session 'abc' is being compressed by another writer"
    )
    assert classify_persistence_error(exc) == PERSISTENCE_CAUSE_COMPRESSION_LOCK


def test_compression_busy_base_class_is_compression_lock():
    """The shared parent class also routes here — tests that subclass order
    in the matcher list is not regressed into a SQLite busy classification."""
    exc = CompressionSessionBusyError("compression busy")
    assert classify_persistence_error(exc) == PERSISTENCE_CAUSE_COMPRESSION_LOCK


def test_enospc_oserror_is_disk_full():
    exc = OSError(errno.ENOSPC, "No space left on device")
    assert classify_persistence_error(exc) == PERSISTENCE_CAUSE_DISK_FULL


def test_sqlite_full_is_disk_full():
    exc = sqlite3.OperationalError("database or disk is full")
    assert classify_persistence_error(exc) == PERSISTENCE_CAUSE_DISK_FULL


def test_permission_error_is_permission_denied():
    exc = PermissionError("Permission denied")
    assert classify_persistence_error(exc) == PERSISTENCE_CAUSE_PERMISSION_DENIED


def test_eacces_oserror_is_permission_denied():
    exc = OSError(errno.EACCES, "Permission denied")
    assert classify_persistence_error(exc) == PERSISTENCE_CAUSE_PERMISSION_DENIED


def test_eperm_oserror_is_permission_denied():
    exc = OSError(errno.EPERM, "Operation not permitted")
    assert classify_persistence_error(exc) == PERSISTENCE_CAUSE_PERMISSION_DENIED


def test_erofs_oserror_is_permission_denied():
    exc = OSError(errno.EROFS, "Read-only file system")
    assert classify_persistence_error(exc) == PERSISTENCE_CAUSE_PERMISSION_DENIED


def test_sqlite_permission_operational_error_is_permission_denied():
    exc = sqlite3.OperationalError("attempt to write a readonly database")
    assert classify_persistence_error(exc) == PERSISTENCE_CAUSE_PERMISSION_DENIED


def test_sqlite_locked_operational_error_is_database_locked():
    exc = sqlite3.OperationalError("database is locked")
    assert classify_persistence_error(exc) == PERSISTENCE_CAUSE_DATABASE_LOCKED


def test_sqlite_busy_operational_error_is_database_locked():
    exc = sqlite3.OperationalError("database is busy")
    assert classify_persistence_error(exc) == PERSISTENCE_CAUSE_DATABASE_LOCKED


def test_sqlite_database_error_is_corruption():
    exc = sqlite3.DatabaseError("database disk image is malformed")
    assert classify_persistence_error(exc) == PERSISTENCE_CAUSE_DB_CORRUPTION


def test_runtime_error_is_unknown():
    exc = RuntimeError("network timeout")
    assert classify_persistence_error(exc) == PERSISTENCE_CAUSE_UNKNOWN


def test_compression_lock_takes_priority_over_sqlite_busy_text():
    """The matcher list must check compression subclass BEFORE the generic
    SQLite ``locked/busy`` text match. ``SessionCompressionInProgressError``
    carries the substring "another writer" which is fine, but a future
    backport that appends "busy" to the message must still classify as
    compression rather than database_locked."""
    exc = SessionCompressionInProgressError(
        "Session 'abc' is being compressed by another writer (database busy)"
    )
    assert classify_persistence_error(exc) == PERSISTENCE_CAUSE_COMPRESSION_LOCK


def test_string_only_exception_is_unknown():
    """A bare string is not a ``BaseException`` — classification falls back to
    unknown so callers do not accidentally treat flat error messages as
    ENOSPC."""
    assert classify_persistence_error("some plain text") == PERSISTENCE_CAUSE_UNKNOWN


def test_disk_full_takes_priority_over_locked_text():
    """The disk-full matcher checks against real ENOSPC / SQLITE_FULL before
    the locked-busy matcher reads the lowercased string. A SQLITE_FULL
    message ("database or disk is full") should never be confused with a
    plain VACUUM-held lock."""
    exc = sqlite3.OperationalError("database or disk is full")
    assert classify_persistence_error(exc) == PERSISTENCE_CAUSE_DISK_FULL


# --- persistence_cause_message ---

def test_compression_cause_message_mentions_compression():
    text = persistence_cause_message(PERSISTENCE_CAUSE_COMPRESSION_LOCK)
    assert "compress" in text.lower()
    assert "retry" in text.lower()


def test_database_locked_message_mentions_maintenance():
    text = persistence_cause_message(PERSISTENCE_CAUSE_DATABASE_LOCKED)
    assert "lock" in text.lower() or "vacuum" in text.lower()


def test_disk_full_message_mentions_disk_space():
    text = persistence_cause_message(PERSISTENCE_CAUSE_DISK_FULL)
    assert "disk" in text.lower()


def test_permission_message_mentions_permissions():
    text = persistence_cause_message(PERSISTENCE_CAUSE_PERMISSION_DENIED)
    assert "permission" in text.lower()


def test_corruption_message_mentions_corruption():
    text = persistence_cause_message(PERSISTENCE_CAUSE_DB_CORRUPTION)
    assert "corrupt" in text.lower() or "inspect" in text.lower()


def test_unknown_cause_message_preserves_legacy_wording():
    """The unknown-bucket fallback keeps the original "often a full disk"
    wording so callers without a recorded exception see the prior
    behavior."""
    text = persistence_cause_message(PERSISTENCE_CAUSE_UNKNOWN)
    assert "full disk" in text.lower()


def test_arbitrary_cause_falls_back_to_legacy_wording():
    """An unrecognized cause goes through the same fallback path."""
    text = persistence_cause_message("not-a-real-cause")
    assert "full disk" in text.lower()
