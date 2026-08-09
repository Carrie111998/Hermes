from types import SimpleNamespace

from plugins.agentops.control.cursors import advance_log_cursor
from plugins.agentops.control.observer_models import CursorResetReason, LogCursor


def test_log_cursor_classifies_initial_continuation_rotation_and_truncation():
    initial = advance_log_cursor(None, SimpleNamespace(st_ino=10, st_size=12))
    continuing = advance_log_cursor(LogCursor(inode=10, offset=4), SimpleNamespace(st_ino=10, st_size=12))
    rotated = advance_log_cursor(LogCursor(inode=10, offset=4), SimpleNamespace(st_ino=11, st_size=12))
    truncated = advance_log_cursor(LogCursor(inode=10, offset=13), SimpleNamespace(st_ino=10, st_size=12))

    assert (initial.offset, initial.reason) == (0, CursorResetReason.INITIAL)
    assert (continuing.offset, continuing.reason) == (4, CursorResetReason.CONTINUE)
    assert (rotated.offset, rotated.reason) == (0, CursorResetReason.ROTATED)
    assert (truncated.offset, truncated.reason) == (0, CursorResetReason.TRUNCATED)
