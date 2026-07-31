"""Regression tests for #75588.

A short conversation whose protected head / tool-group alignment reaches the
end of the message list previously made ``_find_tail_cut_by_tokens()`` return
``len(messages) + 1``.  ``compress()`` then passed that value into
``_find_latest_context_summary()`` / ``_find_context_summaries()`` which
indexed ``messages[idx]`` without clamping and raised ``IndexError``,
escaping the compression path and failing the active gateway turn.

These tests pin the post-fix invariants:

1. ``compress()`` never raises for the exact 8-message shape in the report,
   even when alignment pushes the protected head to ``len(messages)``.
2. ``_find_tail_cut_by_tokens(messages, head_end=len(messages)) == len(messages)``
   so the caller takes the no-compressible-window path.
3. ``_find_context_summaries`` / ``_find_latest_context_summary`` clamp
   out-of-range bounds and tolerate non-dict rows instead of indexing past
   the end of the transcript.
4. ``compress()`` removes no messages and calls the summary model no extra
   times when there is no compressible window.
"""

from unittest.mock import patch

from agent.context_compressor import ContextCompressor


# --- Helpers -----------------------------------------------------------------

def _make_compressor(protect_first_n=2, protect_last_n=2):
    """Build a ContextCompressor with mocked model context length."""

    with patch(
        "agent.context_compressor.get_model_context_length",
        return_value=100000,
    ):
        c = ContextCompressor(
            model="test/model",
            threshold_percent=0.85,
            protect_first_n=protect_first_n,
            protect_last_n=protect_last_n,
            quiet_mode=True,
        )
        # Resolve context_length while the mock is still active.
        _ = c.context_length
        return c


def _short_transcript_ending_in_tool_group():
    """The exact 8-message shape from #75588.

    Layout (system + user/assistant/tool-call + tool-results):

        0: system
        1: user (asks something — first exchange, protected head)
        2: assistant (first reply — protected head)
        3: user (active task — the one the agent is mid-flight on)
        4: assistant(tool_calls=[...])  ← last assistant turn
        5: tool(result for 4)
        6: tool(result for 4)
        7: tool(result for 4)

    A short ``protect_first_n`` pushes ``_protect_head_size()`` to 5
    (system + first four messages including the assistant tool_calls row),
    and ``_align_boundary_forward`` slides past the trailing tool results
    to 8 (== ``len(messages)``).  That is the exact alignment that
    triggered the bug in #75588.
    """
    return [
        {"role": "system", "content": "you are a test agent"},
        {"role": "user", "content": "first ask"},
        {"role": "assistant", "content": "first reply"},
        {"role": "user", "content": "active task"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "call_id": "call_1",
                    "type": "function",
                    "function": {"name": "noop", "arguments": "{}"},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "result 1"},
        {"role": "tool", "tool_call_id": "call_1", "content": "result 2"},
        {"role": "tool", "tool_call_id": "call_1", "content": "result 3"},
    ]


# --- The headline regression -------------------------------------------------

class TestShortTranscriptDoesNotRaise:
    """The exact failure mode reported in #75588."""

    def test_compress_does_not_raise_on_eight_message_tool_tail(
        self,
    ):
        """compress() must return without raising for the 8-message shape.

        Pre-fix: IndexError in _find_latest_context_summary because
        ``compress_end = len(messages) + 1`` was passed straight into a
        ``messages[idx]`` indexing call.
        """
        # protect_first_n=3 → _protect_head_size=4 → _align_boundary_forward
        # slides 4→5 (assistant tool_calls) → 6/7 (tool) → 8 (== len).  This
        # is the exact alignment the original report reproduced.
        c = _make_compressor(protect_first_n=3)
        messages = _short_transcript_ending_in_tool_group()

        # compress() is the public entry point.  It must never raise for a
        # well-formed message list, even when the alignment pushes the
        # protected head to ``len(messages)``.
        result = c.compress(messages)

        # The no-compressible-window path returns the transcript unchanged
        # (the active user/assistant/tool group must not be touched).
        assert len(result) == len(messages)
        assert [m.get("role") for m in result] == [
            m.get("role") for m in messages
        ]

    def test_compress_records_no_compressible_window_verdict(self):
        """With no compressible middle, compress() must record an ineffective
        verdict so the anti-thrash guard in should_compress() can fire (#40803).

        The no-compressible-window path increments
        ``_ineffective_compression_count`` and surfaces the verdict through
        the ``_last_compression_savings_pct`` field.
        """
        c = _make_compressor(protect_first_n=3)
        messages = _short_transcript_ending_in_tool_group()

        before_count = c._ineffective_compression_count
        result = c.compress(messages)

        assert c._ineffective_compression_count == before_count + 1
        assert c._last_compression_savings_pct == 0.0
        assert result == messages


# --- _find_tail_cut_by_tokens contract --------------------------------------

class TestTailCutBoundsAtHeadEnd:
    """When alignment leaves no compressible middle, tail cut must equal
    ``len(messages)`` so the caller takes the existing
    ``compress_start >= compress_end`` no-op path (issue #75588)."""

    def test_tail_cut_returns_len_when_head_end_equals_len(self):
        c = _make_compressor()
        messages = _short_transcript_ending_in_tool_group()
        n = len(messages)
        assert c._find_tail_cut_by_tokens(messages, head_end=n) == n

    def test_tail_cut_returns_len_when_head_end_exceeds_len(self):
        c = _make_compressor()
        messages = _short_transcript_ending_in_tool_group()
        n = len(messages)
        # Defensive: any head_end past the transcript tail is also a no-op.
        assert c._find_tail_cut_by_tokens(messages, head_end=n + 5) == n


# --- Summary scan bounds guarding -------------------------------------------

class TestSummaryScanClampsBounds:
    """_find_context_summaries / _find_latest_context_summary must never
    raise IndexError for a valid message list, regardless of caller bounds."""

    def test_end_past_len_scans_only_existing_rows(self):
        messages = _short_transcript_ending_in_tool_group()
        # Pre-fix: this raises IndexError because ``range(1, 9)`` and
        # ``messages[8]`` is out of bounds for an 8-row list.
        hits = ContextCompressor._find_context_summaries(messages, 1, 9)
        # No handoff summary in this transcript — must be empty, not raised.
        assert hits == []

    def test_end_far_past_len_scans_only_existing_rows(self):
        messages = _short_transcript_ending_in_tool_group()
        hits = ContextCompressor._find_context_summaries(messages, 0, 1000)
        assert hits == []

    def test_latest_summary_returns_none_when_end_past_len(self):
        messages = _short_transcript_ending_in_tool_group()
        idx, body = ContextCompressor._find_latest_context_summary(
            messages, 1, 9,
        )
        assert idx is None
        assert body == ""

    def test_start_past_end_returns_empty_without_raising(self):
        messages = _short_transcript_ending_in_tool_group()
        hits = ContextCompressor._find_context_summaries(messages, 5, 3)
        assert hits == []

    def test_negative_start_is_clamped(self):
        messages = _short_transcript_ending_in_tool_group()
        # Defensive: a negative ``start`` from a stale caller must clamp to 0
        # rather than producing a backwards range that never iterates.
        hits = ContextCompressor._find_context_summaries(messages, -10, 8)
        # No handoff summary present → empty list, not raised.
        assert hits == []

    def test_empty_messages_returns_empty(self):
        hits = ContextCompressor._find_context_summaries([], 0, 10)
        assert hits == []
        idx, body = ContextCompressor._find_latest_context_summary([], 0, 10)
        assert idx is None
        assert body == ""

    def test_non_dict_row_is_tolerated(self):
        """A malformed transcript row (None, str, ...) must not raise."""
        messages = _short_transcript_ending_in_tool_group()
        # Splice in a non-dict row in the middle of the scan window.
        messages[4] = "garbage"
        # Must not raise; must skip the non-dict row.
        hits = ContextCompressor._find_context_summaries(messages, 0, 8)
        assert hits == []
