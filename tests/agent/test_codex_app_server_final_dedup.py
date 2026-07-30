"""The codex_app_server turn must not deliver its final answer twice (#74248).

The live app-server event bridge routes every completed ``agentMessage``
through ``_emit_interim_assistant_message``, and that item can BE the turn's
final answer. ``conversation_loop`` communicates "already shown" to the
gateway via the ``response_previewed`` key on the turn result, but the
app-server early-return path never set it — so the gateway defaulted it to
``False`` and sent the same text again. On Discord that surfaces as two
replies about a second apart: one unreferenced (bridge), one reply-referenced
(normal final path).
"""

from __future__ import annotations

import pytest

from agent.codex_runtime import _final_text_was_streamed


class _Agent:
    """Minimal stand-in exposing the real prefix-match semantics."""

    def __init__(self, streamed: str = ""):
        self._current_streamed_assistant_text = streamed

    def _interim_content_was_streamed(self, content: str) -> bool:
        streamed = self._current_streamed_assistant_text or ""
        return bool(streamed) and (content or "").startswith(streamed)


class TestFinalTextWasStreamed:
    def test_final_answer_already_streamed_is_marked_previewed(self):
        """The #74248 shape: the bridge already delivered the final text."""
        assert _final_text_was_streamed(_Agent("The answer is 42."), "The answer is 42.") is True

    def test_streamed_prefix_of_final_counts(self):
        """Final may be the streamed text plus a trailing delta."""
        assert _final_text_was_streamed(_Agent("The answer"), "The answer is 42.") is True

    def test_distinct_commentary_does_not_suppress_the_final(self):
        """Mid-turn commentary must not mark an unrelated final as delivered."""
        assert _final_text_was_streamed(_Agent("Working on it..."), "The answer is 42.") is False

    def test_nothing_streamed_is_not_previewed(self):
        assert _final_text_was_streamed(_Agent(""), "The answer is 42.") is False

    @pytest.mark.parametrize("final_text", ["", None])
    def test_empty_final_is_not_previewed(self, final_text):
        assert _final_text_was_streamed(_Agent("anything"), final_text) is False

    def test_agent_without_the_probe_fails_open(self):
        """Fail toward a benign duplicate, never a suppressed answer."""
        assert _final_text_was_streamed(object(), "The answer is 42.") is False

    def test_probe_errors_fail_open(self):
        class _Boom:
            def _interim_content_was_streamed(self, content):
                raise RuntimeError("boom")

        assert _final_text_was_streamed(_Boom(), "The answer is 42.") is False
