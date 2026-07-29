"""End-to-end test for holographic auto-capture pipeline."""

import tempfile
import os

from plugins.memory.holographic.capture import CaptureEngine


class _RecordingLLM:
    def __init__(self):
        self.calls = []

    def complete(self, messages, *, purpose=None, **kwargs):
        self.calls.append(purpose)
        from agent.plugin_llm import PluginLlmCompleteResult
        return PluginLlmCompleteResult(
            text="- User prefers dark mode\n- Project uses FastAPI\n- Uses uv for Python",
            provider="test",
            model="test",
            agent_id="test",
        )


class TestEndToEndCapture:
    def test_full_pipeline(self):
        """Simulate 6 turns → 2 compression cycles + final flush."""
        from plugins.memory.holographic.store import MemoryStore

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            store = MemoryStore(db_path=db_path)
            llm = _RecordingLLM()
            engine = CaptureEngine(store=store, llm=llm, interval=3)

            # 6 turns, should compress at turn 3 and turn 6
            for i in range(6):
                engine.observe_turn([
                    {"role": "user", "content": f"Message {i*2}"},
                    {"role": "assistant", "content": f"Response {i*2+1}"},
                ])

            # Should have stored facts from 2 compression cycles
            all_facts = store.list_facts()
            assert len(all_facts) >= 2, f"Expected >=2 facts, got {len(all_facts)}"
            assert all(f.get("category") == "auto_capture" for f in all_facts)

            # Check final flush doesn't double-store
            count_before = len(store.list_facts())
            engine.compress_and_store()
            count_after = len(store.list_facts())
            assert count_after == count_before  # no new facts from empty buffer
        finally:
            os.unlink(db_path)

    def test_session_end_flush(self):
        """Simulate session end with partial buffer — should flush remaining."""
        from plugins.memory.holographic.store import MemoryStore

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            store = MemoryStore(db_path=db_path)
            llm = _RecordingLLM()
            engine = CaptureEngine(store=store, llm=llm, interval=5)

            # Only 2 turns — below interval threshold
            engine.observe_turn([
                {"role": "user", "content": "I like Python"},
                {"role": "assistant", "content": "Great language"},
            ])

            # Nothing stored yet (buffer not full)
            assert len(store.list_facts()) == 0

            # Manual flush (what on_session_end would do)
            engine.compress_and_store()

            # Now facts should be stored
            all_facts = store.list_facts()
            assert len(all_facts) >= 1
            assert all_facts[0]["category"] == "auto_capture"
        finally:
            os.unlink(db_path)
