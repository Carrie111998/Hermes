"""Regression: whitespace-only text blocks must be coerced, not sent verbatim.

Reproduces HTTP 400 ``messages: text content blocks must contain non-whitespace
text``. When context compression (or certain tool-call flows) produces an
assistant message with an empty or whitespace-only text block, that block is
stored in session history and replayed on every subsequent turn — permanently
wedging the session behind the same 400.

Fix (mirrors ``bedrock_adapter._safe_text``, ref #9486): coerce empty/whitespace
text to a non-whitespace placeholder at two points on the Anthropic request path
— ``_sanitize_replay_block`` (ordered-blocks replay) and the final content walk
in ``_convert_assistant_message`` (main path). Ref #69512.
"""
import pytest
from agent.anthropic_adapter import (
    _EMPTY_TEXT_PLACEHOLDER,
    _safe_text,
    _sanitize_replay_block,
    _convert_assistant_message,
    _ensure_leading_user_turn,
)


def _text_blocks(msg):
    return [b for b in msg["content"] if isinstance(b, dict) and b.get("type") == "text"]


def _assert_no_blank_text(msg):
    """No text content block in the converted message is empty/whitespace-only."""
    assert isinstance(msg["content"], list)
    for b in _text_blocks(msg):
        assert b["text"].strip(), f"blank text block survived: {b!r}"


class TestSafeText:
    def test_none_becomes_placeholder(self):
        assert _safe_text(None) == _EMPTY_TEXT_PLACEHOLDER

    def test_empty_string_becomes_placeholder(self):
        assert _safe_text("") == _EMPTY_TEXT_PLACEHOLDER

    @pytest.mark.parametrize("blank", ["   ", "\n", "\t", " \n\t "])
    def test_whitespace_only_becomes_placeholder(self, blank):
        assert _safe_text(blank) == _EMPTY_TEXT_PLACEHOLDER

    def test_real_text_is_kept_verbatim(self):
        assert _safe_text("hello") == "hello"
        assert _safe_text("  padded  ") == "  padded  "

    def test_non_string_is_coerced_then_checked(self):
        assert _safe_text(123) == "123"


class TestSanitizeReplayBlockWhitespace:
    def test_whitespace_text_block_dropped(self):
        # Blank text blocks are DROPPED at the block level (not coerced in
        # place) — the caller relocates any cache_control the block carried
        # and appends the non-whitespace placeholder only when nothing
        # cacheable survives, so real thinking/tool_use blocks aren't
        # cluttered with "(empty)" noise. See _convert_assistant_message.
        assert _sanitize_replay_block({"type": "text", "text": "   \n"}) is None

    def test_empty_text_block_dropped(self):
        assert _sanitize_replay_block({"type": "text", "text": ""}) is None

    def test_none_text_block_dropped_without_crash(self):
        # text=None (invalid upstream payload) must not reach .strip().
        assert _sanitize_replay_block({"type": "text", "text": None}) is None

    def test_real_text_block_unchanged(self):
        out = _sanitize_replay_block({"type": "text", "text": "hi"})
        assert out == {"type": "text", "text": "hi"}


class TestConvertAssistantMessageWhitespace:
    def test_ordered_blocks_replay_coerces_blank_text(self):
        # The interleaved-thinking fast path replays anthropic_content_blocks
        # through _sanitize_replay_block; a stored whitespace text block here is
        # exactly the compression-produced poison that wedges the session.
        msg = {
            "role": "assistant",
            "anthropic_content_blocks": [
                {"type": "thinking", "thinking": "reasoning", "signature": "sig-A"},
                {"type": "text", "text": "  "},
            ],
        }
        out = _convert_assistant_message(msg)
        _assert_no_blank_text(out)
        assert _text_blocks(out) == [{"type": "text", "text": _EMPTY_TEXT_PLACEHOLDER}]

    def test_main_path_coerces_whitespace_string_content(self):
        # A whitespace-only string content becomes a whitespace text block that
        # the all-empty guard does not catch; the final walk must coerce it.
        out = _convert_assistant_message({"role": "assistant", "content": "   "})
        _assert_no_blank_text(out)
        assert _text_blocks(out) == [{"type": "text", "text": _EMPTY_TEXT_PLACEHOLDER}]

    def test_fully_empty_content_still_gets_placeholder(self):
        # Pre-existing behavior preserved.
        out = _convert_assistant_message({"role": "assistant", "content": ""})
        assert out["content"] == [{"type": "text", "text": _EMPTY_TEXT_PLACEHOLDER}]

    def test_real_text_content_unchanged(self):
        out = _convert_assistant_message({"role": "assistant", "content": "answer"})
        assert out["content"] == [{"type": "text", "text": "answer"}]

    def test_thinking_block_not_treated_as_text(self):
        # Only text blocks are coerced; thinking blocks are left untouched even
        # if their payload is whitespace (they obey a different schema rule).
        msg = {
            "role": "assistant",
            "anthropic_content_blocks": [
                {"type": "thinking", "thinking": "  ", "signature": "sig-B"},
                {"type": "text", "text": "real"},
            ],
        }
        out = _convert_assistant_message(msg)
        thinking = [b for b in out["content"] if b.get("type") == "thinking"]
        assert thinking == [{"type": "thinking", "thinking": "  ", "signature": "sig-B"}]


class TestEnsureLeadingUserTurn:
    """A compacted window can start with an assistant turn; Anthropic requires
    messages[0].role == user, so a placeholder user turn is prepended. That
    placeholder's text MUST be non-whitespace or the request 400s on every turn
    and the session wedges permanently. Ref #69512 / whitespace-block deadlock.
    """

    def test_prepends_user_turn_when_first_is_assistant(self):
        result = [{"role": "assistant", "content": [{"type": "text", "text": "summary"}]}]
        _ensure_leading_user_turn(result)
        assert result[0]["role"] == "user"
        assert len(result) == 2

    def test_placeholder_text_is_non_whitespace(self):
        result = [{"role": "assistant", "content": [{"type": "text", "text": "x"}]}]
        _ensure_leading_user_turn(result)
        _assert_no_blank_text(result[0])
        assert _text_blocks(result[0]) == [{"type": "text", "text": _EMPTY_TEXT_PLACEHOLDER}]

    def test_noop_when_first_is_already_user(self):
        result = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
        _ensure_leading_user_turn(result)
        assert len(result) == 1
        assert result[0]["content"] == [{"type": "text", "text": "hi"}]

    def test_empty_list_is_left_alone(self):
        result = []
        _ensure_leading_user_turn(result)
        assert result == []
