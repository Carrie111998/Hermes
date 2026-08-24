"""Desktop/TUI /refine must dispatch against the LIVE session's agent.

``slash.exec`` routes commands in ``_LIVE_SESSION_DIRECT_COMMANDS`` to the
live TUI/desktop session's agent (see ``_live_slash_command_output``);
everything else falls through to the per-session slash-worker subprocess.
The slash worker builds a bare ``HermesCLI`` that never constructs an
AIAgent for pure slash commands, so ``/refine`` routed there always answers
"Nothing to refine yet — send a message first." even mid-conversation.

``/review`` already lives in the direct set; these tests pin ``/refine``
to the same treatment: snapshot the live history, hand it to the live
agent's ``_spawn_background_review``, report the background dispatch.
"""

from __future__ import annotations

import importlib
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture()
def server(hermes_home, monkeypatch):
    # Mocks are scoped to the initial import only (see
    # tests/tui_gateway/test_protocol.py for the rationale).
    with patch.dict(
        "sys.modules",
        {
            "hermes_cli.env_loader": MagicMock(),
            "hermes_cli.banner": MagicMock(),
        },
    ):
        mod = importlib.import_module("tui_gateway.server")

    monkeypatch.setattr(mod, "_hermes_home", hermes_home)
    monkeypatch.setattr(mod, "_cfg_cache", None)
    monkeypatch.setattr(mod, "_cfg_mtime", None)
    monkeypatch.setattr(mod, "_cfg_path", None)
    yield mod
    mod._sessions.clear()
    mod._pending.clear()
    mod._answers.clear()


@pytest.fixture()
def session(server):
    sid = "sid-refine"
    agent = MagicMock()
    agent.valid_tool_names = {"skill_manage"}
    agent._session_messages = [
        {"role": "user", "content": "we deployed via docker compose"},
        {"role": "assistant", "content": "Deployed. Staging is up."},
    ]
    s = {
        "session_key": "tui-refine-session-1",
        "history": list(agent._session_messages),
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "cols": 120,
        "agent": agent,
    }
    server._sessions[sid] = s
    return sid, s


def _call(server, method, **params):
    handler = server._methods[method]
    return handler(1, params)


class _StubWorker:
    """Stands in for the slash-worker subprocess so the test never spawns one.

    If the bug regresses (refine falls back to the worker route), the stub
    records the use instead of silently launching a child HermesCLI.
    """

    used = False

    def __init__(self, *a, **kw):
        _StubWorker.used = True

    def run(self, cmd):
        return f"(stub worker executed: {cmd})"

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _no_real_worker(monkeypatch, server):
    _StubWorker.used = False
    monkeypatch.setattr(server, "_SlashWorker", _StubWorker)


def test_refine_dispatches_live_background_review(server, session):
    sid, s = session
    r = _call(
        server, "slash.exec", command="/refine focus on the deploy steps", session_id=sid
    )

    assert r["result"]["output"].startswith("⚗"), r
    assert "background" in r["result"]["output"]
    assert "focus on the deploy steps" in r["result"]["output"]
    # The review fork must receive the LIVE conversation snapshot.
    agent = s["agent"]
    assert agent._spawn_background_review.called
    kwargs = agent._spawn_background_review.call_args.kwargs
    snapshot = kwargs.get("messages_snapshot")
    assert any(m.get("content") == "we deployed via docker compose" for m in snapshot)
    assert kwargs.get("review_memory") is True
    assert kwargs.get("review_skills") is True
    assert kwargs.get("focus") == "focus on the deploy steps"
    # And it must NOT have been delegated to the slash-worker subprocess.
    assert _StubWorker.used is False


def test_refine_without_agent_reports_honestly(server, session):
    sid, s = session
    del s["agent"]
    s["history"] = []
    r = _call(server, "slash.exec", command="/refine", session_id=sid)
    assert "Nothing to refine yet" in r["result"]["output"]
    assert _StubWorker.used is False


def test_refine_rejects_while_turn_is_running(server, session):
    sid, s = session
    s["running"] = True
    r = _call(server, "slash.exec", command="/refine", session_id=sid)
    assert "wait for the current turn" in r["result"]["output"]
    assert not s["agent"]._spawn_background_review.called
