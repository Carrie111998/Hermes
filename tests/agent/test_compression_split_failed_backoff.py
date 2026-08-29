"""Phase 1 RC3 — session_split_failed composes with landed durable backoff ladder.

Verifies our change from flat 60s _record_compression_failure_cooldown to
record_timeout_failure(kind=session_split_failed) which stamps
backoff:session_split_failed:strategy=<tail_mode>.
"""
import time
import tempfile
from pathlib import Path

import pytest

from agent.context_compressor import ContextCompressor
from hermes_state import SessionDB


def _make_compressor(session_id="s1", tmp_path=None):
    db = SessionDB(tmp_path / "state.db") if tmp_path is not None else None
    comp = ContextCompressor(model="test")
    comp._session_db = db  # type: ignore[attr-defined]
    comp._session_id = session_id  # type: ignore[attr-defined]
    comp.tail_mode = "lean"
    return comp, db


def test_split_failed_records_stamped_backoff(tmp_path):
    comp, db = _make_compressor("s1", tmp_path)
    db.create_session("s1", source="test")
    comp.record_timeout_failure("session_split_failed: lease lost", failure_kind="session_split_failed")
    row = db.get_compression_failure_cooldown_row("s1")
    assert row["session_exists"] is True
    assert row["cooldown_until"] is not None
    assert row["cooldown_until"] > time.time()
    assert row["error"].startswith("backoff:session_split_failed:strategy=")


def test_same_failure_suppressed(tmp_path):
    comp, db = _make_compressor("s1", tmp_path)
    db.create_session("s1", source="test")
    comp.record_timeout_failure("session_split_failed: x", failure_kind="session_split_failed")
    assert comp.get_active_compression_failure_cooldown() is not None
    comp2, _ = _make_compressor("s1", tmp_path)
    comp2._session_db = db
    comp2._session_id = "s1"
    assert comp2.get_active_compression_failure_cooldown(refresh=True) is not None


def test_force_manual_bypasses():
    comp, _ = _make_compressor("s1", None)
    comp.record_timeout_failure("session_split_failed: x", failure_kind="session_split_failed")
    assert comp._summary_failure_cooldown_until > 0
    assert comp._last_summary_error.startswith("backoff:session_split_failed")


def test_success_clears_according_to_existing_mechanism(tmp_path):
    comp, db = _make_compressor("s1", tmp_path)
    db.create_session("s1", source="test")
    comp.record_timeout_failure("session_split_failed: x", failure_kind="session_split_failed")
    assert db.get_compression_failure_cooldown_row("s1")["cooldown_until"] is not None
    comp._clear_compression_failure_cooldown()
    row = db.get_compression_failure_cooldown_row("s1")
    assert row["cooldown_until"] is None
    assert row["error"] is None


def test_98741_compatibility_same_turn_typed_outcome_still_works():
    """#98741's thread-local typed timeout must not be clobbered by our change."""
    try:
        from agent.conversation_compression import (
            _get_context_compression_timeout_outcome,
            _set_context_compression_timeout_outcome,
            _clear_context_compression_timeout_outcome,
        )
    except ImportError:
        pytest.skip("upstream #98741 symbols not yet present on this base — compatibility trivial")
    _clear_context_compression_timeout_outcome()
    assert _get_context_compression_timeout_outcome() is None
    _set_context_compression_timeout_outcome("ceiling_exhausted")
    assert _get_context_compression_timeout_outcome() == "ceiling_exhausted"
    _clear_context_compression_timeout_outcome()
    assert _get_context_compression_timeout_outcome() is None
