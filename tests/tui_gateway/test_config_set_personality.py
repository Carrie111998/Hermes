import threading
import types

import pytest

from tui_gateway import server


@pytest.fixture(autouse=True)
def _neuter_agent_prewarm_timer(request, monkeypatch):
    """Stub the deferred agent pre-warm timer for every test in this module.

    ``session.create`` and non-eager ``session.resume`` fire a 50 ms
    background ``threading.Timer`` (``_schedule_agent_build``) that calls
    whatever ``server._make_agent`` is patched in AT FIRE TIME. Left live,
    a timer armed by one test outlives it and lands in the NEXT test's
    ``_make_agent`` mock, racily corrupting its captured state (the
    ``'tip' == 'cont_tip'`` flakes in the session_resume tests). Tests that
    exercise the deferred build itself opt back in with
    ``@pytest.mark.real_agent_prewarm``.
    """
    if request.node.get_closest_marker("real_agent_prewarm"):
        yield
        return
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *a, **k: None)
    yield


def _session(agent=None, **extra):
    return {
        "agent": agent if agent is not None else types.SimpleNamespace(),
        "session_key": "session-key",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "image_counter": 0,
        "cols": 80,
        "slash_worker": None,
        "show_reasoning": False,
        "tool_progress_mode": "all",
        **extra,
    }


def test_config_set_personality_rejects_unknown_name(monkeypatch):
    monkeypatch.setattr(
        server,
        "_available_personalities",
        lambda cfg=None: {"helpful": "You are helpful."},
    )
    resp = server.handle_request(
        {
            "id": "1",
            "method": "config.set",
            "params": {"key": "personality", "value": "bogus"},
        }
    )

    assert "error" in resp
    assert "Unknown personality" in resp["error"]["message"]


def test_config_set_personality_preserves_history_and_returns_info(monkeypatch):
    agent = types.SimpleNamespace(
        ephemeral_system_prompt=None, _cached_system_prompt="old"
    )
    session = _session(
        agent=agent,
        history=[{"role": "user", "text": "hi"}],
        history_version=4,
    )
    emits = []

    server._sessions["sid"] = session
    monkeypatch.setattr(
        server,
        "_available_personalities",
        lambda cfg=None: {"helpful": "You are helpful."},
    )
    monkeypatch.setattr(
        server, "_session_info", lambda agent, *a: {"model": getattr(agent, "model", "?")}
    )
    monkeypatch.setattr(server, "_emit", lambda *args: emits.append(args))
    monkeypatch.setattr(server, "_write_config_key", lambda path, value: None)

    resp = server.handle_request(
        {
            "id": "1",
            "method": "config.set",
            "params": {"session_id": "sid", "key": "personality", "value": "helpful"},
        }
    )

    assert resp["result"]["history_reset"] is False
    assert resp["result"]["info"] == {"model": "?"}
    # History is preserved with a pivot marker appended
    assert len(session["history"]) == 2
    assert session["history"][0] == {"role": "user", "text": "hi"}
    assert session["history"][1]["role"] == "user"
    assert "personality" in session["history"][1]["content"].lower()
    assert "You are helpful." in session["history"][1]["content"]
    assert session["history_version"] == 5
    # Agent's system prompt was updated in-place; cached prompt untouched
    assert agent.ephemeral_system_prompt == "You are helpful."
    assert agent._cached_system_prompt == "old"
    assert ("session.info", "sid", {"model": "?"}) in emits
