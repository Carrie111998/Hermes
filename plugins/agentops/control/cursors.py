"""Inode/offset cursor decisions without any file mutation."""

from __future__ import annotations

import os

from plugins.agentops.control.observer_models import CursorDecision, CursorResetReason, LogCursor


def advance_log_cursor(cursor: LogCursor | None, stat_result: os.stat_result) -> CursorDecision:
    """Select the safe starting offset for a regular-file observation."""
    inode = int(stat_result.st_ino)
    size = int(stat_result.st_size)
    if inode < 0 or size < 0:
        raise ValueError("invalid file metadata")
    if cursor is None:
        return CursorDecision(offset=0, reason=CursorResetReason.INITIAL)
    if cursor.inode != inode:
        return CursorDecision(offset=0, reason=CursorResetReason.ROTATED)
    if cursor.offset > size:
        return CursorDecision(offset=0, reason=CursorResetReason.TRUNCATED)
    return CursorDecision(offset=cursor.offset, reason=CursorResetReason.CONTINUE)
