"""llm.oneshot model inheritance for freshly opened Desktop/Bot sessions."""

import threading
from types import SimpleNamespace

from agent import oneshot
from tui_gateway import server


def test_oneshot_waits_for_deferred_session_agent(monkeypatch):
    """A fast composer click must not fall back to an unrelated aux model."""
    session_id = "rewrite-bot-session"
    ready = threading.Event()
    session = {
        "agent": None,
        "agent_error": None,
        "agent_ready": ready,
    }
    agent = SimpleNamespace(
        api_key="profile-key",
        api_mode="chat_completions",
        auth_mode="api_key",
        base_url="https://profile.example/v1",
        model="profile-default-model",
        provider="profile-provider",
    )
    captured = {}
    wait_timeouts = []

    def build(sid, candidate):
        assert sid == session_id
        assert candidate is session
        candidate["agent"] = agent
        ready.set()

    def run_oneshot(**kwargs):
        captured.update(kwargs)
        return "rewritten"

    original_wait_agent = server._wait_agent

    def wait_agent(candidate, rid, timeout=30):
        wait_timeouts.append(timeout)
        return original_wait_agent(candidate, rid, timeout=timeout)

    clock = iter((100.0, 110.0, 130.0))
    monkeypatch.setattr(server, "_start_agent_build", build)
    monkeypatch.setattr(server, "_wait_agent", wait_agent)
    monkeypatch.setattr(server.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(oneshot, "run_oneshot", run_oneshot)
    server._sessions[session_id] = session

    try:
        response = server.handle_request(
            {
                "id": "rewrite-1",
                "method": "llm.oneshot",
                "params": {
                    "input": "rough draft",
                    "instructions": "rewrite it",
                    "session_id": session_id,
                    "task": "prompt_rewrite",
                    "timeout": 180,
                },
            }
        )
    finally:
        server._sessions.pop(session_id, None)

    assert response["result"]["text"] == "rewritten"
    assert captured["task"] == "prompt_rewrite"
    assert wait_timeouts == [170]
    assert captured["timeout"] == 150
    assert captured["main_runtime"]["provider"] == "profile-provider"
    assert captured["main_runtime"]["model"] == "profile-default-model"
    assert captured["main_runtime"]["base_url"] == "https://profile.example/v1"
