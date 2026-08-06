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


def test_config_set_reasoning_global_scope_clears_session_override(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    (tmp_path / "config.yaml").write_text("agent:\n  reasoning_effort: medium\n", encoding="utf-8")
    agent = types.SimpleNamespace(reasoning_config=None)
    server._sessions["sid"] = _session(agent=agent)
    server._sessions["sid"]["create_reasoning_override"] = {"enabled": True, "effort": "low"}

    resp = server.handle_request(
        {
            "id": "1",
            "method": "config.set",
            "params": {
                "session_id": "sid",
                "key": "reasoning",
                "value": "high",
                "scope": "global",
            },
        }
    )

    assert resp["result"]["value"] == "high"
    assert server._load_cfg()["agent"]["reasoning_effort"] == "high"
    assert "create_reasoning_override" not in server._sessions["sid"]

    status = server.handle_request(
        {"id": "2", "method": "config.get", "params": {"session_id": "sid", "key": "reasoning"}}
    )
    assert status["result"]["value"] == "high"


def test_config_set_verbose_updates_session_mode_and_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    agent = types.SimpleNamespace(verbose_logging=False)
    server._sessions["sid"] = _session(agent=agent)

    resp = server.handle_request(
        {
            "id": "1",
            "method": "config.set",
            "params": {"session_id": "sid", "key": "verbose", "value": "cycle"},
        }
    )

    assert resp["result"]["value"] == "verbose"
    assert server._sessions["sid"]["tool_progress_mode"] == "verbose"
    assert agent.verbose_logging is True
