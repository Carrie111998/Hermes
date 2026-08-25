"""Tests for delta.content list normalization in the streaming path.

Mistral's relay serving GLM-5.2 (and other OpenAI-compatible relays) can
emit ``delta.content`` as a list of multi-part blocks instead of a plain
string — e.g. ``[{"type": "text", "text": "..."}]``. The streaming
accumulator in chat_completion_helpers assumed a string and crashed with
``AttributeError: 'list' object has no attribute 'lstrip'``, failing the
whole API call twice (with retries) before delivery.

The fix normalizes ``delta.content`` to plain text at the accumulation
point (_normalize_stream_content_to_text) and hardens
_provider_stream_text_may_be_sse against non-str input.
"""

from agent.chat_completion_helpers import (
    _normalize_stream_content_to_text,
    _provider_stream_text_may_be_sse,
)


class TestNormalizeStreamContentToText:
    def test_str_passthrough(self):
        assert _normalize_stream_content_to_text("hello") == "hello"
        assert _normalize_stream_content_to_text("") == ""

    def test_list_of_text_blocks(self):
        content = [
            {"type": "text", "text": "Bonjour "},
            {"type": "text", "text": "monsieur"},
        ]
        assert _normalize_stream_content_to_text(content) == "Bonjour monsieur"

    def test_mixed_list_with_raw_strings(self):
        content = ["abc", {"type": "text", "text": "def"}]
        assert _normalize_stream_content_to_text(content) == "abcdef"

    def test_non_text_blocks_are_dropped(self):
        content = [
            {"type": "text", "text": "keep"},
            {"type": "image_url", "image_url": {"url": "x"}},
            {"type": "text", "text": "me"},
        ]
        assert _normalize_stream_content_to_text(content) == "keepme"

    def test_invalid_shapes_degrade_to_empty(self):
        assert _normalize_stream_content_to_text(None) == ""
        assert _normalize_stream_content_to_text(42) == ""
        assert _normalize_stream_content_to_text([{"type": "text"}]) == ""

    def test_empty_list(self):
        assert _normalize_stream_content_to_text([]) == ""

    def test_accumulated_output_is_join_safe(self):
        # Mirrors "".join(content_parts) in the streaming accumulator: the
        # normalized parts must all be strings regardless of delta shape.
        deltas = [
            [{"type": "text", "text": "A"}],
            "B",
            [{"type": "text", "text": "C"}],
        ]
        accumulated = [_normalize_stream_content_to_text(d) for d in deltas]
        assert "".join(accumulated) == "ABC"


class TestProviderStreamTextMayBeSse:
    def test_list_input_returns_false_without_crash(self):
        # Regression: previously raised AttributeError on .lstrip().
        assert (
            _provider_stream_text_may_be_sse([{"type": "text", "text": "data: x"}])
            is False
        )

    def test_none_input(self):
        assert _provider_stream_text_may_be_sse(None) is False

    def test_str_behavior_unchanged(self):
        # Plain SSE-looking text is still detected as pending control block.
        assert _provider_stream_text_may_be_sse("data: hello") is True
        # Ordinary prose is not.
        assert _provider_stream_text_may_be_sse("hello") is False