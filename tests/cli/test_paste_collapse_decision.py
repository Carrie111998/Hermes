"""Tests for the CLI paste-collapse decision (bracketed-paste + fallback).

Regression for the whitespace-free single-line data dump: a ~1-2KB paste of
unit/ID stream data (no newlines, no whitespace) sat below both the 5-line
threshold and the 2000-char guard and was dumped raw into the input box.
"""

from cli import _should_collapse_paste

LINES = 5
CHARS = 2000
DATA = 500

# Representative of the reported blob: whitespace-free single-line synthesis
# unit/timing data, ~680 chars — under CHARS, over DATA.
DATA_BLOB = "".join(f"1{i}M35;{i + 8};" for i in range(80))


class TestShouldCollapsePaste:

    def test_collapses_multiline_at_line_threshold(self):
        assert _should_collapse_paste("a\nb\nc\nd\ne", 5, LINES, CHARS, DATA) is True
        assert _should_collapse_paste("a\nb\nc\nd", 4, LINES, CHARS, DATA) is False

    def test_collapses_long_single_line_at_char_threshold(self):
        assert _should_collapse_paste("x" * CHARS, 1, LINES, CHARS, DATA) is True

    def test_collapses_whitespace_free_data_dump_below_char_threshold(self):
        assert len(DATA_BLOB) > DATA
        assert len(DATA_BLOB) < CHARS
        assert " " not in DATA_BLOB and "\n" not in DATA_BLOB
        assert _should_collapse_paste(DATA_BLOB, 1, LINES, CHARS, DATA) is True

    def test_keeps_prose_raw(self):
        prose = "the quick brown fox jumps over the lazy dog " * 12
        assert len(prose) > DATA
        assert _should_collapse_paste(prose, 1, LINES, CHARS, DATA) is False

    def test_keeps_short_whitespace_free_line_raw(self):
        assert _should_collapse_paste("1M35;9;2M35;11", 1, LINES, CHARS, DATA) is False

    def test_data_guard_disable_with_zero(self):
        assert _should_collapse_paste(DATA_BLOB, 1, LINES, CHARS, 0) is False

    def test_char_guard_disable_with_zero(self):
        # Whitespace present, so only the char guard could collapse it.
        assert _should_collapse_paste("x " * 2000, 1, LINES, 0, DATA) is False

    def test_line_guard_disable_with_zero(self):
        assert _should_collapse_paste("a\nb\nc\nd\ne\nf", 6, 0, CHARS, DATA) is False
