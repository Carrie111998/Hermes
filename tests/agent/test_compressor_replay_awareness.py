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
