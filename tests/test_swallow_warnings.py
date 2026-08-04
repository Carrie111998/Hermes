"""T5 regression tests: exception swallows must WARN, not stay silent.

Hermes Deep Audit 2026-08-04. Six lifecycle/discovery sites used to swallow
exceptions with bare ``except Exception: pass``:

  run_agent.py  shutdown_memory_provider: memory_manager.shutdown_all()
  run_agent.py  commit_memory_session:    memory_manager.on_session_end()
  run_agent.py  commit_memory_session:    context_compressor.on_session_end()
  run_agent.py  shutdown_memory_provider: context_compressor.on_session_end()
  toolsets.py   _get_plugin_toolset_names: registry lookup failure
  toolsets.py   _get_registry_toolset_aliases: registry lookup failure

Contract pinned here: every failure emits a structured warning (with the
exception attached) AND the caller still gets the documented degraded
result — never an exception. Toolset-discovery warnings are additionally
deduplicated so a broken plugin cannot flood the log on every lookup.
"""

import logging
import pytest


# ---------------------------------------------------------------------------
# run_agent.py memory/session-end lifecycle swallows
# ---------------------------------------------------------------------------

class _RaisingMemoryManager:
    def on_session_end(self, messages):
        raise RuntimeError("memory backend down")

    def shutdown_all(self):
        raise OSError("provider teardown failed")


class _RaisingCompressor:
    def on_session_end(self, session_id, messages):
        raise ValueError("engine state corrupt")


def _bare_agent():
    """Agent instance without __init__ — only the attrs the methods touch."""
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent.session_id = "t5-session"
    return agent


class TestCommitMemorySessionWarns:
    def test_memory_on_session_end_failure_warns(self, caplog):
        agent = _bare_agent()
        agent._memory_manager = _RaisingMemoryManager()
        agent.context_compressor = None
        with caplog.at_level(logging.WARNING, logger="run_agent"):
            agent.commit_memory_session([{"role": "user", "content": "hi"}])
        assert any(
            "on_session_end failed" in r.message and "memory" in r.message
            for r in caplog.records
        ), "memory on_session_end failure must warn, not pass silently"

    def test_compressor_on_session_end_failure_warns(self, caplog):
        agent = _bare_agent()
        agent._memory_manager = None
        agent.context_compressor = _RaisingCompressor()
        with caplog.at_level(logging.WARNING, logger="run_agent"):
            agent.commit_memory_session([])
        assert any(
            "Context engine on_session_end failed" in r.message
            for r in caplog.records
        )

    def test_failure_never_propagates(self):
        agent = _bare_agent()
        agent._memory_manager = _RaisingMemoryManager()
        agent.context_compressor = _RaisingCompressor()
        agent.commit_memory_session(None)  # must not raise


class TestShutdownMemoryProviderWarns:
    def test_shutdown_all_failure_warns(self, caplog):
        agent = _bare_agent()
        agent._memory_manager = _RaisingMemoryManager()
        agent.context_compressor = _RaisingCompressor()
        with caplog.at_level(logging.WARNING, logger="run_agent"):
            agent.shutdown_memory_provider()
        messages = [r.message for r in caplog.records]
        assert any("shutdown_all failed" in m for m in messages)
        assert any("Context engine on_session_end failed" in m for m in messages)


# ---------------------------------------------------------------------------
# toolsets.py registry-discovery swallows
# ---------------------------------------------------------------------------

class _ExplodingRegistry:
    def get_registered_toolset_names(self):
        raise ImportError("plugin loader crashed")

    def get_registered_toolset_aliases(self):
        raise KeyError("alias table missing")


class TestToolsetDiscoveryWarnsOnce:
    @pytest.fixture(autouse=True)
    def _reset_dedupe(self):
        import toolsets

        toolsets._toolset_discovery_warned.clear()
        yield
        toolsets._toolset_discovery_warned.clear()

    def test_names_fallback_with_warning(self, monkeypatch, caplog):
        import toolsets
        import tools.registry as registry_mod

        monkeypatch.setattr(registry_mod, "registry", _ExplodingRegistry())
        with caplog.at_level(logging.WARNING, logger="toolsets"):
            result = toolsets._get_plugin_toolset_names()
        assert result == set()
        assert any("Toolset discovery degraded" in r.message for r in caplog.records)

    def test_aliases_fallback_with_warning(self, monkeypatch, caplog):
        import toolsets
        import tools.registry as registry_mod

        monkeypatch.setattr(registry_mod, "registry", _ExplodingRegistry())
        with caplog.at_level(logging.WARNING, logger="toolsets"):
            result = toolsets._get_registry_toolset_aliases()
        assert result == {}
        assert any("Toolset discovery degraded" in r.message for r in caplog.records)

    def test_warning_is_deduplicated(self, monkeypatch, caplog):
        import toolsets
        import tools.registry as registry_mod

        monkeypatch.setattr(registry_mod, "registry", _ExplodingRegistry())
        with caplog.at_level(logging.WARNING, logger="toolsets"):
            for _ in range(5):
                toolsets._get_plugin_toolset_names()
        warns = [r for r in caplog.records if "Toolset discovery degraded" in r.message]
        assert len(warns) == 1, "broken plugin must not flood the log"
