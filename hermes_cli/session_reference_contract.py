"""Shared durable session-reference contract for cold Purge and recovery."""

from __future__ import annotations

STATE_META_SESSION_NAMESPACES = ("goal", "loop", "heartbeat")

ASYNC_DELEGATION_SESSION_COLUMNS = (
    "origin_session",
    "parent_session_id",
    "origin_session_id",
)
