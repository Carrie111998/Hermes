"""Tests for Discord feature T2: thread session isolation + history partition."""

import pytest

from plugins.platforms.discord.thread_context import (
    HistoryPartition,
    ThreadContextError,
    ThreadSessionRegistry,
)


def msg(message_id, **extra):
    data = {"id": message_id}
    data.update(extra)
    return data


class TestThreadContextError:
    def test_is_value_error_subclass(self):
        assert issubclass(ThreadContextError, ValueError)


class TestThreadSessionRegistry:
    def test_distinct_keys_for_different_threads(self):
        registry = ThreadSessionRegistry()
        assert registry.key_for(100, 1) != registry.key_for(100, 2)

    def test_distinct_keys_for_different_channels(self):
        registry = ThreadSessionRegistry()
        assert registry.key_for(100, 1) != registry.key_for(200, 1)

    def test_key_is_stable_and_normalized(self):
        registry = ThreadSessionRegistry()
        assert registry.key_for(100, 1) == registry.key_for("100", "1")
        assert registry.key_for(100, 1) == registry.key_for(100, 1)

    def test_is_isolated_same_key_false(self):
        registry = ThreadSessionRegistry()
        key = registry.key_for(100, 1)
        assert registry.is_isolated(key, key) is False

    def test_is_isolated_different_thread_true(self):
        registry = ThreadSessionRegistry()
        assert (
            registry.is_isolated(
                registry.key_for(100, 1), registry.key_for(100, 2)
            )
            is True
        )

    def test_is_isolated_different_channel_true(self):
        registry = ThreadSessionRegistry()
        assert (
            registry.is_isolated(
                registry.key_for(100, 1), registry.key_for(200, 1)
            )
            is True
        )

    def test_register_session_for_unregister_roundtrip(self):
        registry = ThreadSessionRegistry()
        key = registry.register(100, 1, "session-a")
        assert key == registry.key_for(100, 1)
        assert registry.session_for(100, 1) == "session-a"
        registry.unregister(100, 1)
        assert registry.session_for(100, 1) is None

    def test_unregister_missing_is_idempotent(self):
        registry = ThreadSessionRegistry()
        registry.unregister(999, 888)  # must not raise

    def test_register_overwrites_session(self):
        registry = ThreadSessionRegistry()
        registry.register(100, 1, "session-a")
        registry.register(100, 1, "session-b")
        assert registry.session_for(100, 1) == "session-b"

    def test_sessions_stay_isolated_between_threads(self):
        registry = ThreadSessionRegistry()
        registry.register(100, 1, "session-a")
        registry.register(100, 2, "session-b")
        assert registry.session_for(100, 1) == "session-a"
        assert registry.session_for(100, 2) == "session-b"

    def test_invalid_channel_raises(self):
        registry = ThreadSessionRegistry()
        with pytest.raises(ThreadContextError):
            registry.key_for("not-a-snowflake", 1)

    def test_invalid_thread_raises(self):
        registry = ThreadSessionRegistry()
        with pytest.raises(ThreadContextError):
            registry.register(100, -5, "session-a")

    def test_invalid_session_id_raises(self):
        registry = ThreadSessionRegistry()
        with pytest.raises(ThreadContextError):
            registry.register(100, 1, "  ")


class TestHistoryPartition:
    def test_partition_empty(self):
        partitioner = HistoryPartition()
        assert partitioner.partition([], [], thread_id=42) == {
            "parent": [],
            "thread": [],
        }

    def test_partition_dedup_message_in_both_only_in_thread(self):
        partitioner = HistoryPartition()
        shared = msg(7, content="thread reply")
        parent_messages = [msg(1), shared, msg(2)]
        thread_messages = [msg(8), shared]
        result = partitioner.partition(
            parent_messages, thread_messages, thread_id=42
        )
        assert [m["id"] for m in result["parent"]] == [1, 2]
        assert [m["id"] for m in result["thread"]] == [8, 7]
        assert shared not in result["parent"]

    def test_partition_thread_messages_kept_whole(self):
        partitioner = HistoryPartition()
        thread_messages = [msg(5), msg(6)]
        result = partitioner.partition(
            [msg(1), msg(2)], thread_messages, thread_id=42
        )
        assert result["thread"] == thread_messages

    def test_partition_disjoint_lists_unchanged(self):
        partitioner = HistoryPartition()
        parent_messages = [msg(1), msg(2)]
        thread_messages = [msg(3), msg(4)]
        result = partitioner.partition(
            parent_messages, thread_messages, thread_id=42
        )
        assert [m["id"] for m in result["parent"]] == [1, 2]
        assert [m["id"] for m in result["thread"]] == [3, 4]

    def test_invalid_thread_id_raises(self):
        partitioner = HistoryPartition()
        with pytest.raises(ThreadContextError):
            partitioner.partition([], [], thread_id="abc")
        with pytest.raises(ValueError):
            partitioner.partition([], [], thread_id=-5)

    def test_invalid_message_raises(self):
        partitioner = HistoryPartition()
        with pytest.raises(ThreadContextError):
            partitioner.partition([{"no_id": True}], [], thread_id=42)

    def test_non_list_input_raises(self):
        partitioner = HistoryPartition()
        with pytest.raises(ThreadContextError):
            partitioner.partition("nope", [], thread_id=42)
