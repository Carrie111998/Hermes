"""Compressor replay-awareness tests for _replays_all_turn_thinking."""
from agent.context_compressor import ContextCompressor


def _stub(provider, model, base_url):
    cc = object.__new__(ContextCompressor)
    cc.provider, cc.model, cc.base_url = provider, model, base_url
    return cc


class TestCompressorReplayAllAwareness:
    def test_soft_replay_loopback(self):
        cc = _stub("local", "qwen", "http://localhost:8081/v1")
        assert cc._replays_all_turn_thinking() is True

    def test_require_side_echo_family(self):
        cc = _stub("deepseek", "d", "https://api.deepseek.com")
        assert cc._replays_all_turn_thinking() is True

    def test_strict_indifferent(self):
        cc = _stub("openai", "gpt-5", "https://api.openai.com/v1")
        assert cc._replays_all_turn_thinking() is False

    def test_result_cached_per_provider_triplet(self):
        cc = _stub("local", "qwen", "http://localhost:8081/v1")
        assert cc._replays_all_turn_thinking() is True
        assert cc._replay_all_cache is not None  # warm
        # a different stub for a strict provider evaluates independently
        assert _stub("mistral", "m", "https://api.mistral.ai/v1") \
            ._replays_all_turn_thinking() is False


class TestAgentSoftReplayCacheKey:
    """The agent-side cache key must match the classifier signature
    (provider, model, base_url) — same triplet as the require-side pad
    cache and _replays_all_turn_thinking.  A model change alone must
    invalidate a warm entry, so a future model-dependent rule cannot
    serve a stale answer (review point on PR #87123)."""

    class _Agent:
        _soft_replay_cache = None  # warmed dynamically by the helper

        def __init__(self, provider, model, base_url):
            self.provider, self.model, self.base_url = provider, model, base_url

    def test_model_change_invalidates_warm_cache(self):
        from agent.agent_runtime_helpers import replays_reasoning_content_for_agent

        agent = self._Agent("local", "qwen-a", "http://localhost:8081/v1")
        assert replays_reasoning_content_for_agent(agent) is True
        warm = getattr(agent, "_soft_replay_cache")
        assert warm is not None and warm[0] == ("local", "qwen-a", "http://localhost:8081/v1")
        # swap model only — classification is host-based so the answer is
        # still True, but the warm entry must NOT be served: it must be
        # re-evaluated under the new triplet
        agent.model = "qwen-b"
        assert replays_reasoning_content_for_agent(agent) is True
        warm = getattr(agent, "_soft_replay_cache")
        assert warm is not None and warm[0] == ("local", "qwen-b", "http://localhost:8081/v1")
