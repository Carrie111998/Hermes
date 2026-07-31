"""Tests for GatewayStreamConsumer._clean_for_display — secret redaction.

Regression tests for the streaming-path secret redaction gap.
The streaming path must redact secrets and strip tool-trace banners
in every chunk, including finalized split-message chunks that are
never edited again.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig


_BOUNDARY_SECRET = "sk-abc123def456ghi789jkl012mno345pqr678stu"


def _overflow_adapter() -> MagicMock:
    """Return an adapter that records the real streaming send/edit paths."""
    adapter = MagicMock()
    adapter.MAX_MESSAGE_LENGTH = 601  # run() clamps its safe streaming limit to 500.
    adapter.REQUIRES_EDIT_FINALIZE = False
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id="sent"))
    adapter.edit_message = AsyncMock(return_value=SimpleNamespace(success=True, message_id="edited"))
    return adapter


def _delivered_text(adapter: MagicMock) -> str:
    """Reconstruct visible text in platform delivery order."""
    sent = [call.kwargs["content"] for call in adapter.send.await_args_list]
    edited = [call.kwargs["content"] for call in adapter.edit_message.await_args_list]
    return "".join((*edited, *sent))


def _boundary_crossing_response() -> str:
    """Place a redactable token across the consumer's 500-character split."""
    return "x" * 496 + " " + _BOUNDARY_SECRET + "\ncomplete"


class TestCleanForDisplaySecretRedaction:
    """Verify _clean_for_display redacts secrets and strips banners."""

    def test_media_tags_still_stripped(self):
        """Existing behavior: MEDIA: tags are removed."""
        text = "Hello MEDIA:/path/to/file.png world"
        result = GatewayStreamConsumer._clean_for_display(text)
        assert "MEDIA:" not in result
        assert "Hello" in result
        assert "world" in result

    def test_audio_as_voice_still_stripped(self):
        """Existing behavior: [[audio_as_voice]] directives are removed."""
        text = "Hello [[audio_as_voice]] world"
        result = GatewayStreamConsumer._clean_for_display(text)
        assert "[[audio_as_voice]]" not in result

    def test_normal_text_preserved(self):
        """Normal text without secrets passes through unchanged."""
        text = "Hello world, this is a normal response."
        result = GatewayStreamConsumer._clean_for_display(text)
        assert result == text

    def test_api_key_redacted(self):
        """API keys in streamed text must be redacted."""
        text = f"Here is your key: {_BOUNDARY_SECRET}"
        result = GatewayStreamConsumer._clean_for_display(text)
        # The redactor preserves first 6 + last 4 chars for long tokens
        assert _BOUNDARY_SECRET not in result
        assert "sk-abc" in result  # prefix preserved
        assert "8stu" in result  # suffix preserved

    def test_tool_trace_banner_stripped(self):
        """Tool-trace banners in streamed text must be stripped."""
        text = "Done.\n⚠️ 🛠️ `search repos (agent)` failed"
        result = GatewayStreamConsumer._clean_for_display(text)
        assert result == "Done."
        assert "failed" not in result

    def test_empty_text_returns_empty(self):
        """Empty text returns empty string."""
        result = GatewayStreamConsumer._clean_for_display("")
        assert result == ""

    def test_whitespace_only_returns_empty(self):
        """Whitespace-only text returns empty string after rstrip."""
        result = GatewayStreamConsumer._clean_for_display("   \n\n  ")
        assert result == ""


class TestOverflowSecretRedaction:
    """Overflow must sanitize the complete buffer before splitting it."""

    def test_first_message_overflow_cannot_reconstruct_cross_boundary_secret(self):
        """The no-message overflow branch never delivers reconstructable fragments."""
        adapter = _overflow_adapter()
        consumer = GatewayStreamConsumer(
            adapter,
            "chat_1",
            StreamConsumerConfig(buffer_threshold=1),
        )
        consumer.on_delta(_boundary_crossing_response())
        consumer.finish()

        asyncio.run(consumer.run())

        assert _BOUNDARY_SECRET not in _delivered_text(adapter)

    def test_existing_message_overflow_cannot_reconstruct_cross_boundary_secret(self):
        """The existing-message overflow branch never delivers reconstructable fragments."""
        adapter = _overflow_adapter()
        consumer = GatewayStreamConsumer(
            adapter,
            "chat_1",
            StreamConsumerConfig(buffer_threshold=1),
        )
        consumer._message_id = "preview"
        consumer._already_sent = True
        consumer.on_delta(_boundary_crossing_response())
        consumer.finish()

        asyncio.run(consumer.run())

        assert _BOUNDARY_SECRET not in _delivered_text(adapter)
