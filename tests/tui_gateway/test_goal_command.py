"""Tests for /goal handling in tui_gateway.

The TUI routes ``/goal`` through ``command.dispatch`` (not ``slash.exec``)
because the CLI's ``_handle_goal_command`` queues the kickoff message onto
``_pending_input``, which the slash-worker subprocess has no reader for.
Instead we handle ``/goal`` directly in the server and return a
``{"type": "send", "notice": ..., "message": ...}`` payload the TUI client
uses to render a system line and fire the kickoff prompt.
"""

from __future__ import annotations

import importlib
import threading
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    # Bust the goal-module DB cache so it re-resolves HERMES_HOME.
    from hermes_cli import goals

    goals._DB_CACHE.clear()
    yield home
    goals._DB_CACHE.clear()


@pytest.fixture()
def server(hermes_home, monkeypatch):
    with patch.dict(
        "sys.modules",
        {
            "hermes_cli.env_loader": MagicMock(),
            "hermes_cli.banner": MagicMock(),
        },
    ):
        mod = importlib.import_module("tui_gateway.server")
        # The module may have been imported by another test before this fixture
        # set HERMES_HOME. Repoint its cached home/config explicitly so this
        # contract suite never reads the operator's live config or goal DB.
        monkeypatch.setattr(mod, "_hermes_home", hermes_home)
        mod._cfg_cache = None
        mod._cfg_mtime = None
        mod._cfg_path = None
        yield mod
        # Reset module-level session state without re-importing. importlib.reload
        # would re-register the module's atexit hooks (ThreadPoolExecutor
        # shutdown, _shutdown_sessions); the duplicates race the stderr
        # buffer at interpreter shutdown and surface as Fatal Python error:
        # _enter_buffered_busy. Clearing the per-session dicts gives the
        # next test a clean slate; _methods is NOT cleared because it's
        # populated at module import time and re-registration only happens
        # via reload (which we don't do).
        mod._sessions.clear()
        mod._pending.clear()
        mod._answers.clear()


@pytest.fixture()
def session(server):
    sid = f"sid-{uuid.uuid4().hex}"
    session_key = f"tui-goal-session-{uuid.uuid4().hex}"
    s = {
        "session_key": session_key,
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "cols": 120,
    }
    server._sessions[sid] = s
    return sid, session_key, s


def _call(server, method, **params):
    handler = server._methods[method]
    return handler(1, params)


def test_prompt_submit_surfaces_session_persistence_failure_before_kickoff(server, session, monkeypatch):
    sid, _, state = session
    started = []
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda _session: False)
    monkeypatch.setattr(server, "_start_agent_build", lambda *_args, **_kwargs: started.append(True))

    response = _call(server, "prompt.submit", session_id=sid, text="must not launch")

    assert response["error"]["code"] == 5006
    assert "persistence failed" in response["error"]["message"]
    assert state["running"] is False
    assert not started


# ── command.dispatch /goal ────────────────────────────────────────────


def test_goal_bare_shows_status_when_none_set(server, session):
    sid, _, _ = session
    r = _call(server, "command.dispatch", name="goal", arg="", session_id=sid)
    assert r["result"]["type"] == "exec"
    assert "No active goal" in r["result"]["output"]


def test_goal_whitespace_only_shows_status(server, session):
    sid, _, _ = session
    r = _call(server, "command.dispatch", name="goal", arg="   ", session_id=sid)
    assert r["result"]["type"] == "exec"
    assert "No active goal" in r["result"]["output"]


def test_goal_status_alias_shows_status(server, session):
    sid, _, _ = session
    r = _call(server, "command.dispatch", name="goal", arg="status", session_id=sid)
    assert r["result"]["type"] == "exec"
    assert "No active goal" in r["result"]["output"]


def test_goal_set_returns_send_with_notice(server, session):
    sid, session_key, _ = session
    r = _call(server, "command.dispatch", name="goal", arg="build a rocket", session_id=sid)
    result = r["result"]
    assert result["type"] == "send"
    assert result["message"] == "build a rocket"
    assert "notice" in result
    assert "Goal set" in result["notice"]
    assert "20-turn budget" in result["notice"]

    # Persisted in SessionDB
    from hermes_cli.goals import GoalManager

    mgr = GoalManager(session_key)
    assert mgr.state is not None
    assert mgr.state.goal == "build a rocket"
    assert mgr.state.status == "active"


def test_goal_status_rpc_projects_canonical_goal_state_without_mutation(server, session):
    """The desktop must hydrate from GoalManager/SessionDB, never status prose."""
    sid, session_key, _ = session
    _call(server, "command.dispatch", name="goal", arg="build a rocket", session_id=sid)

    response = _call(server, "goal.status", session_id=sid)
    projection = response["result"]

    assert projection["exists"] is True
    assert projection["session_id"] == sid
    assert projection["session_key"] == session_key
    assert projection["goal"] == "build a rocket"
    assert projection["status"] == "active"
    assert projection["outcome"] == "GOAL_ACTIVE"
    assert projection["turns_used"] == 0
    assert projection["max_turns"] == 20
    assert projection["checkpoint_revision"] == 0
    assert projection["continuation_pending"] is False
    # Control-plane secrets/internal lifecycle state must not leak into UI.
    assert "continuation_token" not in projection
    assert "continuation_claimed_by" not in projection
    assert "checkpoint" not in projection
    assert "completion_evidence" not in projection

    # Read-only projection: a fresh canonical manager observes identical state.
    from hermes_cli.goals import GoalManager

    state = GoalManager(session_key).state
    assert state is not None
    assert projection["goal_id"] == state.goal_id
    assert projection["turns_used"] == state.turns_used


def test_goal_status_rpc_reports_no_goal_without_creating_one(server, session):
    sid, session_key, _ = session

    response = _call(server, "goal.status", session_id=sid)

    assert response["result"] == {
        "exists": False,
        "session_id": sid,
        "session_key": session_key,
    }


def test_goal_pause_after_set(server, session):
    sid, session_key, _ = session
    _call(server, "command.dispatch", name="goal", arg="write a story", session_id=sid)
    r = _call(server, "command.dispatch", name="goal", arg="pause", session_id=sid)
    assert r["result"]["type"] == "exec"
    assert "paused" in r["result"]["output"].lower()

    from hermes_cli.goals import GoalManager

    assert GoalManager(session_key).state.status == "paused"


def test_goal_resume_reactivates_and_returns_one_continuation_dispatch(server, session):
    sid, session_key, _ = session
    _call(server, "command.dispatch", name="goal", arg="write a story", session_id=sid)
    _call(server, "command.dispatch", name="goal", arg="pause", session_id=sid)
    r = _call(server, "command.dispatch", name="goal", arg="resume", session_id=sid)
    result = r["result"]
    assert result["type"] == "send"
    assert "resumed" in result["notice"].lower()
    assert result["message"].startswith("[Continuing toward your standing goal]")

    from hermes_cli.goals import GoalManager

    state = GoalManager(session_key).state
    assert state.status == "active"
    assert state.continuation_pending is True
    assert state.checkpoint_revision == 1


def test_goal_compression_exhaustion_retries_once_without_spending_turn(server, session):
    sid, session_key, state = session
    _call(server, "command.dispatch", name="goal", arg="finish despite pressure", session_id=sid)

    decision = server._plan_goal_compression_recovery(
        state,
        {"failed": True, "compression_exhausted": True},
        status="error",
        raw="",
    )

    from hermes_cli.goals import GoalManager

    persisted = GoalManager(session_key).state
    assert decision["should_continue"] is True
    assert persisted.turns_used == 0
    assert persisted.continuation_pending is True
    assert persisted.checkpoint["stop_reason"] == "CONTEXT_COMPRESSION_EXHAUSTED"
    assert state[server._GOAL_COMPRESSION_RECOVERY_ATTEMPTS]["attempts"] == 1


def test_goal_compression_retry_bound_survives_gateway_restart(server, session):
    sid, session_key, state = session
    _call(server, "command.dispatch", name="goal", arg="bounded recovery", session_id=sid)
    exhausted = {"failed": True, "compression_exhausted": True}

    first = server._plan_goal_compression_recovery(
        state, exhausted, status="error", raw=""
    )
    assert first["should_continue"] is True

    from hermes_cli.goals import GoalManager

    # The queued synthetic turn owns consumption. Simulate that admission,
    # then drop process-local accounting as a gateway restart would.
    assert GoalManager(session_key).start_continuation() is True
    state.pop(server._GOAL_COMPRESSION_RECOVERY_ATTEMPTS, None)

    second = server._plan_goal_compression_recovery(
        state, exhausted, status="error", raw=""
    )
    persisted = GoalManager(session_key).state
    assert second["should_continue"] is False
    assert second["verdict"] == "compression_exhausted"
    assert persisted.status == "paused"
    assert persisted.turns_used == 0


def test_successful_goal_turn_clears_process_local_compression_recovery(server, session):
    _, _, state = session
    state[server._GOAL_COMPRESSION_RECOVERY_ATTEMPTS] = {
        "goal_id": "old-goal",
        "attempts": 1,
    }

    decision = server._plan_goal_compression_recovery(
        state, {"completed": True}, status="complete", raw="usable response"
    )

    assert decision is None
    assert server._GOAL_COMPRESSION_RECOVERY_ATTEMPTS not in state


def test_goal_clear_removes_active_goal(server, session):
    sid, session_key, _ = session
    _call(server, "command.dispatch", name="goal", arg="write a story", session_id=sid)
    r = _call(server, "command.dispatch", name="goal", arg="clear", session_id=sid)
    assert r["result"]["type"] == "exec"
    assert "cleared" in r["result"]["output"].lower()

    from hermes_cli.goals import GoalManager

    # After clear the row is marked status=cleared (kept for audit);
    # ``has_goal()`` / ``is_active()`` return False so the goal loop
    # stays off and ``status`` reports "No active goal".
    mgr = GoalManager(session_key)
    assert not mgr.has_goal()
    assert not mgr.is_active()
    assert "No active goal" in mgr.status_line()


def test_goal_stop_and_done_are_clear_aliases(server, session):
    sid, _, _ = session
    _call(server, "command.dispatch", name="goal", arg="first goal", session_id=sid)
    r = _call(server, "command.dispatch", name="goal", arg="stop", session_id=sid)
    assert "cleared" in r["result"]["output"].lower()

    _call(server, "command.dispatch", name="goal", arg="second goal", session_id=sid)
    r = _call(server, "command.dispatch", name="goal", arg="done", session_id=sid)
    assert "cleared" in r["result"]["output"].lower()


def test_goal_requires_session(server):
    r = _call(server, "command.dispatch", name="goal", arg="nope", session_id="unknown")
    assert "error" in r
    assert r["error"]["code"] == 4001


# ── slash.exec /goal routing ──────────────────────────────────────────


def test_slash_exec_routes_goal_to_command_dispatch(server, session):
    """slash.exec must route /goal directly to command.dispatch internally
    instead of returning an error.  Previously the 4018 error required the
    TUI client to retry via command.dispatch, but some clients failed the
    fallback, leaving the command empty ("empty command")."""
    sid, _, _ = session
    r = _call(server, "slash.exec", command="goal status", session_id=sid)
    # Should succeed by routing to command.dispatch internally
    assert "result" in r
    assert r["result"]["type"] == "exec"
    assert "No active goal" in r["result"]["output"]


def test_pending_input_commands_includes_goal(server):
    """Guard: _PENDING_INPUT_COMMANDS must list 'goal' — removing it would
    silently re-break the TUI."""
    assert "goal" in server._PENDING_INPUT_COMMANDS


# ── command.dispatch /moa ────────────────────────────────────────────

def _write_moa_config(home, text):
    cfg_path = home / "config.yaml"
    cfg_path.write_text(text)


def test_moa_bare_returns_usage(server, session, hermes_home):
    _write_moa_config(hermes_home, """
moa:
  default_preset: default
  presets:
    default:
      reference_models:
        - provider: openai-codex
          model: gpt-5.5
      aggregator:
        provider: openrouter
        model: anthropic/claude-opus-4.8
""")
    sid, _, s = session
    r = _call(server, "command.dispatch", name="moa", arg="", session_id=sid)
    # Bare /moa is usage-only now; switching to a preset is via the model picker.
    assert "error" in r
    assert "model_override" not in s


def test_moa_arg_is_always_one_shot(server, session, hermes_home):
    # Any arg (even a preset name) is a one-shot prompt through the DEFAULT
    # preset; /moa never does a sticky switch anymore.
    _write_moa_config(hermes_home, """
moa:
  default_preset: default
  presets:
    default: {}
    review:
      reference_models:
        - provider: openrouter
          model: deepseek/deepseek-v4-pro
      aggregator:
        provider: openrouter
        model: anthropic/claude-opus-4.8
""")
    sid, _, s = session
    r = _call(server, "command.dispatch", name="moa", arg="review", session_id=sid)
    result = r["result"]
    assert result["type"] == "send"
    assert result["message"] == "review"
    assert "one-shot" in result["notice"]
    # Lazy session (no live agent) → MoA preset pinned via model_override for
    # the build, and it is the DEFAULT preset, not the "review" arg.
    assert s["model_override"]["provider"] == "moa"
    assert s["model_override"]["model"] == "default"


def test_moa_non_preset_returns_one_shot_send(server, session, hermes_home):
    _write_moa_config(hermes_home, """
moa:
  default_preset: default
  presets:
    default:
      reference_models:
        - provider: openai-codex
          model: gpt-5.5
      aggregator:
        provider: openrouter
        model: anthropic/claude-opus-4.8
""")
    sid, _, _ = session
    r = _call(server, "command.dispatch", name="moa", arg="inspect this project", session_id=sid)
    result = r["result"]
    assert result["type"] == "send"
    assert result["message"] == "inspect this project"
    assert "one-shot" in result["notice"]


def test_pending_input_commands_includes_moa(server):
    assert "moa" in server._PENDING_INPUT_COMMANDS
