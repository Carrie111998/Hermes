"""Tests for holographic auto-capture engine."""

import pytest
from plugins.memory.holographic.capture import CaptureEngine


class _FakeLLM:
    """Simulates ctx.llm.complete()."""
    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or iter(["User prefers Python for backend work\nUses uv for package management"])
        self._responses = self.responses if hasattr(self.responses, '__next__') else iter(self.responses)

    def complete(self, messages, *, purpose=None, **kwargs):
        self.calls.append({"messages": messages, "purpose": purpose})
        from agent.plugin_llm import PluginLlmCompleteResult
        return PluginLlmCompleteResult(
            text=next(self._responses),
            provider="test",
            model="test",
            agent_id="test",
        )


class _FakeStore:
    def __init__(self):
        self.facts = []

    def add_fact(self, content, category="general", tags="", initial_trust=None):
        fid = len(self.facts) + 1
        self.facts.append({"id": fid, "content": content, "category": category, "tags": tags})
        return fid


class TestCaptureEngine:
    def test_buffers_and_compresses_at_interval(self):
        store = _FakeStore()
        llm = _FakeLLM()
        engine = CaptureEngine(store=store, llm=llm, interval=2)

        # Turn 1: just buffer
        engine.observe_turn([{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Hello"}])
        assert len(store.facts) == 0
        assert llm.calls == []

        # Turn 2: should trigger compression
        engine.observe_turn([{"role": "user", "content": "I use uv for Python"}, {"role": "assistant", "content": "Noted"}])
        assert len(store.facts) == 2  # 2 lines from fake response
        assert len(llm.calls) == 1
        assert store.facts[0]["category"] == "auto_capture"
        assert "auto_capture" in store.facts[0]["tags"]

    def test_empty_tool_session_no_crash(self):
        store = _FakeStore()
        llm = _FakeLLM(responses=[""])
        engine = CaptureEngine(store=store, llm=llm, interval=1)
        engine.observe_turn([])
        # Empty response from LLM should produce 0 facts, not crash
        assert len(store.facts) == 0

    def test_manual_flush(self):
        store = _FakeStore()
        llm = _FakeLLM()
        engine = CaptureEngine(store=store, llm=llm, interval=100)  # won't trigger by count
        engine.observe_turn([{"role": "user", "content": "Some message"}])
        assert len(store.facts) == 0
        engine.compress_and_store()
        assert len(store.facts) == 2  # compressed immediately
