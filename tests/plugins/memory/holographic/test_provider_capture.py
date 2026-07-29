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
        """auto_capture: true should set config flags."""
        from plugins.memory.holographic import HolographicMemoryProvider
        provider = HolographicMemoryProvider(config={"auto_capture": True, "capture_interval": 3})
        provider.initialize(session_id="test")
        assert provider._auto_capture is True
        assert provider._capture_interval == 3
        # capture engine not initialized yet (no llm)
        assert provider._capture is None

    def test_sync_turn_no_messages_does_not_crash(self):
        """sync_turn without messages kwarg should not crash even with auto_capture."""
        from plugins.memory.holographic import HolographicMemoryProvider
        provider = HolographicMemoryProvider(config={"auto_capture": True})
        provider._store = MagicMock()
        provider._llm = MagicMock()
        provider._auto_capture = True
        provider._capture_interval = 5
        provider._capture = None  # no engine yet
        # Should not raise
        provider.sync_turn("hello", "hi there")
        assert True

    def test_init_capture_creates_engine(self):
        """init_capture should create CaptureEngine when auto_capture is enabled."""
        from plugins.memory.holographic import HolographicMemoryProvider
        from plugins.memory.holographic.capture import CaptureEngine
        provider = HolographicMemoryProvider(config={"auto_capture": True, "capture_interval": 3})
        provider._store = MagicMock()
        provider.initialize(session_id="test")
        assert provider._capture is None
        llm = MagicMock()
        provider.init_capture(llm)
        assert provider._capture is not None
        assert isinstance(provider._capture, CaptureEngine)

    def test_sync_turn_feeds_capture_engine(self):
        """sync_turn should forward messages to CaptureEngine when enabled."""
        from plugins.memory.holographic import HolographicMemoryProvider
        from plugins.memory.holographic.capture import CaptureEngine
        provider = HolographicMemoryProvider(config={"auto_capture": True})
        provider._store = MagicMock()
        provider._llm = MagicMock()
        provider._auto_capture = True
        provider._capture_interval = 5
        provider._capture = CaptureEngine(store=provider._store, llm=provider._llm, interval=5)
        provider.sync_turn("hello", "hi there", messages=[
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ])
        assert provider._capture.turn_count() == 1
