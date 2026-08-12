from types import SimpleNamespace

from plugins.memory.obsidian_duo.contracts import EvidenceRecord, MemoryEvent, MemoryRecord
from plugins.memory.obsidian_duo.inference import MemoryInference


class FakeLlm:
    def __init__(self, parsed=None, error=None):
        self.parsed = parsed or {"ranked_ids": [], "uncertainties": []}
        self.error = error
        self.calls = []

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(parsed=self.parsed, text="{}")


def test_rerank_inherits_active_route_without_overrides():
    llm = FakeLlm({"ranked_ids": ["mem_1"], "uncertainties": []})
    inference = MemoryInference(llm)

    result = inference.rerank("HUD", [MemoryRecord("mem_1", "HUD", "fact", "global")])

    assert result.status == "complete"
    assert llm.calls[0]["fallback_policy"] == "same_provider_only"
    assert "provider" not in llm.calls[0]
    assert "model" not in llm.calls[0]
    assert llm.calls[0]["purpose"] == "memory-duo.rerank"


def test_route_failure_degrades_without_durable_mutation():
    llm = FakeLlm(error=RuntimeError("rate limit"))
    result = MemoryInference(llm).rerank(
        "HUD", [MemoryRecord("mem_1", "HUD", "fact", "global")]
    )

    assert result.status == "deferred"
    assert result.deferred is True


def test_inference_does_not_send_secret_bearing_input():
    llm = FakeLlm()
    result = MemoryInference(llm).rerank("API_KEY=sk-proj-1234567890abcdefghijklmnop", [])
    assert result.deferred
    assert llm.calls == []


def test_extract_and_consolidate_are_bounded_structured_calls():
    llm = FakeLlm({"candidates": [], "uncertainties": []})
    inference = MemoryInference(llm)

    inference.extract_candidates(MemoryEvent("turn", content="short"))
    inference.consolidate([MemoryEvent("turn", content="short")], [EvidenceRecord("e", "session", "short")])

    assert len(llm.calls) == 2
    assert all(call["fallback_policy"] == "same_provider_only" for call in llm.calls)
