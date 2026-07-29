# Task 2: Wire CaptureEngine into HolographicMemoryProvider

## Files
- Modify: `plugins/memory/holographic/__init__.py`
- Modify: `plugins/memory/holographic/plugin.yaml`
- Create: `tests/plugins/memory/holographic/test_provider_capture.py`

## Context
Task 1 just created `plugins/memory/holographic/capture.py` (CaptureEngine class) and `tests/plugins/memory/holographic/test_capture.py`. The capture module is built and tested. This task wires it into the existing `HolographicMemoryProvider`.

## Global Constraints (verbatim)
- Zero new Python dependencies. ctx.llm is already available to all bundled plugins.
- Auto-capture off by default (auto_capture: false) — backward compatible.
- Auto-captured facts must use category="auto_capture" and tags="auto_capture".
- Must not block the turn loop — sync_turn() runs on a background thread already.
- Plugin manifest (plugin.yaml) must list sync_turn and on_session_end in hooks.
- No core Hermes changes. This is a bundled plugin PR only.

## Changes to `__init__.py`

### 1. Add to `initialize()` (after `self._retriever = ...` line, around line 182):
```python
        # -- Auto-capture config --------------------------------------------------
        self._auto_capture = is_truthy_value(self._config.get("auto_capture", False))
        self._capture_interval = int(self._config.get("capture_interval", 5))
        self._capture = None  # lazily initialized when ctx.llm becomes available
```

### 2. Add to `get_config_schema()` (after the `hrr_dim` entry):
```python
            {"key": "auto_capture", "description": "Auto-capture tool observations via LLM into facts mid-session", "default": "false", "choices": ["true", "false"]},
            {"key": "capture_interval", "description": "Auto-capture: compress every N turns", "default": "5"},
```

### 3. Add new method `init_capture(self, llm)` to HolographicMemoryProvider:
```python
    def init_capture(self, llm: Any) -> None:
        """Initialize the CaptureEngine once ctx.llm is available.

        Called from register() after ctx is handed to the plugin.
        Separate from initialize() because ctx.llm is not available
        until after register() completes.
        """
        if not self._auto_capture:
            return
        if self._capture is not None:
            return  # already initialized
        from .capture import CaptureEngine
        self._capture = CaptureEngine(
            store=self._store,
            llm=llm,
            interval=self._capture_interval,
        )
```

### 4. Replace sync_turn() passthrough with capture-aware version:
```python
    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "", messages: Optional[List[Dict[str, Any]]] = None) -> None:
        # Auto-capture: feed turn messages to CaptureEngine
        if self._auto_capture and self._capture is not None and messages:
            try:
                self._capture.observe_turn(messages)
            except Exception as e:
                logger.debug("Auto-capture observe_turn failed: %s", e)
```

### 5. Add `Optional` to the typing imports if not already present (check line ~23):
The current import is: `from typing import Any, Dict, List`
Add `Optional`: `from typing import Any, Dict, List, Optional`

### 6. Wire in register() function (bottom of file):
Change from:
```python
def register(ctx) -> None:
    config = _load_plugin_config()
    provider = HolographicMemoryProvider(config=config)
    ctx.register_memory_provider(provider)
```
To:
```python
def register(ctx) -> None:
    config = _load_plugin_config()
    provider = HolographicMemoryProvider(config=config)
    provider.init_capture(ctx.llm)  # <-- wire capture engine
    ctx.register_memory_provider(provider)
```

## Changes to `plugin.yaml`
Change from:
```yaml
name: holographic
version: 0.1.0
description: "Holographic memory — local SQLite fact store with FTS5 search, trust scoring, and HRR-based compositional retrieval."
hooks:
  - on_session_end
```
To:
```yaml
name: holographic
version: 0.2.0
description: "Holographic memory — local SQLite fact store with FTS5 search, trust scoring, and HRR-based compositional retrieval."
hooks:
  - on_session_end
  - sync_turn
```

## Test file: `tests/plugins/memory/holographic/test_provider_capture.py`
```python
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
```
