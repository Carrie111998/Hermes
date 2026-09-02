"""Tests for the ContextEngine ABC and plugin slot."""

import json
import pytest
from typing import Any, Dict, List

from agent.context_engine import ContextEngine
from agent.context_compressor import ContextCompressor


# ---------------------------------------------------------------------------
# A minimal concrete engine for testing the ABC
# ---------------------------------------------------------------------------

class StubEngine(ContextEngine):
    """Minimal engine that satisfies the ABC without doing real work."""

    def __init__(self, context_length=200000, threshold_pct=0.50):
        self.context_length = context_length
        self.threshold_tokens = int(context_length * threshold_pct)
        self._compress_called = False
        self._tools_called = []

    @property
    def name(self) -> str:
        return "stub"

    def update_model(self, model="", context_length=0, base_url="", api_key="",
                     provider="", api_mode="", **kwargs) -> None:
        """Mirror ContextCompressor.update_model — recompute threshold from the
        new context_length. This is the mutation that corrupted the shared
        singleton in #42449."""
        self.context_length = context_length
        self.threshold_tokens = int(context_length * 0.20)

    def update_from_response(self, usage: Dict[str, Any]) -> None:
        self.last_prompt_tokens = usage.get("prompt_tokens", 0)
        self.last_completion_tokens = usage.get("completion_tokens", 0)
        self.last_total_tokens = usage.get("total_tokens", 0)

    def should_compress(self, prompt_tokens: int = None) -> bool:
        tokens = prompt_tokens if prompt_tokens is not None else self.last_prompt_tokens
        return tokens >= self.threshold_tokens

    def compress(self, messages: List[Dict[str, Any]], current_tokens: int = None) -> List[Dict[str, Any]]:
        self._compress_called = True
        self.compression_count += 1
        # Trivial: just return as-is
        return messages

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "stub_search",
                "description": "Search the stub engine",
                "parameters": {"type": "object", "properties": {}},
            }
        ]

    def handle_tool_call(self, name: str, args: Dict[str, Any]) -> str:
        self._tools_called.append(name)
        return json.dumps({"ok": True, "tool": name})


# ---------------------------------------------------------------------------
# ABC contract tests
# ---------------------------------------------------------------------------

class TestContextEngineABC:
    """Verify the ABC enforces the required interface."""


    def test_missing_methods_raises(self):
        """A subclass missing required methods cannot be instantiated."""
        class Incomplete(ContextEngine):
            @property
            def name(self):
                return "incomplete"
        with pytest.raises(TypeError):
            Incomplete()

    def test_stub_engine_satisfies_abc(self):
        engine = StubEngine()
        assert isinstance(engine, ContextEngine)
        assert engine.name == "stub"



# ---------------------------------------------------------------------------
# Default method behavior
# ---------------------------------------------------------------------------

class TestDefaults:
    """Verify ABC default implementations work correctly."""



    def test_default_get_status(self):
        engine = StubEngine()
        engine.last_prompt_tokens = 50000
        status = engine.get_status()
        assert status["last_prompt_tokens"] == 50000
        assert status["context_length"] == 200000
        assert status["threshold_tokens"] == 100000
        assert 0 < status["usage_percent"] <= 100


    def test_on_session_reset(self):
        engine = StubEngine()
        engine.last_prompt_tokens = 999
        engine.compression_count = 3
        engine.on_session_reset()
        assert engine.last_prompt_tokens == 0
        assert engine.compression_count == 0



# ---------------------------------------------------------------------------
# StubEngine behavior
# ---------------------------------------------------------------------------

class TestStubEngine:



    def test_tool_schemas(self):
        engine = StubEngine()
        schemas = engine.get_tool_schemas()
        assert len(schemas) == 1
        assert schemas[0]["name"] == "stub_search"

    def test_handle_tool_call(self):
        engine = StubEngine()
        result = engine.handle_tool_call("stub_search", {})
        assert json.loads(result)["ok"] is True
        assert "stub_search" in engine._tools_called




# ---------------------------------------------------------------------------
# ContextCompressor session reset via ABC
# ---------------------------------------------------------------------------

class TestCompressorSessionReset:
    """Verify ContextCompressor.on_session_reset() clears all state."""

    def test_reset_clears_state(self):
        c = ContextCompressor(model="test", quiet_mode=True, config_context_length=200000)
        c.last_prompt_tokens = 50000
        c.compression_count = 3
        c._previous_summary = "some old summary"
        c._context_probed = True
        c._context_probe_persistable = True

        c.on_session_reset()

        assert c.last_prompt_tokens == 0
        assert c.last_completion_tokens == 0
        assert c.last_total_tokens == 0
        assert c.compression_count == 0
        assert c._context_probed is False
        assert c._context_probe_persistable is False
        assert c._previous_summary is None


# ---------------------------------------------------------------------------
# Plugin slot (PluginManager integration)
# ---------------------------------------------------------------------------

class TestPluginContextEngineSlot:
    """Test register_context_engine on PluginContext."""

    def test_register_engine(self):
        from hermes_cli.plugins import PluginManager, PluginContext, PluginManifest
        mgr = PluginManager()
        manifest = PluginManifest(name="test-lcm")
        ctx = PluginContext(manifest, mgr)

        engine = StubEngine()
        ctx.register_context_engine(engine)

        assert mgr._context_engine is engine
        assert mgr._context_engine.name == "stub"



    def test_get_plugin_context_engine(self):
        from hermes_cli.plugins import PluginManager, get_plugin_context_engine
        import hermes_cli.plugins as plugins_mod

        # Inject a test manager
        old_mgr = plugins_mod._plugin_manager
        try:
            mgr = PluginManager()
            plugins_mod._plugin_manager = mgr

            assert get_plugin_context_engine() is None

            engine = StubEngine()
            mgr._context_engine = engine
            assert get_plugin_context_engine() is engine
        finally:
            plugins_mod._plugin_manager = old_mgr



class TestPluginContextEngineDeepCopy:
    """Verify that the plugin context engine singleton is deep-copied before
    mutation in agent_init — regression test for #42449."""


    def test_deepcopy_preserves_engine_name(self):
        """Deep-copied engine retains its identity (name property)."""
        import copy
        engine = StubEngine(context_length=500000)
        clone = copy.deepcopy(engine)
        assert clone.name == engine.name == "stub"

    def test_deepcopy_preserves_compressor_state(self):
        """Deep-copied engine starts with the same token counters."""
        import copy
        engine = StubEngine(context_length=500000)
        engine.last_prompt_tokens = 1000
        engine.last_total_tokens = 1500
        engine.compression_count = 3

        clone = copy.deepcopy(engine)
        assert clone.last_prompt_tokens == 1000
        assert clone.last_total_tokens == 1500
        assert clone.compression_count == 3
        assert clone is not engine



class TestClonePluginContextEngine:
    """Per-agent clone of the process-wide plugin context engine.

    #99640: prefer ``clone_for_agent()`` when present, else deepcopy.
    #42449: the clone must be distinct so ``update_model()`` cannot leak
    back into the registered singleton.
    """

    def test_clone_for_agent_used_when_engine_is_unpickleable(self, monkeypatch):
        """Unpickleable state plus ``clone_for_agent()`` uses the hook, not deepcopy."""
        import copy as copy_mod
        import sqlite3

        from agent.agent_init import _try_clone_plugin_context_engine

        class _UnpickleableClonableEngine(StubEngine):
            def __init__(self, context_length=1_000_000, threshold_pct=0.20):
                super().__init__(
                    context_length=context_length, threshold_pct=threshold_pct
                )
                self._conn = sqlite3.connect(":memory:")
                self.clone_calls = 0

            def clone_for_agent(self):
                self.clone_calls += 1
                clone = type(self)(
                    context_length=self.context_length,
                    threshold_pct=0.20,
                )
                clone.threshold_tokens = self.threshold_tokens
                return clone

        engine = _UnpickleableClonableEngine()
        clone = None
        try:
            with pytest.raises(TypeError):
                copy_mod.deepcopy(engine)

            engine_deepcopy_calls = []
            real_deepcopy = copy_mod.deepcopy

            def _spy(obj, *args, **kwargs):
                if obj is engine:
                    engine_deepcopy_calls.append(obj)
                return real_deepcopy(obj, *args, **kwargs)

            monkeypatch.setattr(copy_mod, "deepcopy", _spy)

            clone, failed = _try_clone_plugin_context_engine(engine, "stub")

            assert failed is False
            assert clone is not engine
            assert engine.clone_calls == 1
            assert engine_deepcopy_calls == []
            assert clone.context_length == engine.context_length

            clone.update_model(
                model="MiniMax-M2", context_length=204800, provider="minimax"
            )
            assert engine.context_length == 1_000_000
            assert clone.context_length == 204800
        finally:
            engine._conn.close()
            clone_conn = getattr(clone, "_conn", None) if clone is not None else None
            if clone_conn is not None:
                clone_conn.close()

    def test_deepcopy_used_when_clone_for_agent_absent(self, monkeypatch):
        """Engines without the hook are deep-copied; child mutation stays isolated."""
        import copy as copy_mod

        import hermes_cli.plugins as plugins_mod
        from agent.agent_init import _try_clone_plugin_context_engine
        from hermes_cli.plugins import PluginManager, get_plugin_context_engine

        singleton = StubEngine(context_length=1_000_000, threshold_pct=0.20)
        assert not callable(getattr(singleton, "clone_for_agent", None))

        engine_deepcopy_calls = []
        real_deepcopy = copy_mod.deepcopy

        def _spy(obj, *args, **kwargs):
            if obj is singleton:
                engine_deepcopy_calls.append(obj)
            return real_deepcopy(obj, *args, **kwargs)

        monkeypatch.setattr(copy_mod, "deepcopy", _spy)

        old_mgr = plugins_mod._plugin_manager
        try:
            mgr = PluginManager()
            mgr._context_engine = singleton
            plugins_mod._plugin_manager = mgr

            candidate = get_plugin_context_engine()
            assert candidate is singleton

            clone, failed = _try_clone_plugin_context_engine(candidate, "stub")

            assert failed is False
            assert engine_deepcopy_calls == [singleton]
            assert clone is not singleton

            clone.update_model(
                model="MiniMax-M2", context_length=204800, provider="minimax"
            )
            assert singleton.context_length == 1_000_000
            assert singleton.threshold_tokens == 200_000
            assert clone.context_length == 204800
        finally:
            plugins_mod._plugin_manager = old_mgr

    def test_unpickleable_engine_without_hook_falls_back(self, monkeypatch):
        """Copy failure without ``clone_for_agent()`` falls back and warns."""
        import copy as copy_mod
        import sqlite3
        from unittest.mock import MagicMock

        from agent.agent_init import _try_clone_plugin_context_engine

        class _UncopyableEngine(StubEngine):
            def __init__(self):
                super().__init__(context_length=1_000_000, threshold_pct=0.20)
                self._conn = sqlite3.connect(":memory:")

        engine = _UncopyableEngine()
        mock_logger = MagicMock()
        monkeypatch.setattr(
            "agent.agent_init._ra", lambda: MagicMock(logger=mock_logger)
        )
        try:
            with pytest.raises(TypeError):
                copy_mod.deepcopy(engine)

            clone, failed = _try_clone_plugin_context_engine(engine, "stub")

            assert failed is True
            assert clone is None
            assert engine.context_length == 1_000_000
            mock_logger.warning.assert_called_once()
            warning = mock_logger.warning.call_args[0][0]
            assert "could not be safely cloned" in warning
            assert "falling back to built-in compressor" in warning
        finally:
            engine._conn.close()

    def test_clone_for_agent_error_falls_back(self, monkeypatch):
        """A raising ``clone_for_agent()`` falls back; deepcopy is not a second try."""
        import copy as copy_mod
        from unittest.mock import MagicMock

        from agent.agent_init import _try_clone_plugin_context_engine

        class _BrokenCloneEngine(StubEngine):
            def clone_for_agent(self):
                raise RuntimeError("clone exploded")

        engine = _BrokenCloneEngine()
        engine_deepcopy_calls = []
        real_deepcopy = copy_mod.deepcopy

        def _spy(obj, *args, **kwargs):
            if obj is engine:
                engine_deepcopy_calls.append(obj)
            return real_deepcopy(obj, *args, **kwargs)

        monkeypatch.setattr(copy_mod, "deepcopy", _spy)
        mock_logger = MagicMock()
        monkeypatch.setattr(
            "agent.agent_init._ra", lambda: MagicMock(logger=mock_logger)
        )

        clone, failed = _try_clone_plugin_context_engine(engine, "stub")

        assert failed is True
        assert clone is None
        assert engine_deepcopy_calls == []
        mock_logger.warning.assert_called_once()

    def test_agent_init_source_routes_plugin_singleton_through_clone_helper(self):
        """Source-pin for #42449: production init must clone the plugin
        singleton, not alias it. Full init_agent is too heavy to drive
        here, so this pins the call site. The helper tests above do not
        catch `_selected_engine = _candidate`."""
        import inspect
        import re

        import agent.agent_init as _ai

        src = inspect.getsource(_ai)
        assert re.search(
            r"_try_clone_plugin_context_engine\s*\(\s*_candidate",
            src,
        ), (
            "agent_init must clone the plugin context-engine singleton via "
            "`_try_clone_plugin_context_engine(_candidate, …)` — a bare "
            "`_selected_engine = _candidate` re-introduces #42449."
        )
        assert not re.search(
            r"_selected_engine\s*=\s*_candidate\b", src
        ), "found the #42449 bug-shape alias `_selected_engine = _candidate`"
