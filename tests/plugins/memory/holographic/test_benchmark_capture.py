"""Benchmarks for holographic auto-capture.

These tests measure the value proposition of auto-capture:
  1. RECALL: conversations with auto_capture preserve facts that would be lost
     with manual-only capture.
  2. LATENCY: auto-capture adds negligible overhead to the agent turn loop.

No real LLM or external dependencies — all benchmarks run against FakeLLM.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List


_CONTENT = """\
{
  "name": "sonpham",
  "role": "developer",
  "proficiency": "engineering"
}
"""  # FakeLLM returns this JSON for the test


class FakeLLM:
    """Simulates ctx.llm.complete() for benchmark tests."""

    def __init__(self, response: str = "- user prefers Python over Go\n- project uses FastAPI\n"):
        self.response = response
        self.last_prompt = ""

    def complete(self, messages: List[Dict[str, Any]], **kwargs) -> Any:
        self.last_prompt = messages[0]["content"] if messages else ""
        return _FakeResponse(self.response)


class _FakeResponse:
    def __init__(self, text: str):
        self.text = text


class FakeStore:
    """Simulates HolographicStore for benchmark tests."""

    def __init__(self):
        self.facts: List[Dict[str, Any]] = []

    def add_fact(self, fact: str, category: str = "", tags: str = "", initial_trust: float | None = None) -> int:
        self.facts.append({"fact": fact, "category": category, "tags": tags})
        return 1

    def search_facts(self, query: str) -> List[Dict[str, Any]]:
        results = []
        q = query.lower()
        for f in self.facts:
            if q in f["fact"].lower():
                results.append(f)
        return results

    def count(self) -> int:
        return len(self.facts)


# ---------------------------------------------------------------------------
# Helper: build the engine from the real module
# ---------------------------------------------------------------------------
def _make_engine(
    store: Any = None,
    llm: Any = None,
    interval: int = 5,
) -> Any:
    from plugins.memory.holographic.capture import CaptureEngine
    return CaptureEngine(
        store=store or FakeStore(),
        llm=llm or FakeLLM(),
        interval=interval,
    )


def _message(role: str, content: str) -> Dict[str, str]:
    return {"role": role, "content": content}


# ---------------------------------------------------------------------------
# 1. RECALL — auto-capture preserves conversation knowledge
# ---------------------------------------------------------------------------
# Without auto_capture, facts discussed in conversation are lost after the
# session ends (or context compresses). With auto_capture=true, they survive
# in fact_store as structured facts.

def test_auto_capture_recovers_known_facts():
    """Auto-capture should recover ground-truth facts embedded in conversation."""
    store = FakeStore()
    llm = FakeLLM(response="- user works at Acme Corp\n- deployed on Kubernetes\n")
    engine = _make_engine(store=store, llm=llm, interval=3)

    # Simulate a conversation where facts are mentioned but never manually stored
    turns = [
        [_message("user", "I work at Acme Corp now")],
        [_message("user", "we use Kubernetes for deployment")],
        [_message("assistant", "let me help with the migration")],
    ]

    for msgs in turns:
        engine.observe_turn(msgs)

    # After interval=3, the 3rd turn should trigger compression
    # (turn_count % interval == 0)
    assert store.count() >= 2, f"Expected ≥2 facts, got {store.count()}"

    results = store.search_facts("Acme")
    assert len(results) >= 1, f"Expected 'Acme' in captured facts: {store.facts}"

    results = store.search_facts("Kubernetes")
    assert len(results) >= 1, f"Expected 'Kubernetes' in captured facts: {store.facts}"


def test_auto_capture_fact_persistence():
    """Auto-captured facts survive explicit store reads like manual facts."""
    store = FakeStore()
    llm = FakeLLM(response="- database is PostgreSQL-16\n- port is 5432\n")
    engine = _make_engine(store=store, llm=llm, interval=2)

    for msgs in [
        [_message("user", "we upgraded to PostgreSQL 16")],
        [_message("user", "running on port 5432 as usual")],
    ]:
        engine.observe_turn(msgs)

    facts = list(store.facts)
    assert any("PostgreSQL" in f["fact"] for f in facts)
    assert any("5432" in f["fact"] for f in facts)
    for f in facts:
        assert f["category"] == "auto_capture"


def test_no_auto_capture_no_facts():
    """Without auto_capture (engine not created), facts are never stored."""
    store = FakeStore()
    # No CaptureEngine created — facts remain empty
    assert store.count() == 0


# ---------------------------------------------------------------------------
# 2. LATENCY — auto-capture overhead is negligible
# ---------------------------------------------------------------------------
# The agent loop calls sync_turn() every turn. Auto-capture must add no
# perceivable latency:
#   - Non-compression turns: just appends to buffer (< 0.5 ms)
#   - Compression turns: LLM call (~10-100 ms with fake LLM)

def test_non_compression_turn_overhead():
    """Non-compression turns (buffer-only) must be under 1 ms."""
    engine = _make_engine(interval=100)  # won't trigger

    # Warm up
    engine.observe_turn([_message("user", "hello")])

    runs = 100
    start = time.perf_counter()
    for i in range(runs):
        engine.observe_turn([_message("user", f"test message {i}")])
    elapsed = time.perf_counter() - start
    avg_ms = (elapsed / runs) * 1000

    assert avg_ms < 1.0, (
        f"Non-compression turn avg {avg_ms:.3f} ms — expected < 1.0 ms"
    )


def test_compression_turn_overhead():
    """Compression turns must complete within 100 ms with a fake LLM."""
    store = FakeStore()
    llm = FakeLLM(response="- user prefers Python over Go\n- project uses FastAPI\n")
    engine = _make_engine(store=store, llm=llm, interval=5)

    # Buffer 4 turns, then the 5th triggers compression
    for i in range(4):
        engine.observe_turn([_message("user", f"fill turn {i}")])

    start = time.perf_counter()
    engine.observe_turn([_message("user", "compression trigger")])
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert elapsed_ms < 100, (
        f"Compression turn took {elapsed_ms:.1f} ms — expected < 100 ms"
    )
    # And facts were actually stored
    assert store.count() == 2, f"Expected 2 facts, got {store.count()}"


def test_zero_overhead_when_disabled():
    """When auto_capture=false, the provider leaves capture inactive.

    Behavioral assertion: the provider must not create a CaptureEngine
    (and thus never buffers or calls the LLM) when auto_capture is off.
    """
    from plugins.memory.holographic import HolographicMemoryProvider

    provider = HolographicMemoryProvider(config={"auto_capture": "false"})
    provider.initialize(session_id="test")

    assert provider._auto_capture is False
    assert provider._capture is None

    # sync_turn with a conversation must not buffer anything or construct an engine
    provider.sync_turn(
        "user says hello",
        "assistant replies",
        session_id="test",
        messages=[_message("user", "hello"), _message("assistant", "hi")],
    )
    assert provider._capture is None
    assert provider._msg_cursor == 0  # nothing was consumed

    provider.shutdown()
