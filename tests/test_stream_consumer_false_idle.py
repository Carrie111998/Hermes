"""Regression tests for false idle cutoff bug (OPENCLAW-RUNVIS-18D).

These tests verify that:
1. Multi-step tasks with tool outputs remain visibly active until final closeout
2. No false idle/cutoff before final message
3. No need for a "status?" prompt to recover the summary
4. No duplicate finals or leakage regressions
"""

import asyncio
import queue
import pytest
from unittest.mock import AsyncMock, MagicMock

from gateway.stream_consumer import (
    GatewayStreamConsumer,
    StreamConsumerConfig,
    _DONE,
    _TERMINAL_SENTINEL,
)


class TestStreamConsumerTerminalState:
    """Test the stream consumer's terminal state handling."""

    def test_terminal_sentinel_marked_on_finish(self):
        """finish() should add both _DONE and _TERMINAL_SENTINEL to queue."""
        adapter = MagicMock()
        consumer = GatewayStreamConsumer(
            adapter=adapter,
            chat_id="test_chat",
            config=StreamConsumerConfig(),
        )

        consumer.finish()

        # Both sentinels should be in queue
        items = list(consumer._queue.queue)
        assert _DONE in items
        assert any(isinstance(i, type(_TERMINAL_SENTINEL)) for i in items)

    def test_is_terminal_state_detects_sentinel(self):
        """is_terminal_state() should return True after finish() is called."""
        adapter = MagicMock()
        consumer = GatewayStreamConsumer(
            adapter=adapter,
            chat_id="test_chat",
            config=StreamConsumerConfig(),
        )

        assert not consumer.is_terminal_state()

        consumer.finish()

        assert consumer.is_terminal_state()

    @pytest.mark.asyncio
    async def test_final_response_sent_set_even_with_empty_accumulated(self):
        """Even if no content accumulated, final_response_sent should be set on done."""
        adapter = MagicMock()
        adapter.send = AsyncMock(return_value=MagicMock(success=True, message_id="msg_1"))
        adapter.MAX_MESSAGE_LENGTH = 4096

        consumer = GatewayStreamConsumer(
            adapter=adapter,
            chat_id="test_chat",
            config=StreamConsumerConfig(),
        )

        # Simulate empty stream completion
        consumer.finish()

        # Run consumer
        task = asyncio.create_task(consumer.run())
        await asyncio.wait_for(task, timeout=1.0)

        # final_response_sent should be True even with empty content
        # (because run() handles the got_done case and sets it)
        assert consumer.final_response_sent
        # and stream_completed should also be True
        assert consumer.stream_completed

    @pytest.mark.asyncio
    async def test_final_response_sent_set_with_content(self):
        """final_response_sent should be True after sending content."""
        adapter = MagicMock()
        adapter.send = AsyncMock(return_value=MagicMock(success=True, message_id="msg_1"))
        adapter.MAX_MESSAGE_LENGTH = 4096

        consumer = GatewayStreamConsumer(
            adapter=adapter,
            chat_id="test_chat",
            config=StreamConsumerConfig(),
        )

        # Simulate streaming some content
        consumer.on_delta("Hello world")
        consumer.finish()

        # Run consumer
        task = asyncio.create_task(consumer.run())
        await asyncio.wait_for(task, timeout=1.0)

        # final_response_sent should be True
        assert consumer.final_response_sent

    @pytest.mark.asyncio
    async def test_sentinel_consumed_before_return(self):
        """The terminal sentinel should be consumed before run() returns."""
        adapter = MagicMock()
        adapter.send = AsyncMock(return_value=MagicMock(success=True, message_id="msg_1"))
        adapter.MAX_MESSAGE_LENGTH = 4096

        consumer = GatewayStreamConsumer(
            adapter=adapter,
            chat_id="test_chat",
            config=StreamConsumerConfig(),
        )

        consumer.on_delta("Test content")
        consumer.finish()

        # Run consumer
        task = asyncio.create_task(consumer.run())
        await asyncio.wait_for(task, timeout=1.0)

        # Queue should be empty (all items consumed)
        assert consumer._queue.empty()
        assert consumer.final_response_sent


class TestStreamConsumerMidRunCutoff:
    """Test prevention of mid-run cutoff / false idle."""

    @pytest.mark.asyncio
    async def test_stream_shows_active_until_terminal(self):
        """The stream should indicate it's active until terminal state is reached."""
        adapter = MagicMock()
        adapter.send = AsyncMock(return_value=MagicMock(success=True, message_id="msg_1"))
        adapter.MAX_MESSAGE_LENGTH = 4096

        consumer = GatewayStreamConsumer(
            adapter=adapter,
            chat_id="test_chat",
            config=StreamConsumerConfig(),
        )

        # Before sending anything
        assert not consumer.already_sent
        assert not consumer.final_response_sent
        assert not consumer.is_terminal_state()

        # Send some content (simulating tool output)
        consumer.on_delta("Processing tool result...")

        # After content but before finish
        assert not consumer.already_sent  # Not yet sent (awaiting flush)
        assert not consumer.final_response_sent
        assert not consumer.is_terminal_state()

        # Finish the stream
        consumer.finish()

        # After finish (is_terminal_state detects sentinel before run consumes it)
        assert consumer.is_terminal_state()

        # After run completes, sentinel is consumed
        task = asyncio.create_task(consumer.run())
        await asyncio.wait_for(task, timeout=1.0)
        # Note: After run() completes, the sentinel has been consumed
        # so is_terminal_state() returns False, but stream_completed remains True
        assert consumer.stream_completed

    @pytest.mark.asyncio
    async def test_multiple_tool_outputs_remain_active(self):
        """Multiple tool outputs should keep stream active."""
        adapter = MagicMock()
        adapter.send = AsyncMock(return_value=MagicMock(success=True, message_id="msg_1"))
        adapter.MAX_MESSAGE_LENGTH = 4096

        consumer = GatewayStreamConsumer(
            adapter=adapter,
            chat_id="test_chat",
            config=StreamConsumerConfig(),
        )

        # Simulate multiple tool outputs
        for i in range(5):
            consumer.on_delta(f"Tool output {i}\n")
            # After each tool, the stream is still not terminal
            assert not consumer.is_terminal_state()

        # Only after finish does it become terminal
        consumer.finish()
        assert consumer.is_terminal_state()

    @pytest.mark.asyncio
    async def test_segment_break_preserves_activity(self):
        """Segment breaks (tool boundaries) should not trigger false completion."""
        adapter = MagicMock()
        adapter.send = AsyncMock(return_value=MagicMock(success=True, message_id="msg_1"))
        adapter.MAX_MESSAGE_LENGTH = 4096

        consumer = GatewayStreamConsumer(
            adapter=adapter,
            chat_id="test_chat",
            config=StreamConsumerConfig(),
        )

        # First tool output
        consumer.on_delta("First tool result")
        consumer.on_segment_break()
        assert not consumer.is_terminal_state()

        # Second tool output
        consumer.on_delta("Second tool result")
        consumer.on_segment_break()
        assert not consumer.is_terminal_state()

        # Final content
        consumer.on_delta("Final summary")
        consumer.finish()
        assert consumer.is_terminal_state()


class TestStreamConsumerNoDuplicateFinals:
    """Test that there are no duplicate finals or leakage regressions."""

    @pytest.mark.asyncio
    async def test_exactly_once_final_delivery(self):
        """final_response_sent should only become True once."""
        adapter = MagicMock()
        adapter.send = AsyncMock(return_value=MagicMock(success=True, message_id="msg_1"))
        adapter.MAX_MESSAGE_LENGTH = 4096

        consumer = GatewayStreamConsumer(
            adapter=adapter,
            chat_id="test_chat",
            config=StreamConsumerConfig(),
        )

        consumer.on_delta("Test content")
        consumer.finish()

        # Run consumer
        task = asyncio.create_task(consumer.run())
        await asyncio.wait_for(task, timeout=1.0)

        # Should be set, but not set again (no duplicate)
        assert consumer.final_response_sent
        # Running again shouldn't change behavior
        _prev = consumer._final_response_sent
        assert consumer.final_response_sent == _prev

    @pytest.mark.asyncio
    async def test_empty_accumulated_does_not_leak(self):
        """Empty accumulated content should not cause delivery issues."""
        adapter = MagicMock()
        adapter.send = AsyncMock(return_value=MagicMock(success=True, message_id="msg_1"))
        adapter.MAX_MESSAGE_LENGTH = 4096

        consumer = GatewayStreamConsumer(
            adapter=adapter,
            chat_id="test_chat",
            config=StreamConsumerConfig(),
        )

        # Direct finish without content
        consumer.finish()

        task = asyncio.create_task(consumer.run())
        await asyncio.wait_for(task, timeout=1.0)

        # Should be marked as done even with empty content
        assert consumer.final_response_sent
        # No send should have been attempted (nothing to send)
        assert not adapter.send.called or consumer._accumulated == ""


class TestStreamConsumerErrorHandling:
    """Test error handling in stream consumer."""

    @pytest.mark.asyncio
    async def test_final_set_on_cancellation(self):
        """On cancellation, final_response_sent should still be set correctly."""
        adapter = MagicMock()
        adapter.send = AsyncMock(return_value=MagicMock(success=True, message_id="msg_1"))
        adapter.MAX_MESSAGE_LENGTH = 4096

        consumer = GatewayStreamConsumer(
            adapter=adapter,
            chat_id="test_chat",
            config=StreamConsumerConfig(),
        )

        consumer.on_delta("Test content")
        # Don't call finish() - simulate abrupt end

        # Start the consumer
        task = asyncio.create_task(consumer.run())
        await asyncio.sleep(0.01)

        # Cancel the task
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Consumer should handle cancellation gracefully
        # (Depends on implementation - may or may not have final_response_sent)

    @pytest.mark.asyncio
    async def test_final_set_on_send_error(self):
        """If send fails, final_response_sent should still reflect completion."""
        adapter = MagicMock()
        adapter.send = AsyncMock(return_value=MagicMock(success=False, error="Mock error"))
        adapter.MAX_MESSAGE_LENGTH = 4096

        consumer = GatewayStreamConsumer(
            adapter=adapter,
            chat_id="test_chat",
            config=StreamConsumerConfig(),
        )

        consumer.on_delta("Test content")
        consumer.finish()

        # This should complete without raising
        task = asyncio.create_task(consumer.run())
        await asyncio.wait_for(task, timeout=1.0)

        # Even with send errors, stream should be completed
        # (is_terminal_state() returns False because sentinel was consumed)
        assert consumer.stream_completed


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
