"""The ``auto_capture`` gate on Mem0's per-turn fact extraction.

``sync_turn()`` ships every exchange to the server with ``infer=True``, so an
LLM extractor writes its own paraphrases of ordinary chatter into the store.
That is the right default for a personal assistant, but not for a shared store
several agents read from: a single throwaway question can leave several
invented "facts" behind for every other agent to recall.

The sibling providers already gate the same call — ``supermemory`` on
``auto_capture``, ``byterover`` on ``auto_extract``. These tests pin the same
switch for mem0, and pin the part that makes it useful rather than merely off:
explicit ``mem0_add`` keeps writing while the gate is closed.
"""

from __future__ import annotations

import json

import plugins.memory.mem0 as mem0_plugin
from plugins.memory.mem0 import Mem0MemoryProvider


class RecordingBackend:
    """Minimal backend that records the writes it is asked to perform."""

    def __init__(self):
        self.adds = []

    def add(self, messages, *, user_id, agent_id, infer=False, metadata=None):
        self.adds.append({"messages": messages, "infer": infer})
        return {"status": "PENDING", "event_id": "evt-test"}


def _provider(monkeypatch, config):
    monkeypatch.setattr(mem0_plugin, "_load_config", lambda: config)
    provider = Mem0MemoryProvider()
    provider.initialize("test-session")
    provider._user_id = "u123"
    provider._agent_id = "agent-a"
    provider._backend = RecordingBackend()
    return provider


def _sync(provider):
    provider.sync_turn("what is 17 times 23", "391")
    if provider._sync_thread is not None:
        provider._sync_thread.join(timeout=2)
    return provider._backend.adds


class TestAutoCaptureGate:
    def test_absent_key_keeps_capturing(self, monkeypatch):
        """Default must not change for anyone who never sets the key."""
        provider = _provider(monkeypatch, {"host": "http://mem0.test"})
        assert provider._auto_capture is True
        adds = _sync(provider)
        assert len(adds) == 1
        assert adds[0]["infer"] is True

    def test_false_skips_the_turn(self, monkeypatch):
        provider = _provider(monkeypatch, {"host": "http://mem0.test", "auto_capture": False})
        assert provider._auto_capture is False
        assert _sync(provider) == []
        assert provider._sync_thread is None

    def test_string_false_skips_the_turn(self, monkeypatch):
        """mem0.json and the setup wizard can both hand the flag over as text."""
        provider = _provider(monkeypatch, {"host": "http://mem0.test", "auto_capture": "false"})
        assert provider._auto_capture is False
        assert _sync(provider) == []

    def test_string_true_keeps_capturing(self, monkeypatch):
        provider = _provider(monkeypatch, {"host": "http://mem0.test", "auto_capture": "true"})
        assert provider._auto_capture is True
        assert len(_sync(provider)) == 1


class TestExplicitWritesSurviveTheGate:
    def test_mem0_add_still_writes_when_capture_is_off(self, monkeypatch):
        """The gate must silence the extractor, not the agent."""
        provider = _provider(monkeypatch, {"host": "http://mem0.test", "auto_capture": False})

        raw = provider.handle_tool_call("mem0_add", {"content": "the deploy key lives in vault"})

        assert "error" not in json.loads(raw)
        assert len(provider._backend.adds) == 1
        # Explicit writes are stored verbatim — no extraction pass.
        assert provider._backend.adds[0]["infer"] is False


class TestSetupWizard:
    def test_schema_exposes_the_key(self, monkeypatch):
        monkeypatch.setattr(mem0_plugin, "_load_config", lambda: {"host": "http://mem0.test"})
        keys = {entry["key"] for entry in Mem0MemoryProvider().get_config_schema()}
        assert "auto_capture" in keys
