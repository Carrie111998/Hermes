"""Shared classification for durable cold-archive tombstone rejections."""

from __future__ import annotations

import sqlite3


def is_cold_archive_tombstone_rejection(exc: BaseException) -> bool:
    """Return whether SQLite rejected a write to a permanently fenced ID."""
    return isinstance(exc, sqlite3.IntegrityError) and "cold-archived" in str(exc)
