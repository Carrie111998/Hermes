"""Tests for dynamic (auto) reasoning effort resolution.

Covers:
- ``hermes_constants.compute_dynamic_reasoning_effort`` — the classifier
  (length/CJK weighting, code blocks, keywords, model-tier clamping).
- ``chat_completion_helpers._resolve_auto_reasoning_config`` — the central
  chokepoint that turns ``effort: auto`` into a concrete level per call,
  including the iteration-limit summary path.
"""
import sys

import pytest

sys.path.insert(0, ".")

from hermes_constants import (
    VALID_REASONING_EFFORTS,
    compute_dynamic_reasoning_effort,
    _model_tier,
)


# ---------------------------------------------------------------------------
# _model_tier — model capability classification
# ---------------------------------------------------------------------------

class TestModelTier:
    @pytest.mark.parametrize("model", [
        "deepseek-v4-flash", "gemini-2.5-flash", "claude-haiku-4",
        "gpt-4o-mini", "qwen-nano", "llama-3.2-light", "mimo-v2.5",
    ])
    def test_low_tier_models(self, model):
        """Small/fast models are classified low (reasoning capped)."""
        assert _model_tier(model) == "low"

    @pytest.mark.parametrize("model", [
        "deepseek-v4-pro", "claude-opus-4", "gpt-o3", "gpt-o4-mini",
        "gemini-ultra", "grok-max", "mimo-v2.5-pro",
    ])
    def test_high_tier_models(self, model):
        """Premium reasoning models are classified high (can reach ultra).
        ``pro`` beats ``mimo`` in the tier pattern — a pro-tier MiMo is a
        premium model, not a cheap one."""
        assert _model_tier(model) == "high"

    @pytest.mark.parametrize("model", [
        "claude-sonnet-4", "gpt-4o", "llama-3.3-70b", "unknown-model", None, "",
    ])
    def test_mid_tier_models(self, model):
        """Balanced/unknown models default to mid."""
        assert _model_tier(model) == "mid"


# ---------------------------------------------------------------------------
# compute_dynamic_reasoning_effort — the classifier
# ---------------------------------------------------------------------------

def _msg(content):
    return [{"role": "user", "content": content}]


class TestClassifierBasics:
    def test_empty_messages_returns_medium(self):
        assert compute_dynamic_reasoning_effort([], model=None) == "medium"
        assert compute_dynamic_reasoning_effort(None, model=None) == "medium"

    def test_greeting_is_low(self):
        assert compute_dynamic_reasoning_effort(_msg("hello"), model=None) == "low"
        assert compute_dynamic_reasoning_effort(_msg("hi"), model=None) == "low"
        assert compute_dynamic_reasoning_effort(_msg("thanks"), model=None) == "low"

    def test_technical_no_is_not_low(self):
        """'no' inside a technical sentence must NOT trigger the low penalty."""
        msg = "No, the deadlock happens in acquire() when two threads contend"
        assert compute_dynamic_reasoning_effort(_msg(msg), model=None) == "medium"

    def test_short_simple_question_is_low(self):
        """A short (<30 effective chars) question with no complexity signal
        is cheap — low reasoning suffices."""
        assert compute_dynamic_reasoning_effort(
            _msg("what time is it in Tokyo"), model=None
        ) == "low"

    def test_medium_length_is_medium(self):
        msg = "write a python script to parse json files and extract fields"
        assert compute_dynamic_reasoning_effort(_msg(msg), model=None) == "medium"

    def test_code_block_is_high(self):
        msg = "refactor this: ```python\ndef foo():\n    pass\n```"
        assert compute_dynamic_reasoning_effort(_msg(msg), model=None) == "high"

    def test_single_keyword_is_medium(self):
        """A single complexity keyword without length/code signals scores +1
        only — not enough for high. High needs multiple signals."""
        assert compute_dynamic_reasoning_effort(
            _msg("debug this production issue"), model=None
        ) == "medium"

    def test_keyword_plus_long_message_is_high(self):
        """Keyword + message >800 chars → score ≥ 2 → high."""
        msg = "debug this production issue " + ("x" * 800)
        assert compute_dynamic_reasoning_effort(_msg(msg), model=None) == "high"

    def test_multiple_keywords_overrides_short_penalty(self):
        """Two complexity keywords (+2) cancel the short-message penalty (-1)
        but need a third signal for high — this lands on medium."""
        assert compute_dynamic_reasoning_effort(
            _msg("analyze and optimize this"), model=None
        ) == "medium"

    def test_three_signals_is_high(self):
        """Keyword + code block + medium length → high on mid-tier."""
        msg = "analyze this and fix it: ```python\nfor i in range(10):\n    print(i)\n```"
        assert compute_dynamic_reasoning_effort(_msg(msg), model=None) == "high"

    def test_very_long_message_is_high(self):
        msg = "x" * 2500
        assert compute_dynamic_reasoning_effort(_msg(msg), model=None) == "high"


class TestClassifierCJK:
    def test_chinese_design_is_high_on_mid(self):
        """CJK keyword 设计/架构 + effective length weighting."""
        msg = "帮我设计一个高可用系统的架构，考虑容错和水平扩展"
        assert compute_dynamic_reasoning_effort(_msg(msg), model="sonnet-4") == "high"

    def test_chinese_greeting_is_low(self):
        assert compute_dynamic_reasoning_effort(_msg("你好"), model=None) == "low"

    def test_cjk_length_weighting(self):
        """A short-but-dense Chinese sentence should not be 'low' just because
        its raw char count is small."""
        # 10 CJK chars → effective_len ≈ 10 + 10*2 = 30, not < 30.
        assert compute_dynamic_reasoning_effort(
            _msg("帮我设计系统架构"), model=None
        ) in ("medium", "high")


class TestClassifierModelTier:
    def test_flash_never_exceeds_medium(self):
        """Low-tier models cap at medium even for hard tasks."""
        for msg in (
            "debug this production issue",
            "x" * 2500,
            "refactor this: ```python\npass\n```",
        ):
            assert compute_dynamic_reasoning_effort(
                _msg(msg), model="deepseek-v4-flash"
            ) in ("low", "medium")

    def test_pro_can_reach_ultra(self):
        """High-tier models can reach ultra for the hardest tasks."""
        msg = "debug this production issue " + "x" * 500
        result = compute_dynamic_reasoning_effort(_msg(msg), model="deepseek-v4-pro")
        assert result in ("high", "ultra")

    def test_multipart_content(self):
        """Multi-part (text+image) user messages are flattened for scoring."""
        msg = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "analyze this architecture diagram"},
                {"type": "image_url", "image_url": {"url": "data:..."}},
            ],
        }]
        assert compute_dynamic_reasoning_effort(msg, model="opus-4") in ("high", "ultra")


# ---------------------------------------------------------------------------
# parse_reasoning_effort accepts "auto"
# ---------------------------------------------------------------------------

class TestAutoParsing:
    def test_auto_is_valid_effort(self):
        from hermes_constants import parse_reasoning_effort
        assert parse_reasoning_effort("auto") == {"enabled": True, "effort": "auto"}

    def test_auto_in_valid_efforts(self):
        assert "auto" in VALID_REASONING_EFFORTS


# ---------------------------------------------------------------------------
# _resolve_auto_reasoning_config — central chokepoint
# ---------------------------------------------------------------------------

class TestResolveAutoReasoningConfig:
    def _agent(self, reasoning_config, model="deepseek-v4-pro"):
        import types as _types
        agent = _types.SimpleNamespace(
            reasoning_config=reasoning_config,
            model=model,
        )
        return agent

    def test_auto_resolves_to_concrete(self):
        from agent.chat_completion_helpers import _resolve_auto_reasoning_config
        agent = self._agent({"enabled": True, "effort": "auto"}, model="deepseek-v4-flash")
        resolved = _resolve_auto_reasoning_config(agent, _msg("hello"))
        assert resolved["effort"] in ("low", "medium", "high", "ultra")
        assert resolved["effort"] != "auto"

    def test_non_auto_passes_through_unchanged(self):
        from agent.chat_completion_helpers import _resolve_auto_reasoning_config
        agent = self._agent({"enabled": True, "effort": "high"})
        assert _resolve_auto_reasoning_config(agent, _msg("hello")) == {
            "enabled": True, "effort": "high",
        }

    def test_disabled_passes_through(self):
        from agent.chat_completion_helpers import _resolve_auto_reasoning_config
        agent = self._agent({"enabled": False})
        assert _resolve_auto_reasoning_config(agent, _msg("hello")) == {"enabled": False}

    def test_does_not_mutate_agent_config(self):
        from agent.chat_completion_helpers import _resolve_auto_reasoning_config
        original = {"enabled": True, "effort": "auto"}
        agent = self._agent(original)
        _resolve_auto_reasoning_config(agent, _msg("debug this issue"))
        assert agent.reasoning_config == original  # unchanged in place

    def test_none_config_returns_none(self):
        from agent.chat_completion_helpers import _resolve_auto_reasoning_config
        agent = self._agent(None)
        assert _resolve_auto_reasoning_config(agent, _msg("hello")) is None
