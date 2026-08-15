"""Tests for Discord recovery cursor correctness (R3)."""

import pytest

from plugins.platforms.discord.recovery_cursor import (
    Cursor,
    RecoveryCursorError,
    RecoveryCursorManager,
)


class TestAdvance:
    def test_sets_max_of_message_ids(self):
        mgr = RecoveryCursorManager()
        cursor = mgr.advance("111", ["5", "3", "9"], has_more=True)
        assert cursor.channel_id == "111"
        assert cursor.last_message_id == "9"
        assert cursor.has_more is True

    def test_accepts_int_message_ids(self):
        mgr = RecoveryCursorManager()
        cursor = mgr.advance("111", [5, 9, 3], has_more=False)
        assert cursor.last_message_id == "9"

    def test_monotonic_forward_moves(self):
        mgr = RecoveryCursorManager()
        mgr.advance("111", ["10"], has_more=True)
        cursor = mgr.advance("111", ["11", "12"], has_more=False)
        assert cursor.last_message_id == "12"
        assert cursor.has_more is False

    def test_equal_max_is_allowed(self):
        mgr = RecoveryCursorManager()
        mgr.advance("111", ["10"], has_more=True)
        cursor = mgr.advance("111", ["10"], has_more=False)
        assert cursor.last_message_id == "10"
        assert cursor.has_more is False

    def test_backward_move_raises(self):
        mgr = RecoveryCursorManager()
        mgr.advance("111", ["100"], has_more=True)
        with pytest.raises(RecoveryCursorError):
            mgr.advance("111", ["99"], has_more=True)

    def test_backward_error_is_value_error(self):
        mgr = RecoveryCursorManager()
        mgr.advance("111", ["100"], has_more=True)
        with pytest.raises(ValueError):
            mgr.advance("111", ["1"], has_more=True)

    def test_channels_are_independent(self):
        mgr = RecoveryCursorManager()
        mgr.advance("111", ["100"], has_more=True)
        cursor = mgr.advance("222", ["1"], has_more=True)
        assert cursor.last_message_id == "1"

    def test_empty_message_ids_raises(self):
        mgr = RecoveryCursorManager()
        with pytest.raises(ValueError):
            mgr.advance("111", [], has_more=True)

    def test_invalid_channel_id_raises(self):
        mgr = RecoveryCursorManager()
        with pytest.raises(ValueError):
            mgr.advance("not-a-snowflake", ["1"], has_more=True)

    @pytest.mark.parametrize("bad", ["abc", "12a", "1.5", "-3", ""])
    def test_invalid_message_id_raises(self, bad):
        mgr = RecoveryCursorManager()
        with pytest.raises(ValueError):
            mgr.advance("111", ["1", bad], has_more=True)


class TestDedup:
    def test_filters_global_seen_ids(self):
        mgr = RecoveryCursorManager()
        seen = {"5"}
        result = mgr.dedup("111", ["1", "5", "9"], seen)
        assert result == ["1", "9"]
        assert seen == {"5", "1", "9"}

    def test_filters_per_channel_even_with_fresh_seen_set(self):
        mgr = RecoveryCursorManager()
        mgr.dedup("111", ["1", "2"], set())
        fresh = set()
        result = mgr.dedup("111", ["2", "3"], fresh)
        assert result == ["3"]
        assert fresh == {"3"}

    def test_per_channel_isolation(self):
        mgr = RecoveryCursorManager()
        mgr.dedup("111", ["1"], set())
        fresh = set()
        result = mgr.dedup("222", ["1", "2"], fresh)
        assert result == ["1", "2"]

    def test_invalid_message_id_raises(self):
        mgr = RecoveryCursorManager()
        with pytest.raises(ValueError):
            mgr.dedup("111", ["1", "nope"], set())

    def test_invalid_channel_id_raises(self):
        mgr = RecoveryCursorManager()
        with pytest.raises(ValueError):
            mgr.dedup("bad", ["1"], set())


class TestCursorFor:
    def test_returns_none_for_unknown_channel(self):
        mgr = RecoveryCursorManager()
        assert mgr.cursor_for("111") is None

    def test_returns_recorded_cursor(self):
        mgr = RecoveryCursorManager()
        mgr.advance("111", ["1", "2"], has_more=True)
        cursor = mgr.cursor_for("111")
        assert isinstance(cursor, Cursor)
        assert cursor.channel_id == "111"
        assert cursor.last_message_id == "2"
        assert cursor.has_more is True
