import asyncio
import logging
import textwrap
import threading
import types
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from tui_gateway import server


class _SyncThread:
    """Fake Thread class that runs the target synchronously."""
    def __init__(self, target, name=None, args=(), kwargs=None, daemon=None):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)

    def is_alive(self):
        return False


@pytest.fixture
def mock_sync_thread(monkeypatch):
    monkeypatch.setattr(server.threading, "Thread", _SyncThread)


@pytest.fixture
def mock_get_tui_hook_registry(monkeypatch):
    mock_registry = Mock()
    mock_emit_collect = AsyncMock(return_value=[])
    mock_registry.emit_collect = mock_emit_collect
    monkeypatch.setattr(server, "_get_tui_hook_registry", lambda: mock_registry)
    return mock_registry


def test_tui_pre_route_hook_fires(mock_sync_thread, mock_get_tui_hook_registry, monkeypatch):
    """Test that the hook fires when a message is processed."""
    agent = types.SimpleNamespace(
        run_conversation=Mock(return_value={}),
        clear_interrupt=Mock(),
    )
    session = {
        "history_lock": threading.Lock(),
        "history": [],
        "agent": agent,
        "session_key": "test-session-key",
    }
    
    # Mock helpers inside tui_gateway.server to avoid real database/home side effects
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_start_inflight_turn", lambda *a, **k: None)
    monkeypatch.setattr(server, "_clear_inflight_turn", lambda *a, **k: None)
    monkeypatch.setattr(server, "record_turn_start", lambda *a, **k: None)
    monkeypatch.setattr(server, "_retire_turn_marker", lambda *a, **k: None)
    monkeypatch.setattr(server, "_session_home", lambda *a, **k: "")
    monkeypatch.setattr(server, "_session_cwd", lambda *a, **k: "")
    monkeypatch.setattr(server, "_register_session_cwd", lambda *a, **k: None)
    monkeypatch.setattr(server, "_set_session_context", lambda *a, **k: [])
    monkeypatch.setattr(server, "_tts_stream_begin", lambda *a, **k: None)
    monkeypatch.setattr(server, "_voice_mode_enabled", lambda *a, **k: False)
    monkeypatch.setattr("tools.tts_streaming.take_speech_interrupted", lambda *a, **k: False)
    monkeypatch.setattr(server, "_load_interim_assistant_messages", lambda *a, **k: False)
    monkeypatch.setattr(server, "render_message", lambda *a, **k: "")
    monkeypatch.setattr(server, "_get_usage", lambda *a, **k: {})
    
    text = "hello tui pre-route"
    server._run_prompt_submit("rid-1", "sid-1", session, text)
    
    # Verify hook was called with proper context
    mock_get_tui_hook_registry.emit_collect.assert_called_once()
    args, kwargs = mock_get_tui_hook_registry.emit_collect.call_args
    assert args[0] == "message:pre_route"
    
    ctx = args[1]
    assert ctx["platform"] == "desktop"
    assert ctx["user_id"] == ""
    assert ctx["chat_id"] == "test-session-key"
    assert ctx["thread_id"] is None
    assert ctx["chat_type"] == ""
    assert ctx["session_id"] == "test-session-key"
    assert ctx["session_key"] == "test-session-key"
    assert ctx["message"] == "hello tui pre-route"


def test_tui_pre_route_hook_exception_handling(mock_sync_thread, mock_get_tui_hook_registry, monkeypatch):
    """Test that exceptions in the hook don't break message processing."""
    mock_get_tui_hook_registry.emit_collect.side_effect = Exception("Hook failed!")
    
    agent = types.SimpleNamespace(
        run_conversation=Mock(return_value={"final_response": "handled"}),
        clear_interrupt=Mock(),
    )
    session = {
        "history_lock": threading.Lock(),
        "history": [],
        "agent": agent,
        "session_key": "test-session-key",
    }
    
    # Mock helpers
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_start_inflight_turn", lambda *a, **k: None)
    monkeypatch.setattr(server, "_clear_inflight_turn", lambda *a, **k: None)
    monkeypatch.setattr(server, "record_turn_start", lambda *a, **k: None)
    monkeypatch.setattr(server, "_retire_turn_marker", lambda *a, **k: None)
    monkeypatch.setattr(server, "_session_home", lambda *a, **k: "")
    monkeypatch.setattr(server, "_session_cwd", lambda *a, **k: "")
    monkeypatch.setattr(server, "_register_session_cwd", lambda *a, **k: None)
    monkeypatch.setattr(server, "_set_session_context", lambda *a, **k: [])
    monkeypatch.setattr(server, "_tts_stream_begin", lambda *a, **k: None)
    monkeypatch.setattr(server, "_voice_mode_enabled", lambda *a, **k: False)
    monkeypatch.setattr("tools.tts_streaming.take_speech_interrupted", lambda *a, **k: False)
    monkeypatch.setattr(server, "_load_interim_assistant_messages", lambda *a, **k: False)
    monkeypatch.setattr(server, "render_message", lambda *a, **k: "")
    monkeypatch.setattr(server, "_get_usage", lambda *a, **k: {})

    with patch.object(server.logger, "warning") as mock_warn:
        server._run_prompt_submit("rid-2", "sid-2", session, "hello exception")
        # Exception should be caught and logged
        mock_warn.assert_any_call("Failed to fire message:pre_route hook in TUI path: %s", mock_get_tui_hook_registry.emit_collect.side_effect)

    # Message processing should continue and complete successfully
    agent.run_conversation.assert_called_once()


def test_tui_pre_route_hook_switch_session_logged(mock_sync_thread, mock_get_tui_hook_registry, monkeypatch):
    """Test that switch_session decisions are logged but not acted on (TUI limitation)."""
    # Mock emit_collect returning a switch_session decision
    mock_get_tui_hook_registry.emit_collect.return_value = [
        {"decision": "switch_session", "session_id": "target-session-id"}
    ]
    
    agent = types.SimpleNamespace(
        run_conversation=Mock(return_value={}),
        clear_interrupt=Mock(),
    )
    session = {
        "history_lock": threading.Lock(),
        "history": [],
        "agent": agent,
        "session_key": "test-session-key",
    }
    
    # Mock helpers
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_start_inflight_turn", lambda *a, **k: None)
    monkeypatch.setattr(server, "_clear_inflight_turn", lambda *a, **k: None)
    monkeypatch.setattr(server, "record_turn_start", lambda *a, **k: None)
    monkeypatch.setattr(server, "_retire_turn_marker", lambda *a, **k: None)
    monkeypatch.setattr(server, "_session_home", lambda *a, **k: "")
    monkeypatch.setattr(server, "_session_cwd", lambda *a, **k: "")
    monkeypatch.setattr(server, "_register_session_cwd", lambda *a, **k: None)
    monkeypatch.setattr(server, "_set_session_context", lambda *a, **k: [])
    monkeypatch.setattr(server, "_tts_stream_begin", lambda *a, **k: None)
    monkeypatch.setattr(server, "_voice_mode_enabled", lambda *a, **k: False)
    monkeypatch.setattr("tools.tts_streaming.take_speech_interrupted", lambda *a, **k: False)
    monkeypatch.setattr(server, "_load_interim_assistant_messages", lambda *a, **k: False)
    monkeypatch.setattr(server, "render_message", lambda *a, **k: "")
    monkeypatch.setattr(server, "_get_usage", lambda *a, **k: {})

    with patch.object(server.logger, "warning") as mock_warn:
        server._run_prompt_submit("rid-3", "sid-3", session, "hello switch")
        # Warning about switch_session should be logged
        mock_warn.assert_any_call("Session switching is not yet supported in TUI path (switch_session target: %s)", "target-session-id")

    # Message processing should continue normally with the same agent/session
    agent.run_conversation.assert_called_once()


# ── Integration test: real HOOK.yaml + handler.py discovery ────────────────

def test_real_hook_yaml_discovery_and_invocation(tmp_path):
    """Integration test: _get_tui_hook_registry discovers hooks from a real
    temp HERMES_HOME and emit_collect actually invokes the handler.

    This test exercises the full discovery pipeline (HOOK.yaml parsing,
    handler.py dynamic loading, event registration) without mocking the
    registry — verifying the code path that was broken by the module-level
    HOOKS_DIR singleton (blocker #1).
    """
    # Build a minimal HERMES_HOME with one hook for message:pre_route
    hooks_root = tmp_path / "hooks"
    hook_dir = hooks_root / "test-pre-route"
    hook_dir.mkdir(parents=True)

    # Write HOOK.yaml
    (hook_dir / "HOOK.yaml").write_text(
        textwrap.dedent("""\
            name: test-pre-route
            description: Integration test hook for message:pre_route
            events:
              - message:pre_route
        """),
        encoding="utf-8",
    )

    # Write handler.py — sets a sentinel in a shared list so the test can
    # confirm the handler was actually called.
    invocations: list = []
    handler_source = textwrap.dedent("""\
        _invocations = []

        def handle(event_type, context):
            _invocations.append({"event": event_type, "message": context.get("message")})
            return {"decision": "allow"}
    """)
    (hook_dir / "handler.py").write_text(handler_source, encoding="utf-8")

    # Ask for a registry pointed at our temp HERMES_HOME — bypasses the
    # module-level HOOKS_DIR singleton.
    from gateway.hooks import HookRegistry
    registry = HookRegistry(hooks_dir=hooks_root)
    registry.discover_and_load()

    # Confirm discovery found the hook
    loaded_names = [h["name"] for h in registry.loaded_hooks]
    assert "test-pre-route" in loaded_names, (
        f"Hook not discovered. Loaded: {loaded_names}"
    )

    # emit_collect should invoke the handler and return its non-None result
    results = asyncio.run(
        registry.emit_collect(
            "message:pre_route",
            {"message": "integration-test-payload", "platform": "desktop"},
        )
    )

    assert results == [{"decision": "allow"}], (
        f"emit_collect returned unexpected results: {results}"
    )

    # Also confirm that _get_tui_hook_registry accepts the explicit hermes_home
    # kwarg and returns a registry that can discover from the same temp dir.
    # (Clears the cache entry first so the test is independent of call order.)
    hermes_home_str = str(tmp_path)
    server._tui_hook_registries.pop(str(tmp_path.resolve()), None)
    tui_registry = server._get_tui_hook_registry(hermes_home=hermes_home_str)
    tui_results = asyncio.run(
        tui_registry.emit_collect(
            "message:pre_route",
            {"message": "via-tui-registry", "platform": "desktop"},
        )
    )
    assert tui_results == [{"decision": "allow"}], (
        f"TUI registry emit_collect returned unexpected results: {tui_results}"
    )
