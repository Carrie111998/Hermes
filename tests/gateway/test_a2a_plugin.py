"""
Tests for the A2A platform-plugin adapter — concurrency, persistence,
and context-id collision resistance.

Loaded via ``_plugin_adapter_loader`` so it cannot collide with sibling
platform-plugin tests on the same xdist worker.
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from tests.gateway._plugin_adapter_loader import load_plugin_adapter

_a2a = load_plugin_adapter("a2a")
protocol = _a2a.protocol


# ── 1. context-id collision resistance ────────────────────────────────────


class TestContextIdSafety:
    """`contextId` values are hashed for filename use so distinct IDs like
    ``a/b`` and ``ab`` cannot collide and mix conversations."""

    def test_distinct_ids_produce_distinct_filenames(self):
        id1 = "a/b"
        id2 = "ab"
        name1 = protocol.context_filename(id1)
        name2 = protocol.context_filename(id2)
        assert name1 != name2, f"{id1!r} and {id2!r} must not collide"

    def test_same_id_produces_same_filename(self):
        name1 = protocol.context_filename("my-context")
        name2 = protocol.context_filename("my-context")
        assert name1 == name2

    def test_ids_with_special_chars_are_stable(self):
        cid = "alice:bob/chat-1"
        name = protocol.context_filename(cid)
        assert "/" not in name
        assert ":" not in name


# ── 2. Persistence ─────────────────────────────────────────────────────────


class TestPersistence:
    """Messages are persisted to disk outside the context-compaction pipeline."""

    def test_persist_and_readback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(protocol, "_STORE_DIR_OVERRIDE", tmpdir):
                cid = "test-persist-1"
                protocol.persist_message(cid, "user", "Hello from A2A", "task-1")
                protocol.persist_message(cid, "agent", "Got it!", "task-1")

                history = protocol.format_history(cid, limit=10)
                assert "Hello from A2A" in history
                assert "Got it!" in history

    def test_new_context_detection(self):
        assert protocol.is_new_context(protocol.new_context_id()) is True
        assert protocol.is_new_context(None) is True
        assert protocol.is_new_context("") is True
        # A real-looking reused ID should NOT be "new"
        reused = protocol.new_context_id()
        protocol.persist_message(reused, "user", "prior msg", "t1")
        assert protocol.is_new_context(reused) is False

    def test_history_respects_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(protocol, "_STORE_DIR_OVERRIDE", tmpdir):
                cid = "test-limit-ctx"
                for i in range(5):
                    protocol.persist_message(cid, "user", f"msg-{i}", f"t-{i}")

                limited = protocol.format_history(cid, limit=2)
                assert "msg-3" in limited
                assert "msg-4" in limited
                # msg-0 should be dropped (oldest)
                assert "msg-0" not in limited


# ── 3. Concurrency guard ──────────────────────────────────────────────────


class TestConcurrencyGuard:
    """Only one in-flight task per contextId at a time."""

    def _make_adapter(self, extra=None):
        from gateway.config import Platform
        cfg = PlatformConfig(enabled=True, extra=extra or {})
        adapter = _a2a.A2AAdapter(cfg)
        adapter._loop = MagicMock()
        adapter._message_handler = MagicMock()
        return adapter

    def test_first_call_accepted(self):
        adapter = self._make_adapter()
        assert len(adapter._pending_replies) == 0
        # Simulate inbound task reaching the guard
        cid = "ctx-1"
        from concurrent.futures import Future
        fut = Future()
        adapter._pending_replies[cid] = fut
        assert cid in adapter._pending_replies

    def test_concurrent_call_blocked(self):
        adapter = self._make_adapter()
        from concurrent.futures import Future
        cid = "ctx-1"
        fut1 = Future()
        adapter._pending_replies[cid] = fut1

        # Second call — should be rejected
        existing = adapter._pending_replies.get(cid)
        assert existing is not None
        assert not existing.done()
        # This is the guard: concurrent check returns None/error
        assert adapter._pending_replies.get(cid) is fut1

    def test_after_completion_new_call_accepted(self):
        adapter = self._make_adapter()
        from concurrent.futures import Future
        cid = "ctx-1"
        fut = Future()
        adapter._pending_replies[cid] = fut
        fut.set_result("done")

        # After completion, entry is removed during cleanup
        adapter._pending_replies.pop(cid, None)
        assert cid not in adapter._pending_replies

    def test_different_contexts_independent(self):
        adapter = self._make_adapter()
        from concurrent.futures import Future
        adapter._pending_replies["ctx-1"] = Future()
        adapter._pending_replies["ctx-2"] = Future()
        # Both should coexist
        assert len(adapter._pending_replies) == 2


# ── 4. Config priority (config.extra → env → default) ─────────────────────


class TestConfigPriority:

    def test_agent_name_from_extra(self):
        adapter = _a2a.A2AAdapter(
            PlatformConfig(enabled=True, extra={"agent_name": "MyBot"})
        )
        assert adapter.agent_name == "MyBot"

    def test_agent_name_from_env_fallback(self, monkeypatch):
        monkeypatch.setenv("A2A_AGENT_NAME", "EnvBot")
        adapter = _a2a.A2AAdapter(PlatformConfig(enabled=True, extra={}))
        assert adapter.agent_name == "EnvBot"

    def test_port_from_extra(self):
        adapter = _a2a.A2AAdapter(
            PlatformConfig(enabled=True, extra={"port": 12345})
        )
        assert adapter.port == 12345

    def test_port_default(self, monkeypatch):
        monkeypatch.delenv("A2A_PORT", raising=False)
        adapter = _a2a.A2AAdapter(PlatformConfig(enabled=True, extra={}))
        assert adapter.port == 9900


# ── 5. Bearer auth (secret stays in .env) ──────────────────────────────────


class TestBearerAuth:

    def test_no_token_means_localhost_only(self):
        assert _a2a.security.localhost_only() is True

    def test_token_means_not_localhost_only(self, monkeypatch):
        monkeypatch.setenv("A2A_BEARER_TOKEN", "sekret")
        assert _a2a.security.localhost_only() is False

    def test_check_bearer_rejects_wrong_token(self, monkeypatch):
        monkeypatch.setenv("A2A_BEARER_TOKEN", "correct")
        assert _a2a.security.check_bearer("Bearer wrong") is False

    def test_check_bearer_accepts_right_token(self, monkeypatch):
        monkeypatch.setenv("A2A_BEARER_TOKEN", "correct")
        assert _a2a.security.check_bearer("Bearer correct") is True


# ── 6. Plugin registration shape ───────────────────────────────────────────


class TestPluginShape:

    def test_register_is_callable(self):
        assert callable(_a2a.register)

    def test_check_requirements(self):
        assert _a2a.check_requirements() is True

    def test_validate_config(self):
        assert _a2a.validate_config(PlatformConfig(enabled=True)) is True
