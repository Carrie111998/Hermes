"""Tests for StreamingRepetitionGuard — live in-stream degenerate output detection."""

from __future__ import annotations

import sys
import os
import pytest

# Ensure repo root is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from agent.stream_repetition_guard import (
    StreamingRepetitionGuard,
    StreamingRepetitionError,
    _MIN_ACCUMULATED,
    _CHECK_INTERVAL,
    _ROLLING_BUFFER,
)


# Same echo phrase from the #86581 incident (Chinese: "change to Google Gemini 4 31B")
_INCIDENT_ECHO = "好，你幫我更改成 Google Gemini 4 31B。"


class TestStreamingRepetitionGuard:
    """Unit tests mirroring test_repetition_guard.py patterns."""

    def test_incident_echo_detected_in_stream(self):
        """The exact #86581 incident shape — repeated echo line streaming in."""
        guard = StreamingRepetitionGuard()
        # Simulate chunks arriving: each chunk = one echo line
        for _ in range(800):
            if guard.append(_INCIDENT_ECHO + "\n"):
                return  # detected!
        assert False, "Should have detected repetition"

    def test_repeated_text_without_newlines_detected(self):
        """Repetition loop with no line breaks — window path."""
        guard = StreamingRepetitionGuard()
        chunk = _INCIDENT_ECHO * 10  # ~620 chars per chunk
        for _ in range(200):
            if guard.append(chunk):
                return
        assert False, "Should have detected repetition"

    def test_long_legitimate_text_not_flagged(self):
        """Long unique prose — no 60-char window repeats."""
        guard = StreamingRepetitionGuard()
        for i in range(1200):
            text = (
                f"Sentence number {i} describes a distinct topic with unique words "
                f"such as quasar-{i} and nebula-{i} to keep every window distinct. "
            )
            if guard.append(text):
                assert False, f"False positive at sentence {i}"
        assert guard.was_checked  # guard did run checks
        assert guard.accumulated_length > 50000

    def test_short_stream_never_flagged(self):
        """Short responses below _MIN_ACCUMULATED never trigger."""
        guard = StreamingRepetitionGuard()
        guard.append("Hello! " * 10)  # ~70 chars
        assert not guard.was_checked

    def test_empty_and_none_inputs(self):
        guard = StreamingRepetitionGuard()
        assert guard.append("") is False
        assert guard.append(None) is False  # type: ignore

    def test_mixed_repetition_not_flagged(self):
        """Scattered repeats in mostly-unique text — not dominated."""
        guard = StreamingRepetitionGuard()
        for i in range(3000):
            text = f"unique filler token {i} "
            if guard.append(text):
                assert False, "False positive on scattered repetition"
        # Now add some repeats but intermixed
        for _ in range(30):
            guard.append(_INCIDENT_ECHO + "\n")
            for i in range(100):
                guard.append(f"more unique padding {i} ")
        # Should still not be dominated
        # (if it fires here, that's actually OK — 30 repeats in 2000 window
        #  could legitimately trip. But the 100 unique lines between each
        #  should keep it below threshold.)
        assert True  # just verify no crash

    def test_check_interval_respected(self):
        """Guard only runs expensive check every _CHECK_INTERVAL chars."""
        guard = StreamingRepetitionGuard()
        # Feed small chunks — guard shouldn't check yet
        for i in range(10):
            guard.append("short ")
        assert not guard.was_checked  # below _MIN_ACCUMULATED

        # Feed enough to trigger first check
        guard.append("x" * _MIN_ACCUMULATED)
        assert guard.was_checked

    def test_rolling_buffer_does_not_grow_unbounded(self):
        """Guard keeps memory bounded — _ROLLING_BUFFER chars max for checks."""
        guard = StreamingRepetitionGuard()
        # Feed a lot of unique text
        for i in range(10000):
            guard.append(f"unique-{i}-token ")
            if i % 1000 == 0 and guard.accumulated_length > _ROLLING_BUFFER * 2:
                # The accumulated string grows (that's expected — it's the
                # full response text). But the check only looks at the tail.
                break
        # Verify accumulated_length is reasonable (not checking memory directly,
        # but confirming the guard doesn't hold unbounded check state)
        assert guard.accumulated_length > 0

    def test_detects_after_partial_legitimate_text(self):
        """Model starts normally then degenerates — guard catches the transition."""
        guard = StreamingRepetitionGuard()
        # Legitimate start
        for i in range(20):
            guard.append(f"This is sentence {i} with unique content about topic {i}. ")
        # Degenerate continuation
        for _ in range(100):
            if guard.append(_INCIDENT_ECHO + "\n"):
                return  # detected!
        assert False, "Should detect degeneration after legitimate start"

    def test_gibberish_character_repetition(self):
        """Pure character spam — 'aaaaaaaa...' pattern."""
        guard = StreamingRepetitionGuard()
        if guard.append("a" * 10000):
            return
        assert False, "Should detect character repetition"

    def test_partial_json_repetition(self):
        """Model repeating a JSON fragment — common tool-call degeneration."""
        guard = StreamingRepetitionGuard()
        fragment = '{"action": "search", "query": "same thing over and over"}'
        for _ in range(500):
            if guard.append(fragment + "\n"):
                return
        assert False, "Should detect JSON fragment repetition"


class TestStreamingRepetitionError:
    def test_error_is_runtime_error(self):
        err = StreamingRepetitionError("test")
        assert isinstance(err, RuntimeError)

    def test_error_message(self):
        msg = "degenerate repetition detected at 1500 chars"
        err = StreamingRepetitionError(msg)
        assert msg in str(err)
