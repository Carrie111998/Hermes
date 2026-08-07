"""Lifecycle contract for agent-owned context engines.

An AIAgent owns the context engine clone created for that Python agent
instance. Cache eviction may preserve external session tool state, but it must
release that clone so plugin SQLite handles do not survive after the agent is
removed from the cache.
"""

import threading
import time
from unittest.mock import MagicMock, patch


def _partial_agent():
    """Build the smallest AIAgent fixture accepted by both cleanup paths."""
    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    agent.session_id = "context-engine-lifecycle-test"
    agent._active_children = []
    agent._active_children_lock = threading.Lock()
    agent.client = None
    return agent


def test_soft_release_shuts_down_owned_context_engine_exactly_once():
    agent = _partial_agent()
    engine = MagicMock()
    agent.context_compressor = engine

    agent.release_clients()
    agent.release_clients()

    engine.shutdown.assert_called_once_with()


def test_concurrent_soft_release_shuts_down_owned_context_engine_once():
    agent = _partial_agent()
    shutdown_started = threading.Event()
    allow_shutdown = threading.Event()
    calls = []

    class Engine:
        def shutdown(self):
            calls.append("shutdown")
            shutdown_started.set()
            allow_shutdown.wait(timeout=2)

    agent.context_compressor = Engine()
    first = threading.Thread(target=agent.release_clients)
    second = threading.Thread(target=agent.release_clients)

    first.start()
    assert shutdown_started.wait(timeout=2)
    second.start()
    time.sleep(0.05)
    allow_shutdown.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert calls == ["shutdown"]


def test_hard_close_after_soft_release_does_not_double_shutdown_engine():
    agent = _partial_agent()
    engine = MagicMock()
    agent.context_compressor = engine
    agent._session_db = MagicMock()
    agent._end_session_on_close = False

    agent.release_clients()
    with patch("tools.process_registry.process_registry.kill_all"), patch(
        "run_agent.cleanup_vm"
    ), patch("run_agent.cleanup_browser"), patch(
        "tools.computer_use.release_computer_use_session"
    ):
        agent.close()

    engine.shutdown.assert_called_once_with()
    agent._session_db.close.assert_not_called()


def test_context_engine_shutdown_failure_does_not_block_client_release():
    agent = _partial_agent()
    engine = MagicMock()
    engine.shutdown.side_effect = RuntimeError("synthetic shutdown failure")
    agent.context_compressor = engine
    client = MagicMock()
    agent.client = client
    agent._retire_shared_openai_client = MagicMock()
    agent._close_cached_request_openai_client = MagicMock()

    agent.release_clients()

    engine.shutdown.assert_called_once_with()
    agent._retire_shared_openai_client.assert_called_once_with(
        client, reason="cache_evict"
    )
    assert agent.client is None


def test_soft_release_preserves_external_session_tool_state():
    agent = _partial_agent()
    agent.context_compressor = MagicMock()

    with patch("tools.process_registry.process_registry.kill_all") as kill_all, patch(
        "run_agent.cleanup_vm"
    ) as cleanup_vm, patch("run_agent.cleanup_browser") as cleanup_browser, patch(
        "tools.computer_use.release_computer_use_session"
    ) as cleanup_computer_use:
        agent.release_clients()

    kill_all.assert_not_called()
    cleanup_vm.assert_not_called()
    cleanup_browser.assert_not_called()
    cleanup_computer_use.assert_not_called()
