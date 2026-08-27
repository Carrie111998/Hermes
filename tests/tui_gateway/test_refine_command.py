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


def test_review_shares_the_refusal_guards(server, session):
    """The extracted preflight must keep /review's contract byte-compatible."""
    sid, s = session
    r = _call(server, "slash.exec", command="/review check the diff", session_id=sid)
    # Live dispatch reached start_review (MagicMock agent -> dispatch note path
    # raises nothing); what matters here is the guard parity below.
    s["running"] = True
    r = _call(server, "slash.exec", command="/review", session_id=sid)
    assert "wait for the current turn" in r["result"]["output"]
    del s["agent"]
    r = _call(server, "slash.exec", command="/review", session_id=sid)
    assert "Nothing to review yet" in r["result"]["output"]


def test_refine_on_compute_host_session_is_refused(server, session, monkeypatch):
    # turn_isolation defaults off; enable it the way production config would
    # so the compute-host classification in the preflight actually engages.
    monkeypatch.setattr(
        server,
        "_load_dashboard_process_isolation_config",
        lambda cfg=None: {"turn_isolation": True},
    )
    sid, s = session
    s["_compute_host_active"] = True
    r = _call(server, "slash.exec", command="/refine", session_id=sid)
    assert "local agent only" in r["result"]["output"]
    assert not s["agent"]._spawn_background_review.called
    # And the same guard governs /review.
    r = _call(server, "slash.exec", command="/review", session_id=sid)
    assert "local agent only" in r["result"]["output"]


def test_refine_with_empty_conversation_reports_honestly(server, session):
    sid, s = session
    agent = s["agent"]
    agent._session_messages = []
    del s["history"]
    s.pop("history_lock", None)
    r = _call(server, "slash.exec", command="/refine", session_id=sid)
    assert "conversation is empty" in r["result"]["output"]
    assert not agent._spawn_background_review.called


def test_refine_without_skill_manage_degrades_gracefully(server, session):
    sid, s = session
    s["agent"].valid_tool_names = set()
    r = _call(
        server,
        "slash.exec",
        command="/refine remember the deploy order",
        session_id=sid,
    )
    out = r["result"]["output"]
    assert out.startswith("⚗")
    kwargs = s["agent"]._spawn_background_review.call_args.kwargs
    assert kwargs.get("review_skills") is False
    assert kwargs.get("review_memory") is True


def test_refine_reports_when_no_review_was_started(server, session):
    """Admission honesty: _spawn_background_review returning falsy (declined:
    review already active / auto-reviews disabled) must NOT produce a fake
    '⚗ Reviewing…' success message."""
    sid, s = session
    s["agent"]._spawn_background_review.return_value = False
    r = _call(server, "slash.exec", command="/refine", session_id=sid)
    out = r["result"]["output"]
    assert not out.startswith("⚗")
    assert "No review was started" in out
