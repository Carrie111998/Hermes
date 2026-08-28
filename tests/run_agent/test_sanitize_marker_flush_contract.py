"""Regression tests for the _DB_PERSISTED_MARKER mutate-then-persist contract
(#92231) as it applies to agent.message_sanitization's in-place mutators.

CONTRACT (run_agent.py, _DB_PERSISTED_MARKER docstring): any code that
mutates a loaded or flushed message dict's content in place and needs the
change persisted MUST pop the marker (and invalidate _db_flush_scan_prefix
if the dict may sit inside the bounded-scan prefix), or the change is
silently never re-written to state.db.

_sanitize_messages_surrogates, _sanitize_messages_non_ascii, and
_strip_images_from_messages all mutate `messages` dicts in place (via
conversation_loop.py's UnicodeEncodeError / image-rejection retry paths) and
previously did neither half of the contract:

  1. The sanitizer itself never popped _DB_PERSISTED_MARKER, so an
     already-flushed dict kept the marker True after its content changed.
  2. The conversation_loop.py call sites never invalidated
     agent._db_flush_scan_prefix, so even a popped marker would go unnoticed
     by the bounded-scan optimization (identity match on the dict ⇒ treated
     as already-dispositioned, never re-examined).

Both halves are required together: popping the marker without invalidating
the cursor is not enough (the bounded scan skips right past the dict without
looking at the marker), and invalidating the cursor without popping the
marker is not enough (the marker itself gates re-persistence). This file
proves each half is necessary and that fixed message_sanitization.py +
fixed conversation_loop.py together close the gap.
"""

import copy

import pytest

from agent.context_compressor import _DB_PERSISTED_MARKER
from agent.message_sanitization import (
    _sanitize_messages_non_ascii,
    _sanitize_messages_surrogates,
    _strip_images_from_messages,
)


class _FakeDB:
    """Minimal SessionDB stand-in — mirrors test_cursor_optimizations_parity.py."""

    def __init__(self):
        self.rows = []

    def append_messages_batch(self, session_id, messages, **kw):
        for m in messages:
            row = {k: copy.deepcopy(v) for k, v in m.items()}
            row["session_id"] = session_id
            self.rows.append(row)
        return list(range(1, len(messages) + 1))


def _make_agent():
    import run_agent as ra

    a = ra.AIAgent.__new__(ra.AIAgent)
    a.session_id = "s1"
    a._session_db = _FakeDB()
    a._session_db_created = True
    a._last_flushed_db_idx = 0
    a._flushed_db_message_ids = set()
    a._persist_disabled = False
    a._session_persist_lock = None
    a._db_flush_scan_prefix = None
    return a


class TestSurrogateSanitizerPopsMarker:
    """The sanitizer itself must pop the marker from any message it mutates,
    and must NOT touch it on messages it leaves alone."""

    def test_touched_message_loses_marker(self):
        messages = [
            {"role": "user", "content": "hi", _DB_PERSISTED_MARKER: True},
            {"role": "assistant", "content": "bad \ud800 surrogate", _DB_PERSISTED_MARKER: True},
        ]
        found = _sanitize_messages_surrogates(messages)
        assert found is True
        assert _DB_PERSISTED_MARKER not in messages[1]
        # The untouched message keeps its marker.
        assert messages[0].get(_DB_PERSISTED_MARKER) is True

    def test_no_surrogates_leaves_marker_untouched(self):
        messages = [{"role": "user", "content": "clean", _DB_PERSISTED_MARKER: True}]
        found = _sanitize_messages_surrogates(messages)
        assert found is False
        assert messages[0].get(_DB_PERSISTED_MARKER) is True


class TestNonAsciiSanitizerPopsMarker:
    def test_touched_message_loses_marker(self):
        messages = [
            {"role": "assistant", "content": "hello ⚕🤖 world", _DB_PERSISTED_MARKER: True},
        ]
        found = _sanitize_messages_non_ascii(messages)
        assert found is True
        assert _DB_PERSISTED_MARKER not in messages[0]

    def test_untouched_message_keeps_marker(self):
        messages = [{"role": "assistant", "content": "plain ascii", _DB_PERSISTED_MARKER: True}]
        found = _sanitize_messages_non_ascii(messages)
        assert found is False
        assert messages[0].get(_DB_PERSISTED_MARKER) is True


class TestStripImagesPopsMarker:
    def test_partial_strip_pops_marker(self):
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "look"},
                {"type": "image_url", "image_url": {"url": "data:..."}},
            ],
            _DB_PERSISTED_MARKER: True,
        }]
        found = _strip_images_from_messages(messages)
        assert found is True
        assert _DB_PERSISTED_MARKER not in messages[0]

    def test_tool_placeholder_replace_pops_marker(self):
        messages = [{
            "role": "tool",
            "tool_call_id": "call_1",
            "content": [{"type": "image_url", "image_url": {"url": "data:..."}}],
            _DB_PERSISTED_MARKER: True,
        }]
        found = _strip_images_from_messages(messages)
        assert found is True
        assert _DB_PERSISTED_MARKER not in messages[0]
        assert isinstance(messages[0]["content"], str)

    def test_deleted_message_needs_no_pop(self):
        """A synthetic image-only message is dropped entirely — nothing to pop."""
        messages = [{
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "data:..."}}],
            _DB_PERSISTED_MARKER: True,
        }]
        found = _strip_images_from_messages(messages)
        assert found is True
        assert messages == []


class TestFlushScanPrefixInvalidationContract:
    """End-to-end: an already-flushed message sanitized in place must
    actually reach state.db on the NEXT flush — proving both halves of the
    fix (marker pop + cursor invalidation) work together, mirroring
    conversation_loop.py's UnicodeEncodeError recovery block."""

    def test_sanitized_content_is_re_persisted_when_cursor_is_invalidated(self):
        agent = _make_agent()
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "bad \ud800 surrogate"},
        ]
        # First flush: both rows land, both dicts get the marker stamped.
        assert agent._flush_messages_to_session_db_unlocked(messages, None) is True
        assert agent._session_db.rows[-1]["content"] == "bad \ud800 surrogate"
        assert messages[1].get(_DB_PERSISTED_MARKER) is True

        # A later turn hits UnicodeEncodeError and sanitizes `messages` in
        # place (conversation_loop.py's recovery block) — same identity as
        # what's already in _db_flush_scan_prefix.
        found = _sanitize_messages_surrogates(messages)
        assert found is True
        assert "\ud800" not in messages[1]["content"]
        # Mirrors conversation_loop.py's fix: invalidate the cursor because
        # the sanitizer just popped a marker from a dict that may sit inside
        # the bounded-scan prefix.
        agent._db_flush_scan_prefix = None

        # Next flush must re-write the sanitized content.
        assert agent._flush_messages_to_session_db_unlocked(messages, None) is True
        assert "\ud800" not in agent._session_db.rows[-1]["content"]
        assert agent._session_db.rows[-1]["content"] == messages[1]["content"]

    def test_without_cursor_invalidation_the_sanitized_content_is_silently_dropped(self):
        """Mutation-verify companion: proves the cursor-invalidation half of
        the fix is load-bearing on its own, independent of the marker pop.
        Popping the marker but leaving the stale cursor in place reproduces
        the exact bug conversation_loop.py's fix closes — the bounded scan
        skips the identity-matched dict without ever looking at its marker."""
        agent = _make_agent()
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "bad \ud800 surrogate"},
        ]
        assert agent._flush_messages_to_session_db_unlocked(messages, None) is True

        found = _sanitize_messages_surrogates(messages)
        assert found is True
        assert _DB_PERSISTED_MARKER not in messages[1]
        # Deliberately do NOT invalidate agent._db_flush_scan_prefix here —
        # this is the pre-fix conversation_loop.py behavior.

        assert agent._flush_messages_to_session_db_unlocked(messages, None) is True
        # The bounded scan identity-matched the stale prefix and skipped the
        # dict entirely, so the DB still has the original bad bytes — the
        # sanitized content never arrived. This is the silent-staleness bug;
        # asserting it here documents why the cursor invalidation is
        # required, not optional.
        assert agent._session_db.rows[-1]["content"] == "bad \ud800 surrogate"
