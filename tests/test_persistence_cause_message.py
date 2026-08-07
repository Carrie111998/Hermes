"""``run_agent.AIAgent._format_session_persistence_reason`` and
``agent.turn_finalizer._persistence_failure_error_message`` must name the
real cause of a failed ``session_persistence_failed`` write instead of
always pointing at disk space — #81227.

These tests do not boot a full agent. They bind the cause-aware methods
onto a tiny object and check the user-facing string for every cause
bucket, plus the legacy fallback when no exception is recorded.
"""

from __future__ import annotations

import errno
import sqlite3
import types

from agent.turn_finalizer import _persistence_failure_error_message
from hermes_state import (
    CompressionSessionBusyError,
    SessionCompressionInProgressError,
)


class _StubAgent:
    """Minimal stand-in exposing the two attributes the helpers read."""

    def __init__(self, exc):
        self._last_persistence_failure = exc


def _format(self_obj):
    """Drive ``AIAgent._format_session_persistence_reason`` without importing
    the full agent. The method only reads ``self._last_persistence_failure``,
    so we bind an unbound copy onto the stub.
    """
    from run_agent import AIAgent
    return AIAgent._format_session_persistence_reason(self_obj)


# Shared fallback substrings anchored on the legacy wording so callers without
# ``_last_persistence_failure`` see the pre-#81227 behavior.
_LEGACY_FALLBACK = "often a full disk"


def test_legacy_fallback_when_no_exception_recorded():
    agent = _StubAgent(exc=None)
    text = _format(agent)
    assert "session storage could not be written" in text
    assert _LEGACY_FALLBACK in text


def test_legacy_fallback_when_attribute_missing():
    bare = object()
    text = _format(bare)
    assert _LEGACY_FALLBACK in text


def test_compression_lock_surfaces_compression_hint():
    """The exact incident from #81227: a live foreign compression lock.
    The user-facing message must mention compression, not disk space."""
    exc = SessionCompressionInProgressError(
        "Session 'sess-1' is being compressed by another writer"
    )
    agent = _StubAgent(exc=exc)
    text = _format(agent)
    assert "compression" in text or "compressed" in text
    assert "another writer" in text or "retry" in text.lower()
    assert _LEGACY_FALLBACK not in text


def test_database_locked_surfaces_maintenance_hint():
    exc = sqlite3.OperationalError("database is locked")
    agent = _StubAgent(exc=exc)
    text = _format(agent)
    assert "lock" in text.lower() or "vacuum" in text.lower() or "maintenance" in text.lower()
    assert _LEGACY_FALLBACK not in text


def test_disk_full_keeps_disk_hint():
    exc = OSError(errno.ENOSPC, "No space left on device")
    agent = _StubAgent(exc=exc)
    text = _format(agent)
    assert "disk" in text.lower()
    assert "free" in text.lower() or "space" in text.lower()


def test_permission_denied_surfaces_permission_hint():
    exc = PermissionError("Permission denied")
    agent = _StubAgent(exc=exc)
    text = _format(agent)
    assert "permission" in text.lower()
    assert _LEGACY_FALLBACK not in text


def test_db_corruption_surfaces_corruption_hint():
    exc = sqlite3.DatabaseError("database disk image is malformed")
    agent = _StubAgent(exc=exc)
    text = _format(agent)
    assert "corrupt" in text.lower() or "malformed" in text.lower() or "inspect" in text.lower()


def test_unknown_exception_uses_legacy_fallback():
    """A plain ``RuntimeError`` has no recognizable signature — the helper
    must not invent a cause. The legacy wording is the safe fallback."""
    exc = RuntimeError("network timeout")
    agent = _StubAgent(exc=exc)
    text = _format(agent)
    assert _LEGACY_FALLBACK in text


def test_base_compression_busy_class_also_routes_correctly():
    """CompressionSessionBusyError (the parent) must also classify as
    'compression_lock', not as 'database_locked' despite the message
    reading like a generic busy error."""
    exc = CompressionSessionBusyError("foreign compression lock")
    agent = _StubAgent(exc=exc)
    text = _format(agent)
    assert "compression" in text.lower() or "compress" in text.lower()


# --- turn_finalizer helper parity ---


def test_turn_finalizer_helper_uses_legacy_fallback_when_no_exception():
    agent = _StubAgent(exc=None)
    text = _persistence_failure_error_message(agent, final_response=None)
    assert _LEGACY_FALLBACK in text or "free disk space" in text


def test_turn_finalizer_helper_uses_compression_hint_for_compression_lock():
    exc = SessionCompressionInProgressError(
        "Session 'sess-2' is being compressed by another writer"
    )
    agent = _StubAgent(exc=exc)
    text = _persistence_failure_error_message(agent, final_response=None)
    assert "compression" in text.lower() or "compress" in text.lower()
    assert _LEGACY_FALLBACK not in text


def test_turn_finalizer_helper_uses_disk_full_for_enospc():
    exc = OSError(errno.ENOSPC, "No space left on device")
    agent = _StubAgent(exc=exc)
    text = _persistence_failure_error_message(agent, final_response=None)
    assert "disk" in text.lower()
    assert "free" in text.lower() or "space" in text.lower()


def test_turn_finalizer_helper_keeps_final_response_when_present():
    """When the caller already set a custom ``final_response`` (e.g. an
    earlier layer produced its own message) and there is no recorded
    exception, that message must survive — the helper must not overwrite
    caller-provided text with the legacy fallback."""
    agent = _StubAgent(exc=None)
    text = _persistence_failure_error_message(agent, final_response="custom failure")
    assert "custom failure" in text
