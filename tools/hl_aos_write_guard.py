"""Compatibility exports for the A1.6 HL-AOS write guard.

Historically some tests and tool integrations imported the write-sink guard
from ``tools.hl_aos_write_guard``.  The implementation now lives in
``agent.hl_aos_write_guard`` because it reads agent/session taint state.  Keep
this thin re-export so tool-side integrations have a stable import path without
duplicating policy logic.
"""

from agent.hl_aos_write_guard import (  # noqa: F401
    EGRESS_SINKS,
    check_egress_permission,
    check_write_permission,
    check_write_permission_with_context,
)
