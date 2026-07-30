"""Tests for holographic provider with auto-capture wired."""

import pytest
from unittest.mock import MagicMock, patch


class TestProviderAutoCapture:
    def test_auto_capture_disabled_by_default(self):
        """Config without auto_capture key should not enable capture."""
        from plugins.memory.holographic import HolographicMemoryProvider
        provider = HolographicMemoryProvider(config={})
        provider.initialize(session_id="test")
        assert provider._auto_capture is False
        assert provider._capture is None

    def test_auto_capture_enabled_reads_config(self):
        """auto_capture: true should set config flags and create CaptureEngine in initialize()."""
        from plugins.memory.holographic import HolographicMemoryProvider
        from plugins.memory.holographic.capture import CaptureEngine
        provider = HolographicMemoryProvider(config={"auto_capture": True, "capture_interval": 3})
        provider.initialize(session_id="test")
        assert provider._auto_capture is True
        assert provider._capture_interval == 3
        # capture engine is initialized directly in initialize() using PluginLlm
        assert provider._capture is not None
        assert isinstance(provider._capture, CaptureEngine)

    def test_sync_turn_no_messages_does_not_crash(self):
        """sync_turn without messages kwarg should not crash even with auto_capture."""
        from plugins.memory.holographic import HolographicMemoryProvider
        provider = HolographicMemoryProvider(config={"auto_capture": True})
        provider._store = MagicMock()
        provider._auto_capture = True
        provider._capture_interval = 5
        provider._capture = None  # no engine
        # Should not raise
        provider.sync_turn("hello", "hi there")
        assert True

    def test_initialize_creates_capture_engine(self):
        """initialize should create CaptureEngine when auto_capture is enabled."""
        from plugins.memory.holographic import HolographicMemoryProvider
        from plugins.memory.holographic.capture import CaptureEngine
        provider = HolographicMemoryProvider(config={"auto_capture": True, "capture_interval": 3})
        provider.initialize(session_id="test")
        assert provider._capture is not None
        assert isinstance(provider._capture, CaptureEngine)

    def test_sync_turn_feeds_capture_engine(self):
        """sync_turn should forward messages to CaptureEngine when enabled."""
        from plugins.memory.holographic import HolographicMemoryProvider
        provider = HolographicMemoryProvider(config={"auto_capture": True})
        provider.initialize(session_id="test")
        provider.sync_turn("hello", "hi there", messages=[
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ])
        assert provider._capture.turn_count() == 1

    def test_sync_turn_delta_only(self):
        """sync_turn should only process NEW messages using _msg_cursor."""
        from plugins.memory.holographic import HolographicMemoryProvider
        provider = HolographicMemoryProvider(config={"auto_capture": True})
        provider.initialize(session_id="test")

        # Turn 1: 2 messages
        provider.sync_turn("m1", "r1", messages=[
            {"role": "user", "content": "m1"},
            {"role": "assistant", "content": "r1"},
        ])
        assert provider._capture.turn_count() == 1

        # Turn 2: full conversation has 4 messages, but only 2 are new
        provider.sync_turn("m2", "r2", messages=[
            {"role": "user", "content": "m1"},
            {"role": "assistant", "content": "r1"},
            {"role": "user", "content": "m2"},
            {"role": "assistant", "content": "r2"},
        ])
        assert provider._capture.turn_count() == 2

        # Turn 3: same messages (no delta) — no new turn counted
        provider.sync_turn("m2", "r2", messages=[
            {"role": "user", "content": "m1"},
            {"role": "assistant", "content": "r1"},
            {"role": "user", "content": "m2"},
            {"role": "assistant", "content": "r2"},
        ])
        assert provider._capture.turn_count() == 2

    def test_on_session_switch_resets_cursor(self):
        """on_session_switch should flush buffer and reset message cursor."""
        from plugins.memory.holographic import HolographicMemoryProvider
        provider = HolographicMemoryProvider(config={"auto_capture": True})
        provider.initialize(session_id="old-session")

        # Feed one turn
        provider.sync_turn("hello", "hi", messages=[
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ])
        assert provider._msg_cursor == 2
        assert provider._capture.turn_count() == 1

        # Session switch
        provider.on_session_switch(new_session_id="new-session", reset=True)

        assert provider._session_id == "new-session"
        assert provider._msg_cursor == 0
        assert provider._capture.turn_count() == 0

        # New turn in new session starts fresh
        provider.sync_turn("new", "reply", messages=[
            {"role": "user", "content": "new"},
            {"role": "assistant", "content": "reply"},
        ])
        assert provider._capture.turn_count() == 1
