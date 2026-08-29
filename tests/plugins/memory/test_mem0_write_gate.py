"""Tests for the mem0 automatic-capture write gate.

`sync_turn` is the automatic per-turn capture path. It must run for owner
conversations (`agent_context == "primary"`) and stay silent for machine-driven
runs (`cron`, `flush`, `subagent`), matching the supermemory and honcho
providers. Explicit `mem0_add` tool calls are deliberately not gated.
"""

import pytest

from plugins.memory.mem0 import Mem0MemoryProvider


class RecordingBackend:
    """Minimal backend that records `add` calls made by sync_turn."""

    def __init__(self):
        self.adds = []

    def add(self, messages, *, user_id, agent_id, infer=False, metadata=None):
        self.adds.append(messages)
        return {"status": "PENDING", "event_id": "evt-test"}


def _provider(agent_context=None):
    provider = Mem0MemoryProvider()
    kwargs = {} if agent_context is None else {"agent_context": agent_context}
    provider.initialize("test-session", **kwargs)
    provider._user_id = "u123"
    provider._agent_id = "hermes"
    provider._backend = RecordingBackend()
    return provider


def _sync_and_wait(provider):
    provider.sync_turn("hello", "hi there", session_id="test-session")
    thread = provider._sync_thread
    if thread is not None:
        thread.join(timeout=5.0)
    return provider._backend.adds


class TestMem0WriteGate:

    def test_primary_context_writes(self):
        provider = _provider("primary")
        assert provider._write_enabled is True
        assert len(_sync_and_wait(provider)) == 1

    @pytest.mark.parametrize("context", ["cron", "flush", "subagent"])
    def test_machine_contexts_do_not_write(self, context):
        provider = _provider(context)
        assert provider._write_enabled is False
        assert _sync_and_wait(provider) == []

    def test_missing_agent_context_still_writes(self):
        """Callers that never pass agent_context keep the pre-gate behaviour."""
        provider = _provider(None)
        assert provider._write_enabled is True
        assert len(_sync_and_wait(provider)) == 1

    def test_unknown_context_is_not_gated(self):
        """Only the three machine contexts are suppressed; anything else writes."""
        provider = _provider("some-future-context")
        assert provider._write_enabled is True
        assert len(_sync_and_wait(provider)) == 1

    def test_flag_is_reset_per_initialize_on_a_reused_instance(self):
        """The flag lives on the instance, so a provider reused across sessions
        must re-derive it from each initialize() rather than keep the old value.
        """
        provider = Mem0MemoryProvider()

        provider.initialize("session-cron", agent_context="cron")
        provider._user_id = "u123"
        provider._agent_id = "hermes"
        provider._backend = RecordingBackend()
        assert provider._write_enabled is False
        assert _sync_and_wait(provider) == []

        # Same instance, now serving an owner conversation.
        provider.initialize("session-primary", agent_context="primary")
        provider._user_id = "u123"
        provider._agent_id = "hermes"
        provider._backend = RecordingBackend()
        assert provider._write_enabled is True
        assert len(_sync_and_wait(provider)) == 1

        # ...and back again, so the flag is not sticky in either direction.
        provider.initialize("session-subagent", agent_context="subagent")
        provider._backend = RecordingBackend()
        assert provider._write_enabled is False
        assert _sync_and_wait(provider) == []

    def test_gated_skip_is_logged(self, caplog):
        """A silently dropped capture is hard to diagnose when the operator
        expected memory writes, so the skip carries a debug line.
        """
        import logging
        provider = _provider("cron")
        with caplog.at_level(logging.DEBUG, logger="plugins.memory.mem0"):
            _sync_and_wait(provider)
        assert any(
            "automatic capture skipped" in r.message and "cron" in r.getMessage()
            for r in caplog.records
        )
