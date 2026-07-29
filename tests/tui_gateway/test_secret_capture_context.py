import sys
import threading
import types

import tui_gateway.server as server
from tools.skills_tool import (
    _get_secret_capture_callback,
    bind_secret_capture_callback,
    reset_secret_capture_callback,
)


def test_wire_callbacks_returns_token_and_restores_outer_secret_callback():
    outer_callback = object()
    outer_token = bind_secret_capture_callback(outer_callback)
    turn_token = None
    try:
        turn_token = server._wire_callbacks("session-a")
        turn_callback = _get_secret_capture_callback()
        assert callable(turn_callback)
        assert turn_callback is not outer_callback

        server._reset_secret_capture_token(turn_token)
        turn_token = None
        assert _get_secret_capture_callback() is outer_callback
    finally:
        if turn_token is not None:
            server._reset_secret_capture_token(turn_token)
        reset_secret_capture_callback(outer_token)


def test_reset_secret_capture_token_accepts_no_binding():
    server._reset_secret_capture_token(None)


def _fake_agent_namespace():
    """Minimal agent namespace for background/preview callback lifecycle tests."""
    return types.SimpleNamespace(
        base_url=None,
        api_key=None,
        provider=None,
        api_mode=None,
        acp_command=None,
        acp_args=None,
        model="test-model",
        enabled_toolsets=None,
        ephemeral_system_prompt=None,
        providers_allowed=None,
        providers_ignored=None,
        providers_order=None,
        provider_sort=None,
        provider_require_parameters=False,
        provider_data_collection=None,
        openrouter_min_coding_score=None,
        reasoning_config=None,
        service_tier=None,
        request_overrides={},
        _fallback_model=None,
    )


def _install_worker_test_doubles(monkeypatch, sid, reset_done):
    wired = []
    reset = []

    class FakeAIAgent:
        def __init__(self, **kwargs):
            pass

        def run_conversation(self, **kwargs):
            return {"final_response": "ok"}

    server._sessions[sid] = {
        "agent": _fake_agent_namespace(),
        "session_key": "test-key",
        "history": [],
        "history_lock": threading.Lock(),
        "running": False,
        "cwd": "/tmp",
    }
    monkeypatch.setattr(
        server,
        "_wire_callbacks",
        lambda target: wired.append(target) or "secret-token",
    )

    def record_reset(token):
        reset.append(token)
        reset_done.set()

    monkeypatch.setattr(server, "_reset_secret_capture_token", record_reset)
    monkeypatch.setattr(server, "_emit", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    monkeypatch.setattr(server, "_load_enabled_toolsets", lambda: ["terminal"])
    monkeypatch.setattr(server, "_load_reasoning_config", lambda: None)
    monkeypatch.setattr(server, "_load_service_tier", lambda: None)
    monkeypatch.setattr(server, "_get_db", lambda: None)
    monkeypatch.setattr(server, "_session_cwd", lambda session: "/tmp")

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = FakeAIAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    return wired, reset


# Regression coverage adapted from the worker-thread gap identified by
# upstream PR #38672. ContextVar callbacks must be bound and restored inside
# each new thread; they do not propagate from the session-build thread.
def test_prompt_background_binds_and_resets_secret_callback_on_worker(monkeypatch):
    sid = "background-secret-sid"
    reset_done = threading.Event()
    wired, reset = _install_worker_test_doubles(monkeypatch, sid, reset_done)
    try:
        response = server.handle_request(
            {
                "id": "1",
                "method": "prompt.background",
                "params": {"session_id": sid, "text": "run something"},
            }
        )
        assert response.get("result"), response.get("error")
        assert reset_done.wait(timeout=3.0)
        assert wired == [sid]
        assert reset == ["secret-token"]
    finally:
        server._sessions.pop(sid, None)


def test_preview_restart_binds_and_resets_secret_callback_on_worker(monkeypatch):
    sid = "preview-secret-sid"
    reset_done = threading.Event()
    wired, reset = _install_worker_test_doubles(monkeypatch, sid, reset_done)

    fake_terminal = types.ModuleType("tools.terminal_tool")
    fake_terminal.register_task_env_overrides = lambda *args, **kwargs: None
    fake_terminal.clear_task_env_overrides = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "tools.terminal_tool", fake_terminal)
    try:
        response = server.handle_request(
            {
                "id": "1",
                "method": "preview.restart",
                "params": {
                    "session_id": sid,
                    "url": "http://localhost:3000",
                    "cwd": "",
                    "context": "",
                },
            }
        )
        assert response.get("result"), response.get("error")
        assert reset_done.wait(timeout=3.0)
        assert wired == [sid]
        assert reset == ["secret-token"]
    finally:
        server._sessions.pop(sid, None)
