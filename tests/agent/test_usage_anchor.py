"""Usage-anchored context accounting (agent/model_metadata.py).

Context-size checks anchor on the provider-reported ``usage.prompt_tokens``
of the last main-loop response and estimate ONLY the messages appended
since. These tests cover:

  * anchor + delta arithmetic (exact base, small estimated delta);
  * the image-heavy divergence the anchor eliminates (flat 1500/image
    heuristic vs provider truth);
  * fallback to full estimation when no anchor exists (first request,
    usage-less providers);
  * invalidation when compaction rewrites the transcript (structural
    id/index check fails closed) and on explicit reset sites;
  * the preflight consumer (_preflight_request_tokens) preferring the
    anchor, plus a sabotage check proving the anchored path (not the
    heuristic) produces the number.
"""

from types import SimpleNamespace

import pytest

from agent.model_metadata import (
    anchored_context_tokens,
    capture_usage_anchor,
    estimate_messages_tokens_rough,
)
from agent.turn_context import _preflight_request_tokens


def _msg(role, content):
    return {"role": role, "content": content}


def _image_msg():
    # ~40KB of fake base64 — the rough estimator charges a flat 1500
    # tokens per image part regardless of true provider accounting.
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "look at this"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64," + "A" * 40000},
            },
        ],
    }


def _history_with_images(n_images=10):
    msgs = [_msg("user", "start")]
    for i in range(n_images):
        msgs.append(_msg("assistant", f"taking screenshot {i}"))
        msgs.append(_image_msg())
    msgs.append(_msg("assistant", "done looking"))
    return msgs


class TestAnchorArithmetic:
    def test_anchor_plus_small_delta(self):
        messages = _history_with_images(10)
        anchor = capture_usage_anchor(50_000, 250, messages)
        assert anchor is not None
        assert anchor["prompt_tokens"] == 50_000
        assert anchor["base_count"] == len(messages)

        # Main loop appends the response's own assistant reply, then a tool
        # result / user follow-up.
        messages.append(_msg("assistant", "the anchored reply itself"))
        messages.append(_msg("user", "short follow-up"))

        anchored = anchored_context_tokens(messages, anchor)
        assert anchored is not None
        # Exact base + completion; the assistant reply at base_count is
        # covered by completion_tokens, so only the follow-up is estimated.
        delta_est = estimate_messages_tokens_rough([messages[-1]])
        assert anchored == 50_000 + 250 + delta_est
        assert delta_est < 50  # the estimated window is one small message

    def test_image_heavy_divergence_eliminated(self):
        messages = _history_with_images(10)
        # Provider ground truth: say the real prompt was 12,000 tokens
        # (providers often charge far less than 1500/image, or the images
        # were downscaled). The heuristic charges 10 * 1500 + text.
        anchor = capture_usage_anchor(12_000, 100, messages)
        messages.append(_msg("assistant", "reply"))
        messages.append(_msg("user", "ok"))

        rough = estimate_messages_tokens_rough(messages)
        anchored = anchored_context_tokens(messages, anchor)
        assert rough >= 15_000  # flat 1500 x 10 images dominates
        assert anchored is not None
        assert anchored < 12_200
        # The whole-history heuristic diverges by thousands of tokens;
        # the anchored figure is provider truth + a tiny delta.
        assert rough - anchored > 2_800

    def test_no_usage_returns_none(self):
        messages = [_msg("user", "hi")]
        assert capture_usage_anchor(0, 0, messages) is None
        assert capture_usage_anchor(None, None, messages) is None
        assert capture_usage_anchor("garbage", 1, messages) is None

    def test_missing_anchor_falls_back(self):
        messages = _history_with_images(2)
        assert anchored_context_tokens(messages, None) is None


class TestAnchorInvalidation:
    def test_compaction_rewrite_fails_closed(self):
        messages = _history_with_images(4)
        anchor = capture_usage_anchor(30_000, 50, messages)
        # Compaction: transcript rebuilt as a new, shorter list.
        compacted = [
            _msg("user", "summary handoff"),
            _msg("assistant", "[compressed summary]"),
        ]
        assert anchored_context_tokens(compacted, anchor) is None

    def test_middle_splice_shifts_base_and_fails_closed(self):
        messages = _history_with_images(4)
        anchor = capture_usage_anchor(30_000, 50, messages)
        # Micro-compact style splice: middle window replaced by one marker.
        spliced = messages[:1] + [_msg("assistant", "[marker]")] + messages[5:]
        assert anchored_context_tokens(spliced, anchor) is None

    def test_same_length_different_objects_fails_closed(self):
        messages = _history_with_images(4)
        anchor = capture_usage_anchor(30_000, 50, messages)
        rebuilt = [dict(m) for m in messages]  # fresh dicts, same values
        assert anchored_context_tokens(rebuilt, anchor) is None

    def test_codex_native_compaction_clears_the_anchor(self):
        """A Codex app-server compaction rewrites the provider-side
        context, so the anchor's transcript snapshot no longer matches."""
        from agent.codex_runtime import _record_codex_app_server_compaction

        agent = SimpleNamespace(
            _usage_anchor=object(),
            session_id="s1",
            _last_compaction_in_place=True,
            _emit_status=lambda _s: None,
            context_compressor=SimpleNamespace(
                compression_count=0,
                last_prompt_tokens=0,
                last_completion_tokens=0,
            ),
            event_callback=None,
        )
        turn = SimpleNamespace(
            compacted=True, thread_id="t1", turn_id="u1", token_usage_last=None,
        )

        assert _record_codex_app_server_compaction(agent, turn) is True
        assert agent._usage_anchor is None

    def test_reset_session_state_clears_the_anchor(self, tmp_path):
        """AIAgent.reset_session_state starts a fresh session: the anchor
        belongs to the old transcript and must not survive the reset."""
        import os
        from unittest.mock import MagicMock, patch

        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        db.create_session("ANCHOR_RESET_SESSION", source="cli")
        try:
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
                from run_agent import AIAgent

                agent = AIAgent(
                    api_key="test-key",
                    base_url="https://openrouter.ai/api/v1",
                    model="test/model",
                    quiet_mode=True,
                    session_db=db,
                    session_id="ANCHOR_RESET_SESSION",
                    skip_context_files=True,
                    skip_memory=True,
                )
            agent.context_compressor = MagicMock()
            agent._usage_anchor = object()

            agent.reset_session_state()

            assert agent._usage_anchor is None
        finally:
            try:
                db.close()
            except Exception:
                pass


class TestPreflightConsumer:
    def _agent(self, anchor):
        return SimpleNamespace(
            _usage_anchor=anchor,
            tools=None,
            api_mode="",
            provider="openai",
        )

    def test_preflight_prefers_anchor(self):
        messages = _history_with_images(10)
        anchor = capture_usage_anchor(50_000, 250, messages)
        messages.append(_msg("assistant", "reply"))
        messages.append(_msg("user", "ok"))
        agent = self._agent(anchor)

        got = _preflight_request_tokens(agent, messages, "SYSTEM PROMPT " * 500)
        expected = anchored_context_tokens(messages, anchor)
        assert got == expected
        # The anchored figure ignores the (already-counted) system prompt
        # text passed in — provider usage includes the real one.
        assert 50_000 < got < 50_500

    def test_preflight_falls_back_without_anchor(self):
        messages = _history_with_images(3)
        agent = self._agent(None)
        got = _preflight_request_tokens(agent, messages, "sys")
        # Pure heuristic: flat image cost dominates.
        assert got >= 4_500

    def test_sabotage_disabling_anchor_changes_result(self):
        """Prove the anchored path produced the number: with the anchor
        removed (the sabotage), the same inputs yield the heuristic figure,
        which diverges by thousands of tokens on an image-heavy history."""
        messages = _history_with_images(10)
        anchor = capture_usage_anchor(12_000, 100, messages)
        messages.append(_msg("assistant", "reply"))
        messages.append(_msg("user", "ok"))

        anchored_result = _preflight_request_tokens(
            self._agent(anchor), messages, ""
        )
        sabotaged_result = _preflight_request_tokens(
            self._agent(None), messages, ""
        )
        assert sabotaged_result - anchored_result > 2_800


class TestCompressionTriggerUsesAnchor:
    def test_threshold_decision_flips_with_anchor(self):
        """An image-heavy history the heuristic pushes over a 15K threshold
        stays under it when the provider reports the real 12K prompt."""
        messages = _history_with_images(10)
        anchor = capture_usage_anchor(12_000, 100, messages)
        messages.append(_msg("assistant", "reply"))

        threshold = 15_000
        heuristic = estimate_messages_tokens_rough(messages)
        anchored = anchored_context_tokens(messages, anchor)
        assert heuristic >= threshold  # old behavior: spurious compression
        assert anchored is not None and anchored < threshold


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
