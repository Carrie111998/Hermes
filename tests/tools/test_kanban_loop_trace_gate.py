"""Tests for the orchestrator loop-closure trace completion gate
(card t_31b252aa; LOOP-CLOSURE LAW enforcement, tools/kanban_tools.py).

The gate lives in ``_missing_loop_trace`` + the call inside
``_handle_complete``.  It is scoped to profiles listed in
``_LOOP_TRACE_REQUIRED_PROFILES`` (the orchestrator, "default") and
rejects completions whose summary carries neither the four trace verbs
(attempted / failed / verified / do-differently) nor the explicit
"loop-closure: nothing new" escape hatch.
"""
from __future__ import annotations

import json

import pytest


TRACE = (
    "shipped the gate. "
    "attempted: code gate + tests. "
    "failed: none. "
    "verified: pytest green. "
    "do-differently: n/a."
)


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    """Local copy of tests/tools/test_kanban_tools.py::worker_env —
    isolated HERMES_HOME + a claimed task bound to HERMES_KANBAN_TASK."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="loop-trace-gate-test", assignee="test-worker")
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    return tid


@pytest.fixture
def default_profile_env(worker_env, monkeypatch):
    """worker_env but acting as the default (orchestrator) profile."""
    monkeypatch.setenv("HERMES_PROFILE", "default")
    return worker_env


def _out_dict(out: str) -> dict:
    return json.loads(out)


# ---------------------------------------------------------------------------
# Unit: _missing_loop_trace
# ---------------------------------------------------------------------------

class TestMissingLoopTraceUnit:
    def test_other_profiles_exempt(self, monkeypatch):
        from tools import kanban_tools as kt
        monkeypatch.setenv("HERMES_PROFILE", "frontend")
        assert kt._missing_loop_trace("did stuff") is None

    def test_default_traceless_rejected(self, monkeypatch):
        from tools import kanban_tools as kt
        monkeypatch.setenv("HERMES_PROFILE", "default")
        msg = kt._missing_loop_trace("did stuff, all good")
        assert msg is not None and "attempted" in msg

    def test_default_full_trace_passes(self, monkeypatch):
        from tools import kanban_tools as kt
        monkeypatch.setenv("HERMES_PROFILE", "default")
        assert kt._missing_loop_trace(TRACE) is None

    def test_default_partial_trace_rejected(self, monkeypatch):
        from tools import kanban_tools as kt
        monkeypatch.setenv("HERMES_PROFILE", "default")
        # missing 'do-differently'
        partial = "attempted x. failed y. verified z."
        msg = kt._missing_loop_trace(partial)
        assert msg is not None and "do-differently" in msg

    def test_nothing_new_escape_hatch(self, monkeypatch):
        from tools import kanban_tools as kt
        monkeypatch.setenv("HERMES_PROFILE", "default")
        assert kt._missing_loop_trace("ran probe. loop-closure: nothing new") is None

    def test_case_insensitive_verbs(self, monkeypatch):
        from tools import kanban_tools as kt
        monkeypatch.setenv("HERMES_PROFILE", "default")
        upper = TRACE.upper()
        assert kt._missing_loop_trace(upper) is None

    def test_unprofiled_env_falls_back_to_default(self, monkeypatch):
        from tools import kanban_tools as kt
        monkeypatch.delenv("HERMES_PROFILE", raising=False)
        # fallback resolves to "default" -> gated
        msg = kt._missing_loop_trace("plain summary")
        assert msg is not None


# ---------------------------------------------------------------------------
# Integration: _handle_complete honours the gate
# ---------------------------------------------------------------------------

class TestCompleteGateIntegration:
    def test_default_traceless_completion_rejected(self, default_profile_env):
        from tools import kanban_tools as kt
        out = kt._handle_complete({"summary": "all done, shipped it"})
        d = _out_dict(out)
        assert "error" in d
        assert "LOOP-CLOSURE" in d["error"]
        # task must still be in-flight (not done)
        from hermes_cli import kanban_db as kb
        conn = kb.connect()
        try:
            t = kb.get_task(conn, default_profile_env)
            assert t.status == "running", (
                f"gate must leave task in-flight, got {t.status}"
            )
        finally:
            conn.close()

    def test_default_traced_completion_succeeds(self, default_profile_env):
        from tools import kanban_tools as kt
        out = kt._handle_complete({"summary": TRACE})
        d = _out_dict(out)
        assert d.get("ok") is True, d
        from hermes_cli import kanban_db as kb
        conn = kb.connect()
        try:
            t = kb.get_task(conn, default_profile_env)
            assert t.status == "done"
        finally:
            conn.close()

    def test_default_nothing_new_completion_succeeds(self, default_profile_env):
        from tools import kanban_tools as kt
        out = kt._handle_complete({
            "summary": "probe re-run. loop-closure: nothing new"
        })
        d = _out_dict(out)
        assert d.get("ok") is True, d

    def test_worker_profile_unaffected(self, worker_env):
        """worker_env sets HERMES_PROFILE=test-worker — no gate."""
        from tools import kanban_tools as kt
        out = kt._handle_complete({"summary": "plain worker summary"})
        d = _out_dict(out)
        assert d.get("ok") is True, d
