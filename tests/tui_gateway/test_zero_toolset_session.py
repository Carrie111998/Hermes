"""Per-session zero-tool barrier for the TUI gateway (JSON-RPC backend).

A client that only wants recommendations (no terminal, no file, no browser)
must be able to open or reopen a Hermes session that owns NO tools at all,
without relying on a prompt instruction that the model can ignore.

Contract under test:

1. ``session.create`` / ``session.resume`` accept an optional
   ``enabled_toolsets`` list. ``[]`` is an explicit value (a session with no
   tools) and is never collapsed into the "absent" case.
2. Only ABSENCE of the field keeps the global resolution
   (``_load_enabled_toolsets``); an explicit ``null`` is rejected.
3. The selection survives into every agent construction of that session: the
   deferred build of ``session.create``, the deferred and the eager build of
   ``session.resume``, the first build inside ``tui_gateway.compute_host``,
   and any later rebuild of the same session (``/new``, background agents).
4. The compute-host turn frame carries the field without defaulting it.
5. Resuming a live session that already holds a different selection fails
   closed: an explicit error, no mutation, no tooled fallback.
"""

from __future__ import annotations

import io
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def server():
    with patch.dict(
        "sys.modules",
        {
            "hermes_constants": MagicMock(
                get_hermes_home=MagicMock(return_value="/tmp/hermes_test")
            ),
            "hermes_cli.env_loader": MagicMock(),
            "hermes_cli.banner": MagicMock(),
            "hermes_state": MagicMock(),
        },
    ):
        import importlib

        mod = importlib.import_module("tui_gateway.server")
        yield mod
        mod._sessions.clear()
        mod._pending.clear()
        mod._answers.clear()


class _DB:
    """Minimal SessionDB stand-in for the resume paths."""

    def __init__(self, target: str) -> None:
        self._target = target

    def get_session(self, _sid):
        return {"id": self._target, "model": "vendor/cool-model",
                "model_config": {"provider": "vendor"}}

    def get_session_by_title(self, _title):
        return None

    def resolve_resume_session_id(self, sid):
        return sid

    def reopen_session(self, _sid):
        return None

    def get_resume_conversations(self, _sid):
        return ([], [])

    def get_ancestor_display_prefix(self, _sid):
        return []

    def get_messages_as_conversation(self, _sid, include_ancestors=False,
                                     repair_alternation=False):
        return []


def _quiet(server, monkeypatch) -> None:
    """Neutralize the side machinery a session build/registration triggers."""
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_claim_active_session_slot", lambda *a, **k: (None, None))
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda *a, **k: None)
    monkeypatch.setattr(server, "_register_session_cwd", lambda *a, **k: None)


def _capture_builds(server, monkeypatch) -> list[dict]:
    """Record every ``_make_agent`` call's kwargs; return a stub agent."""
    calls: list[dict] = []

    def _fake(sid, key, **kw):
        calls.append(kw)
        return SimpleNamespace(
            model="vendor/cool-model",
            provider="vendor",
            enabled_toolsets=kw.get("enabled_toolsets_override"),
            session_id=key,
        )

    monkeypatch.setattr(server, "_make_agent", _fake)
    return calls


def _run_deferred_build(server, monkeypatch, sid: str) -> None:
    """Drive the real deferred build with its side machinery stubbed out."""
    monkeypatch.setattr(server, "_SlashWorker", MagicMock())
    monkeypatch.setattr(server, "_attach_worker", lambda *a, **k: None)
    monkeypatch.setattr(server, "_wire_callbacks", lambda *a, **k: None)
    monkeypatch.setattr(server, "_notify_session_boundary", lambda *a, **k: None)
    monkeypatch.setattr(server, "_start_notification_poller", lambda *a, **k: None)
    monkeypatch.setattr(server, "_session_info", lambda *a, **k: {})
    monkeypatch.setattr(server, "_probe_config_health", lambda *a, **k: None)
    monkeypatch.setattr(server, "_config_model_target", lambda *a, **k: "")
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    session = server._sessions[sid]
    server._start_agent_build(sid, session)
    ready = session.get("agent_ready")
    assert ready is not None and ready.wait(timeout=10), "deferred build never finished"


# ── _make_agent: the override reaches AIAgent verbatim ────────────────


def _agent_kwargs(**make_agent_kwargs) -> dict:
    # Deliberately NOT the `server` fixture: this one builds through the real
    # run_agent/model_tools import chain, which needs the real hermes_constants.
    import tui_gateway.server as server

    fake_cfg = {"agent": {"system_prompt": ""}, "model": {"default": "m"}}
    with (
        patch.object(server, "_load_cfg", return_value=fake_cfg),
        patch.object(server, "_get_db", return_value=MagicMock()),
        patch.object(server, "_resolve_model", return_value="m"),
        patch.object(server, "_load_reasoning_config", return_value=None),
        patch.object(server, "_load_service_tier", return_value=None),
        patch.object(server, "_load_fallback_model", return_value=None),
        patch.object(server, "_load_provider_routing", return_value={}),
        patch.object(server, "_load_enabled_toolsets", return_value=["terminal", "file"]),
        patch.object(
            server,
            "_resolve_runtime_with_fallback",
            return_value=SimpleNamespace(
                runtime={"provider": "openai", "base_url": "https://x", "api_key": "k"},
                used_fallback=False,
                selected_model="m",
            ),
        ),
        patch("run_agent.AIAgent") as mock_agent,
    ):
        server._make_agent("sid-x", "key-x", **make_agent_kwargs)
    return mock_agent.call_args.kwargs


class TestMakeAgentOverride:
    def test_empty_list_reaches_aiagent(self) -> None:
        assert _agent_kwargs(enabled_toolsets_override=[])["enabled_toolsets"] == []

    def test_non_empty_list_reaches_aiagent(self) -> None:
        kwargs = _agent_kwargs(enabled_toolsets_override=["file"])
        assert kwargs["enabled_toolsets"] == ["file"]

    def test_absent_override_keeps_global_resolution(self) -> None:
        kwargs = _agent_kwargs()
        assert kwargs["enabled_toolsets"] == ["terminal", "file"]


# ── session.create ───────────────────────────────────────────────────


def _create(server, params: dict) -> dict:
    return server.handle_request({"id": "r1", "method": "session.create", "params": params})


class TestSessionCreate:
    def test_deferred_build_receives_empty_selection(self, server, monkeypatch) -> None:
        _quiet(server, monkeypatch)
        monkeypatch.setattr(server, "_schedule_agent_build", lambda *a, **k: None)
        calls = _capture_builds(server, monkeypatch)

        resp = _create(server, {"cols": 80, "enabled_toolsets": []})
        assert "error" not in resp
        sid = resp["result"]["session_id"]
        assert resp["result"]["stored_session_id"]

        _run_deferred_build(server, monkeypatch, sid)
        assert calls and calls[-1]["enabled_toolsets_override"] == []
        assert server._sessions[sid]["agent"].enabled_toolsets == []

    def test_deferred_build_receives_non_empty_selection(self, server, monkeypatch) -> None:
        _quiet(server, monkeypatch)
        monkeypatch.setattr(server, "_schedule_agent_build", lambda *a, **k: None)
        calls = _capture_builds(server, monkeypatch)

        resp = _create(server, {"cols": 80, "enabled_toolsets": ["file"]})
        _run_deferred_build(server, monkeypatch, resp["result"]["session_id"])
        assert calls[-1]["enabled_toolsets_override"] == ["file"]

    def test_absent_field_leaves_build_untouched(self, server, monkeypatch) -> None:
        _quiet(server, monkeypatch)
        monkeypatch.setattr(server, "_schedule_agent_build", lambda *a, **k: None)
        calls = _capture_builds(server, monkeypatch)

        resp = _create(server, {"cols": 80})
        _run_deferred_build(server, monkeypatch, resp["result"]["session_id"])
        assert "enabled_toolsets_override" not in calls[-1]

    def test_explicit_null_is_rejected(self, server, monkeypatch) -> None:
        """null is not absence: it must not silently inherit the global scope."""
        _quiet(server, monkeypatch)
        monkeypatch.setattr(server, "_schedule_agent_build", lambda *a, **k: None)
        calls = _capture_builds(server, monkeypatch)

        resp = _create(server, {"cols": 80, "enabled_toolsets": None})
        assert "error" in resp
        assert "result" not in resp
        assert not calls
        assert not server._sessions

    def test_non_list_is_rejected(self, server, monkeypatch) -> None:
        _quiet(server, monkeypatch)
        monkeypatch.setattr(server, "_schedule_agent_build", lambda *a, **k: None)
        calls = _capture_builds(server, monkeypatch)

        resp = _create(server, {"cols": 80, "enabled_toolsets": "file"})
        assert "error" in resp
        assert not calls

    @pytest.mark.parametrize(
        "selection",
        [[1], ["file", None], [{"name": "file"}], [True], [["file"]]],
    )
    def test_non_string_items_are_rejected_without_coercion(
        self, server, monkeypatch, selection
    ) -> None:
        _quiet(server, monkeypatch)
        monkeypatch.setattr(server, "_schedule_agent_build", lambda *a, **k: None)
        calls = _capture_builds(server, monkeypatch)

        resp = _create(server, {"cols": 80, "enabled_toolsets": selection})
        assert "error" in resp
        assert "result" not in resp
        assert not calls
        assert not server._sessions

    def test_string_items_are_trimmed(self, server, monkeypatch) -> None:
        _quiet(server, monkeypatch)
        monkeypatch.setattr(server, "_schedule_agent_build", lambda *a, **k: None)
        calls = _capture_builds(server, monkeypatch)

        resp = _create(server, {"cols": 80, "enabled_toolsets": ["  file  ", ""]})
        assert "error" not in resp
        _run_deferred_build(server, monkeypatch, resp["result"]["session_id"])
        assert calls[-1]["enabled_toolsets_override"] == ["file"]


# ── session.resume ───────────────────────────────────────────────────


def _resume(server, params: dict) -> dict:
    return server.handle_request({"id": "r2", "method": "session.resume", "params": params})


class TestSessionResume:
    TARGET = "20260409_010101_abc123"

    def test_deferred_resume_build_receives_empty_selection(self, server, monkeypatch) -> None:
        _quiet(server, monkeypatch)
        monkeypatch.setattr(server, "_get_db", lambda: _DB(self.TARGET))
        monkeypatch.setattr(server, "_schedule_agent_build", lambda *a, **k: None)
        monkeypatch.setattr(server, "sanitize_replay_history", lambda h: h)
        calls = _capture_builds(server, monkeypatch)

        resp = _resume(server, {"session_id": self.TARGET, "enabled_toolsets": []})
        assert "error" not in resp
        assert resp["result"]["resumed"] == self.TARGET
        _run_deferred_build(server, monkeypatch, resp["result"]["session_id"])
        assert calls[-1]["enabled_toolsets_override"] == []

    def test_deferred_resume_absent_field_unchanged(self, server, monkeypatch) -> None:
        _quiet(server, monkeypatch)
        monkeypatch.setattr(server, "_get_db", lambda: _DB(self.TARGET))
        monkeypatch.setattr(server, "_schedule_agent_build", lambda *a, **k: None)
        monkeypatch.setattr(server, "sanitize_replay_history", lambda h: h)
        calls = _capture_builds(server, monkeypatch)

        resp = _resume(server, {"session_id": self.TARGET})
        _run_deferred_build(server, monkeypatch, resp["result"]["session_id"])
        assert "enabled_toolsets_override" not in calls[-1]

    def test_eager_resume_build_receives_empty_selection(self, server, monkeypatch) -> None:
        _quiet(server, monkeypatch)
        monkeypatch.setattr(server, "_get_db", lambda: _DB(self.TARGET))
        monkeypatch.setattr(server, "sanitize_replay_history", lambda h: h)
        monkeypatch.setattr(server, "_init_session", lambda *a, **k: {})
        calls = _capture_builds(server, monkeypatch)

        resp = _resume(
            server,
            {"session_id": self.TARGET, "eager_build": True, "enabled_toolsets": []},
        )
        assert "error" not in resp
        assert calls and calls[-1]["enabled_toolsets_override"] == []

    def test_explicit_null_is_rejected(self, server, monkeypatch) -> None:
        """null is not absence: it must not silently inherit the global scope."""
        _quiet(server, monkeypatch)
        monkeypatch.setattr(server, "_get_db", lambda: _DB(self.TARGET))
        monkeypatch.setattr(server, "_schedule_agent_build", lambda *a, **k: None)
        monkeypatch.setattr(server, "sanitize_replay_history", lambda h: h)
        calls = _capture_builds(server, monkeypatch)

        resp = _resume(server, {"session_id": self.TARGET, "enabled_toolsets": None})
        assert "error" in resp
        assert "result" not in resp
        assert not calls
        assert not server._sessions

    def test_non_list_is_rejected(self, server, monkeypatch) -> None:
        _quiet(server, monkeypatch)
        monkeypatch.setattr(server, "_get_db", lambda: _DB(self.TARGET))
        monkeypatch.setattr(server, "sanitize_replay_history", lambda h: h)
        calls = _capture_builds(server, monkeypatch)

        resp = _resume(
            server,
            {"session_id": self.TARGET, "eager_build": True, "enabled_toolsets": "file"},
        )
        assert "error" in resp
        assert not calls

    @pytest.mark.parametrize("selection", [[1], ["file", None], [{"name": "file"}]])
    def test_non_string_items_are_rejected_without_coercion(
        self, server, monkeypatch, selection
    ) -> None:
        _quiet(server, monkeypatch)
        monkeypatch.setattr(server, "_get_db", lambda: _DB(self.TARGET))
        monkeypatch.setattr(server, "_schedule_agent_build", lambda *a, **k: None)
        monkeypatch.setattr(server, "sanitize_replay_history", lambda h: h)
        calls = _capture_builds(server, monkeypatch)

        resp = _resume(
            server, {"session_id": self.TARGET, "enabled_toolsets": selection}
        )
        assert "error" in resp
        assert "result" not in resp
        assert not calls
        assert not server._sessions

    def test_lazy_resume_build_receives_empty_selection(self, server, monkeypatch) -> None:
        _quiet(server, monkeypatch)
        monkeypatch.setattr(server, "_get_db", lambda: _DB(self.TARGET))
        monkeypatch.setattr(server, "_child_run_active", lambda *a, **k: False)
        calls = _capture_builds(server, monkeypatch)

        resp = _resume(
            server, {"session_id": self.TARGET, "lazy": True, "enabled_toolsets": []}
        )
        assert "error" not in resp
        sid = resp["result"]["session_id"]
        assert server._sessions[sid]["agent"] is None
        _run_deferred_build(server, monkeypatch, sid)
        assert calls[-1]["enabled_toolsets_override"] == []


class TestLiveSessionConflict:
    TARGET = "20260409_020202_def456"

    def _live(self, server, monkeypatch, *, agent_toolsets):
        _quiet(server, monkeypatch)
        monkeypatch.setattr(server, "_get_db", lambda: _DB(self.TARGET))
        agent = SimpleNamespace(
            enabled_toolsets=agent_toolsets,
            model="vendor/cool-model",
            provider="vendor",
            session_id=self.TARGET,
        )
        session = {
            "agent": agent,
            "session_key": self.TARGET,
            "history": [],
            "history_lock": threading.Lock(),
            "history_version": 0,
            "cwd": "/tmp",
            "cols": 80,
            "running": False,
            "created_at": 0.0,
            "last_active": 0.0,
            "inflight_turn": None,
        }
        server._sessions["live1"] = session
        return session

    def test_zero_tool_resume_of_tooled_live_session_fails_closed(
        self, server, monkeypatch
    ) -> None:
        session = self._live(server, monkeypatch, agent_toolsets=["terminal", "file"])
        resp = _resume(server, {"session_id": self.TARGET, "enabled_toolsets": []})
        assert "error" in resp
        assert "result" not in resp
        # No mutation, no tooled fallback: the live agent keeps its selection.
        assert session["agent"].enabled_toolsets == ["terminal", "file"]
        assert session.get("create_enabled_toolsets") is None

    def test_matching_selection_reuses_live_session(self, server, monkeypatch) -> None:
        self._live(server, monkeypatch, agent_toolsets=[])
        resp = _resume(server, {"session_id": self.TARGET, "enabled_toolsets": []})
        assert "error" not in resp
        assert resp["result"]["resumed"] == self.TARGET

    def test_absent_field_reuses_live_session(self, server, monkeypatch) -> None:
        self._live(server, monkeypatch, agent_toolsets=["terminal"])
        resp = _resume(server, {"session_id": self.TARGET})
        assert "error" not in resp
        assert resp["result"]["resumed"] == self.TARGET


# ── compute host ─────────────────────────────────────────────────────


class TestComputeHost:
    def test_turn_frame_carries_selection(self, server) -> None:
        session = {
            "history": [],
            "history_lock": threading.Lock(),
            "history_version": 0,
            "attached_images": [],
            "session_key": "k-frame",
            "cols": 80,
            "cwd": "/tmp",
            "create_enabled_toolsets": [],
        }
        frame = server._compute_host_turn_frame("rid", "sid", session, "hi")
        assert frame["enabled_toolsets"] == []

    def test_turn_frame_omits_default_when_unset(self, server) -> None:
        session = {
            "history": [],
            "history_lock": threading.Lock(),
            "history_version": 0,
            "attached_images": [],
            "session_key": "k-frame2",
            "cols": 80,
            "cwd": "/tmp",
        }
        frame = server._compute_host_turn_frame("rid", "sid", session, "hi")
        assert frame.get("enabled_toolsets") is None

    def test_host_first_build_receives_selection(self, server, monkeypatch) -> None:
        from tui_gateway.compute_host import ComputeHost

        calls = _capture_builds(server, monkeypatch)
        monkeypatch.setattr(
            server,
            "_init_session",
            lambda sid, key, agent, history, **kw: server._sessions.__setitem__(
                sid,
                {
                    "agent": agent,
                    "session_key": key,
                    "history": list(history),
                    "history_lock": threading.Lock(),
                },
            ),
        )
        host = ComputeHost(stdout=io.StringIO())
        try:
            host._ensure_server_session(
                server,
                {
                    "sid": "host-sid",
                    "session_key": "host-key",
                    "history": [],
                    "cols": 80,
                    "enabled_toolsets": [],
                },
            )
        finally:
            host._executor.shutdown(wait=False)
        assert calls and calls[-1]["enabled_toolsets_override"] == []

    def test_host_first_build_without_field_is_unchanged(self, server, monkeypatch) -> None:
        from tui_gateway.compute_host import ComputeHost

        calls = _capture_builds(server, monkeypatch)
        monkeypatch.setattr(
            server,
            "_init_session",
            lambda sid, key, agent, history, **kw: server._sessions.__setitem__(
                sid, {"agent": agent, "session_key": key, "history": list(history),
                      "history_lock": threading.Lock()},
            ),
        )
        host = ComputeHost(stdout=io.StringIO())
        try:
            host._ensure_server_session(
                server,
                {"sid": "host-sid2", "session_key": "host-key2", "history": [], "cols": 80},
            )
        finally:
            host._executor.shutdown(wait=False)
        assert calls[-1].get("enabled_toolsets_override") is None


# ── later rebuilds of the same session ───────────────────────────────


class TestRebuilds:
    def test_new_conversation_rebuild_keeps_the_barrier(self, server, monkeypatch) -> None:
        calls = _capture_builds(server, monkeypatch)
        monkeypatch.setattr(server, "_set_session_context", lambda *a, **k: None)
        monkeypatch.setattr(server, "_clear_session_context", lambda *a, **k: None)
        monkeypatch.setattr(server, "_config_model_target", lambda *a, **k: "")
        monkeypatch.setattr(server, "_session_info", lambda *a, **k: {})
        monkeypatch.setattr(server, "_load_show_reasoning", lambda *a, **k: False)
        monkeypatch.setattr(server, "_load_tool_progress_mode", lambda *a, **k: "off")
        session = {
            "agent": SimpleNamespace(enabled_toolsets=[]),
            "session_key": "k-new",
            "history": [],
            "history_lock": threading.Lock(),
            "history_version": 0,
            "create_enabled_toolsets": [],
        }
        server._reset_session_agent("sid-new", session)
        assert calls[-1]["enabled_toolsets_override"] == []
        assert session["create_enabled_toolsets"] == []

    def test_background_agent_inherits_empty_selection(self, server, monkeypatch) -> None:
        monkeypatch.setattr(server, "_load_cfg", lambda *a, **k: {})
        monkeypatch.setattr(server, "_load_enabled_toolsets", lambda: ["terminal", "file"])
        agent = SimpleNamespace(enabled_toolsets=[])
        kwargs = server._background_agent_kwargs(agent, "task-1")
        assert kwargs["enabled_toolsets"] == []

    def _reload_mcp(self, server, monkeypatch, agent_toolsets, pinned):
        seen: dict = {}
        fake_mcp = MagicMock()
        fake_mcp.refresh_agent_mcp_tools.side_effect = (
            lambda agent, enabled_override=None, **kw: seen.update(
                {"enabled_override": enabled_override}
            )
        )
        session = {
            "session_key": "k-mcp",
            "agent": SimpleNamespace(enabled_toolsets=agent_toolsets),
            "create_enabled_toolsets": pinned,
        }
        monkeypatch.setattr(server, "_load_enabled_toolsets", lambda: ["terminal", "file"])
        monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a, **k: False)
        monkeypatch.setattr(server, "_session_info", lambda *a, **k: {})
        monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
        with patch.dict(server._sessions, {"s-mcp": session}, clear=False), patch.dict(
            "sys.modules", {"tools.mcp_tool": fake_mcp}
        ):
            resp = server.handle_request(
                {
                    "id": "r-mcp",
                    "method": "reload.mcp",
                    "params": {"session_id": "s-mcp", "confirm": True},
                }
            )
        assert "error" not in resp
        return seen

    def test_reload_mcp_keeps_empty_selection(self, server, monkeypatch) -> None:
        seen = self._reload_mcp(server, monkeypatch, [], pinned=[])
        assert seen["enabled_override"] == []

    def test_reload_mcp_without_selection_re_resolves_global(
        self, server, monkeypatch
    ) -> None:
        seen = self._reload_mcp(server, monkeypatch, None, pinned=None)
        assert seen["enabled_override"] == ["terminal", "file"]

    def test_background_agent_without_selection_uses_global(self, server, monkeypatch) -> None:
        monkeypatch.setattr(server, "_load_cfg", lambda *a, **k: {})
        monkeypatch.setattr(server, "_load_enabled_toolsets", lambda: ["terminal", "file"])
        agent = SimpleNamespace(enabled_toolsets=None)
        kwargs = server._background_agent_kwargs(agent, "task-2")
        assert kwargs["enabled_toolsets"] == ["terminal", "file"]


class TestSessionBranch:
    class _BranchDB:
        def get_session_title(self, _key):
            return "parent"

        def get_next_title_in_lineage(self, current):
            return f"{current} 2"

        def create_session(self, *a, **k):
            return None

        def append_message(self, **k):
            return None

        def set_session_title(self, *a, **k):
            return None

    def _branch(self, server, monkeypatch, live: dict) -> dict:
        calls = _capture_builds(server, monkeypatch)
        monkeypatch.setattr(server, "_get_db", lambda: self._BranchDB())
        monkeypatch.setattr(server, "_claim_active_session_slot", lambda *a, **k: (None, None))
        monkeypatch.setattr(server, "_new_session_key", lambda: "child-key")
        monkeypatch.setattr(server, "_resolve_model", lambda *a, **k: "m")
        monkeypatch.setattr(
            server,
            "_init_session",
            lambda sid, key, agent, history, **kw: server._sessions.__setitem__(
                sid,
                {"agent": agent, "session_key": key, "history": list(history),
                 "history_lock": threading.Lock()},
            ),
        )
        ready = threading.Event()
        ready.set()
        live.update({
            "agent_ready": ready,
            "history_lock": threading.Lock(),
            "cwd": "/tmp",
            "cols": 80,
        })
        with patch.dict(server._sessions, {"s-parent": live}, clear=True):
            resp = server.handle_request(
                {"id": "r-branch", "method": "session.branch",
                 "params": {"session_id": "s-parent"}}
            )
            assert "error" not in resp, resp
            child = server._sessions[resp["result"]["session_id"]]
            return {"calls": calls, "child": child, "result": resp["result"]}

    def test_branch_keeps_the_empty_selection(self, server, monkeypatch) -> None:
        live = {
            "session_key": "parent-key",
            "agent": SimpleNamespace(enabled_toolsets=[]),
            "history": [{"role": "user", "content": "hi"}],
            "create_enabled_toolsets": [],
        }
        out = self._branch(server, monkeypatch, live)
        assert out["calls"][-1]["enabled_toolsets_override"] == []
        assert out["child"].get("create_enabled_toolsets") == []
        assert out["child"]["agent"].enabled_toolsets == []

    def test_branch_without_selection_is_unchanged(self, server, monkeypatch) -> None:
        live = {
            "session_key": "parent-key",
            "agent": SimpleNamespace(enabled_toolsets=["terminal"]),
            "history": [{"role": "user", "content": "hi"}],
        }
        out = self._branch(server, monkeypatch, live)
        assert out["calls"][-1].get("enabled_toolsets_override") is None
        assert out["child"].get("create_enabled_toolsets") is None


class TestReloadMcpProvenance:
    """The decision comes from what the client asked, not from the built agent.

    A session created WITHOUT the field already carries a resolved global list
    on its agent, so reading the agent would wrongly pin it forever.
    """

    def _reload(self, server, monkeypatch, session_extra: dict):
        seen: dict = {}
        fake_mcp = MagicMock()
        fake_mcp.refresh_agent_mcp_tools.side_effect = (
            lambda agent, enabled_override=None, **kw: seen.update(
                {"enabled_override": enabled_override}
            )
        )
        session = {"session_key": "k-mcp", **session_extra}
        monkeypatch.setattr(server, "_load_enabled_toolsets", lambda: ["terminal", "file", "mcp_new"])
        monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a, **k: False)
        monkeypatch.setattr(server, "_session_info", lambda *a, **k: {})
        monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
        with patch.dict(server._sessions, {"s-mcp": session}, clear=False), patch.dict(
            "sys.modules", {"tools.mcp_tool": fake_mcp}
        ):
            resp = server.handle_request(
                {"id": "r-mcp", "method": "reload.mcp",
                 "params": {"session_id": "s-mcp", "confirm": True}}
            )
        assert "error" not in resp
        return seen

    def test_global_session_re_resolves_config(self, server, monkeypatch) -> None:
        seen = self._reload(
            server,
            monkeypatch,
            {"agent": SimpleNamespace(enabled_toolsets=["terminal", "file"])},
        )
        assert seen["enabled_override"] == ["terminal", "file", "mcp_new"]

    def test_explicitly_empty_session_stays_empty(self, server, monkeypatch) -> None:
        seen = self._reload(
            server,
            monkeypatch,
            {"agent": SimpleNamespace(enabled_toolsets=[]), "create_enabled_toolsets": []},
        )
        assert seen["enabled_override"] == []

    def test_explicit_list_stays_pinned(self, server, monkeypatch) -> None:
        seen = self._reload(
            server,
            monkeypatch,
            {"agent": SimpleNamespace(enabled_toolsets=["file"]),
             "create_enabled_toolsets": ["file"]},
        )
        assert seen["enabled_override"] == ["file"]


TOOLSET_CONFLICT_FRAGMENT = "different toolset selection"


def _conflict_error_type():
    from tui_gateway.compute_host import ToolsetScopeConflictError

    return ToolsetScopeConflictError


class TestComputeHostScopeConflicts:
    def _host(self):
        from tui_gateway.compute_host import ComputeHost

        return ComputeHost(stdout=io.StringIO())

    def test_existing_session_refuses_incompatible_frame(self, server, monkeypatch) -> None:
        calls = _capture_builds(server, monkeypatch)
        agent = SimpleNamespace(enabled_toolsets=["terminal"])
        session = {
            "agent": agent,
            "session_key": "host-key",
            "history": [],
            "history_lock": threading.Lock(),
        }
        host = self._host()
        try:
            with patch.dict(server._sessions, {"host-sid": session}, clear=True):
                with pytest.raises(
                    _conflict_error_type(), match=TOOLSET_CONFLICT_FRAGMENT
                ):
                    host._ensure_server_session(
                        server,
                        {"sid": "host-sid", "session_key": "host-key",
                         "history": [], "cols": 80, "enabled_toolsets": []},
                    )
                # No mutation, no silently tooled agent handed back.
                assert session["agent"].enabled_toolsets == ["terminal"]
                assert session.get("create_enabled_toolsets") is None
        finally:
            host._executor.shutdown(wait=False)
        assert calls == []

    def _race(self, server, monkeypatch, second_selection):
        """Start a build, hold it, then send a second frame on the same sid.

        Returns the host plus the observation hooks, with the first build still
        blocked so the caller can inspect the lock table mid-race.
        """
        state = {
            "entered": threading.Event(),
            "release": threading.Event(),
            "waiting": threading.Event(),
            "calls": [],
            "errors": [],
            "results": [],
        }
        calls_lock = threading.Lock()

        def fake_make_agent(sid, key, **kw):
            with calls_lock:
                state["calls"].append(kw)
                first = len(state["calls"]) == 1
            if first:
                state["entered"].set()
                assert state["release"].wait(timeout=10), "test gate never released"
            return SimpleNamespace(
                enabled_toolsets=kw.get("enabled_toolsets_override"),
                model="m", provider="p", session_id=key,
            )

        monkeypatch.setattr(server, "_make_agent", fake_make_agent)
        monkeypatch.setattr(
            server,
            "_init_session",
            lambda sid, key, agent, history, **kw: server._sessions.__setitem__(
                sid,
                {"agent": agent, "session_key": key, "history": list(history),
                 "history_lock": threading.Lock()},
            ),
        )

        host = self._host()
        # Signal the exact moment the second caller has registered its interest
        # in the sid and is about to block: an Event, so no timing guesswork.
        original_acquire = host._acquire_session_lock

        def spy_acquire(sid):
            lock, entry = original_acquire(sid)
            if entry.waiters >= 2:
                state["waiting"].set()
            return lock, entry

        host._acquire_session_lock = spy_acquire

        def run(frame):
            try:
                state["results"].append(host._ensure_server_session(server, frame))
            except BaseException as exc:  # noqa: BLE001 - recorded for the assert
                state["errors"].append(exc)

        base = {"sid": "race-sid", "session_key": "race-key", "history": [], "cols": 80}
        state["threads"] = (
            threading.Thread(target=run, args=({**base, "enabled_toolsets": []},)),
            threading.Thread(
                target=run, args=({**base, "enabled_toolsets": second_selection},)
            ),
        )
        return host, state

    def _finish_race(self, state) -> None:
        state["release"].set()
        for thread in state["threads"]:
            thread.join(timeout=15)
        assert all(not t.is_alive() for t in state["threads"])

    def test_concurrent_first_builds_cannot_both_win(self, server, monkeypatch) -> None:
        """Two incompatible frames on a fresh sid: one builds, the other fails."""
        host, state = self._race(server, monkeypatch, ["terminal"])
        first, second = state["threads"]
        try:
            with patch.dict(server._sessions, {}, clear=True):
                first.start()
                assert state["entered"].wait(timeout=10), "first build never started"
                second.start()
                assert state["waiting"].wait(timeout=10), "second caller never queued"
                self._finish_race(state)
                assert len(state["errors"]) == 1, (state["errors"], state["results"])
                assert isinstance(state["errors"][0], _conflict_error_type())
                assert TOOLSET_CONFLICT_FRAGMENT in str(state["errors"][0])
                assert len(state["results"]) == 1
                # The zero-tool builder entered first and must be the one live.
                assert server._sessions["race-sid"]["agent"].enabled_toolsets == []
                assert len(state["calls"]) == 1, state["calls"]
        finally:
            state["release"].set()
            host._executor.shutdown(wait=False)

    def test_lock_entry_is_shared_during_the_race_then_evicted(
        self, server, monkeypatch
    ) -> None:
        host, state = self._race(server, monkeypatch, ["terminal"])
        first, second = state["threads"]
        try:
            with patch.dict(server._sessions, {}, clear=True):
                first.start()
                assert state["entered"].wait(timeout=10)
                second.start()
                assert state["waiting"].wait(timeout=10)
                # Both callers share ONE entry while the race is on.
                assert list(host._session_locks) == ["race-sid"]
                assert host._session_locks["race-sid"].waiters == 2
                self._finish_race(state)
                # Winner and loser both released: no per-sid lock leaks.
                assert host._session_locks == {}
        finally:
            state["release"].set()
            host._executor.shutdown(wait=False)

    def test_lock_table_is_empty_after_a_conflict(self, server, monkeypatch) -> None:
        _capture_builds(server, monkeypatch)
        session = {
            "agent": SimpleNamespace(enabled_toolsets=["terminal"]),
            "session_key": "host-key",
            "history": [],
            "history_lock": threading.Lock(),
        }
        host = self._host()
        try:
            with patch.dict(server._sessions, {"host-sid": session}, clear=True):
                with pytest.raises(_conflict_error_type()):
                    host._ensure_server_session(
                        server,
                        {"sid": "host-sid", "session_key": "host-key",
                         "history": [], "cols": 80, "enabled_toolsets": []},
                    )
            assert host._session_locks == {}
        finally:
            host._executor.shutdown(wait=False)

    def test_lock_is_released_before_the_turn_runs(self, server, monkeypatch) -> None:
        """The lock guards ensure/build only, never the model turn after it."""
        _capture_builds(server, monkeypatch)
        monkeypatch.setattr(
            server,
            "_init_session",
            lambda sid, key, agent, history, **kw: server._sessions.__setitem__(
                sid,
                {"agent": agent, "session_key": key, "history": list(history),
                 "history_lock": threading.Lock()},
            ),
        )
        host = self._host()
        try:
            with patch.dict(server._sessions, {}, clear=True):
                host._ensure_server_session(
                    server,
                    {"sid": "turn-sid", "session_key": "turn-key",
                     "history": [], "cols": 80, "enabled_toolsets": []},
                )
                assert host._session_locks == {}
                # Whatever the turn does next, the sid is immediately lockable.
                lock, entry = host._acquire_session_lock("turn-sid")
                try:
                    assert lock.acquire(blocking=False)
                    lock.release()
                finally:
                    host._release_session_lock("turn-sid", entry)
                assert host._session_locks == {}
        finally:
            host._executor.shutdown(wait=False)


def _reporting_server():
    # Real toolsets/model_tools registry: these RPCs answer from it, so a
    # mocked hermes_constants would make the answer meaningless.
    import tui_gateway.server as server

    return server


def _list_toolsets(server, method: str, session: dict | None) -> list[dict]:
    sessions = {"s-report": session} if session is not None else {}
    with patch.dict(server._sessions, sessions, clear=True):
        params = {"session_id": "s-report"} if session is not None else {}
        resp = server._methods[method]("r-report", params)
    assert "error" not in resp, resp
    return resp["result"]["toolsets"]


def _show_tools(server, session: dict | None) -> dict:
    sessions = {"s-report": session} if session is not None else {}
    with patch.dict(server._sessions, sessions, clear=True):
        params = {"session_id": "s-report"} if session is not None else {}
        resp = server._methods["tools.show"]("r-show", params)
    assert "error" not in resp, resp
    return resp["result"]


@pytest.mark.parametrize("method", ["tools.list", "toolsets.list"])
class TestToolsetReporting:
    def test_empty_selection_reports_every_toolset_disabled(self, method) -> None:
        server = _reporting_server()
        session = {"session_key": "k", "agent": SimpleNamespace(enabled_toolsets=[])}
        items = _list_toolsets(server, method, session)
        assert items, "expected the toolset catalog to be non-empty"
        assert [i["name"] for i in items if i["enabled"]] == []

    def test_explicit_selection_reports_only_that_toolset(self, method) -> None:
        server = _reporting_server()
        session = {"session_key": "k", "agent": SimpleNamespace(enabled_toolsets=["file"])}
        items = _list_toolsets(server, method, session)
        assert [i["name"] for i in items if i["enabled"]] == ["file"]

    def test_absent_selection_still_reports_everything_enabled(self, method) -> None:
        server = _reporting_server()
        session = {"session_key": "k", "agent": SimpleNamespace(enabled_toolsets=None)}
        items = _list_toolsets(server, method, session)
        assert all(i["enabled"] for i in items)

    def test_prebuild_session_reports_its_pinned_empty_selection(self, method) -> None:
        server = _reporting_server()
        session = {"session_key": "k", "agent": None, "create_enabled_toolsets": []}
        items = _list_toolsets(server, method, session)
        assert [i["name"] for i in items if i["enabled"]] == []

    def test_no_session_keeps_the_global_answer(self, method, monkeypatch) -> None:
        server = _reporting_server()
        monkeypatch.setattr(server, "_load_enabled_toolsets", lambda: None)
        items = _list_toolsets(server, method, None)
        assert all(i["enabled"] for i in items)

    def test_response_shape_is_unchanged(self, method) -> None:
        server = _reporting_server()
        session = {"session_key": "k", "agent": SimpleNamespace(enabled_toolsets=[])}
        items = _list_toolsets(server, method, session)
        for item in items:
            assert {"name", "description", "tool_count", "enabled"} <= set(item)
            assert isinstance(item["enabled"], bool)


class TestToolsShowReporting:
    def test_empty_selection_shows_no_tool(self) -> None:
        server = _reporting_server()
        session = {"session_key": "k", "agent": SimpleNamespace(enabled_toolsets=[])}
        result = _show_tools(server, session)
        assert result["total"] == 0
        assert result["sections"] == []

    def test_absent_selection_still_shows_tools(self) -> None:
        server = _reporting_server()
        session = {"session_key": "k", "agent": SimpleNamespace(enabled_toolsets=None)}
        assert _show_tools(server, session)["total"] > 0


class TestTurnOwnershipCleanup:
    """Only the caller that acquired the turn may release it.

    A concurrent frame that is refused (scope conflict) runs the same except
    block; if that block cleared `running` / `inflight_turn`, it would unlock a
    session another turn is still using and let a third turn start in parallel.
    """

    def _host(self):
        from tui_gateway.compute_host import ComputeHost

        return ComputeHost(stdout=io.StringIO())

    def _live_session(self, server, sid: str, selection):
        session = {
            "agent": SimpleNamespace(enabled_toolsets=selection, model="m",
                                     provider="p", session_id="own-key"),
            "session_key": "own-key",
            "history": [],
            "history_lock": threading.Lock(),
            "history_version": 0,
            "running": False,
            "inflight_turn": None,
            "last_active": 0.0,
        }
        server._sessions[sid] = session
        return session

    def test_refused_frame_does_not_release_the_owner_turn(
        self, server, monkeypatch
    ) -> None:
        in_turn = threading.Event()
        release = threading.Event()

        def blocking_submit(request_id, sid, session, text):
            in_turn.set()
            assert release.wait(timeout=10), "test gate never released"

        monkeypatch.setattr(server, "_run_prompt_submit", blocking_submit)
        monkeypatch.setattr(server, "_ensure_session_db_row", lambda *a, **k: None)
        monkeypatch.setattr(server, "_persist_branch_seed", lambda *a, **k: None)
        monkeypatch.setattr(server, "_session_info", lambda *a, **k: {})

        host = self._host()
        events: list[dict] = []
        host.emit = events.append
        owner = threading.Thread(
            target=host._run_real_turn,
            args=({"sid": "own-sid", "session_key": "own-key", "request_id": "owner",
                   "text": "hi", "enabled_toolsets": []},),
        )
        try:
            with patch.dict(server._sessions, {}, clear=True):
                session = self._live_session(server, "own-sid", [])
                owner.start()
                assert in_turn.wait(timeout=10), "owner turn never started"

                # A concurrent, incompatible frame is refused.
                host._run_real_turn(
                    {"sid": "own-sid", "session_key": "own-key", "request_id": "intruder",
                     "text": "hi", "enabled_toolsets": ["terminal"]}
                )
                refused = [e for e in events if e.get("request_id") == "intruder"]
                assert refused and refused[-1]["type"] == "turn.error"

                # The owner's turn state survives the refusal.
                assert session["running"] is True
                assert session["inflight_turn"] is not None

                # And a third turn is still locked out while the owner runs.
                host._run_real_turn(
                    {"sid": "own-sid", "session_key": "own-key", "request_id": "third",
                     "text": "hi", "enabled_toolsets": []}
                )
                third = [e for e in events if e.get("request_id") == "third"]
                assert third and third[-1]["type"] == "turn.error"
                assert "busy" in third[-1]["message"]

                release.set()
                owner.join(timeout=15)
                assert not owner.is_alive()
        finally:
            release.set()
            host._executor.shutdown(wait=False)

    def test_owner_still_cleans_up_its_own_failure(self, server, monkeypatch) -> None:
        def boom(request_id, sid, session, text):
            raise RuntimeError("turn exploded")

        monkeypatch.setattr(server, "_run_prompt_submit", boom)
        monkeypatch.setattr(server, "_ensure_session_db_row", lambda *a, **k: None)
        monkeypatch.setattr(server, "_persist_branch_seed", lambda *a, **k: None)
        monkeypatch.setattr(server, "_session_info", lambda *a, **k: {})

        host = self._host()
        events: list[dict] = []
        host.emit = events.append
        try:
            with patch.dict(server._sessions, {}, clear=True):
                session = self._live_session(server, "own-sid", [])
                host._run_real_turn(
                    {"sid": "own-sid", "session_key": "own-key", "request_id": "owner",
                     "text": "hi", "enabled_toolsets": []}
                )
                assert session["running"] is False
                assert session["inflight_turn"] is None
                assert events[-1]["type"] == "turn.error"
        finally:
            host._executor.shutdown(wait=False)


class TestReloadMcpLiftsRestriction:
    """reload.mcp on an unpinned session must follow the global config, even
    when that config resolves to "everything" (None)."""

    def test_global_none_config_lifts_the_agent_restriction(
        self, server, monkeypatch
    ) -> None:
        agent = SimpleNamespace(
            enabled_toolsets=["file"], disabled_toolsets=None,
            tools=[], valid_tool_names=set(),
        )
        session = {
            "session_key": "k-mcp",
            "agent": agent,
            "create_enabled_toolsets": None,
        }
        seen: dict = {}

        import model_tools

        def _defs(**kw):
            seen.update(kw)
            return []

        monkeypatch.setattr(model_tools, "get_tool_definitions", _defs)
        monkeypatch.setattr(server, "_load_enabled_toolsets", lambda: None)
        monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a, **k: False)
        monkeypatch.setattr(server, "_session_info", lambda *a, **k: {})
        monkeypatch.setattr(server, "_emit", lambda *a, **k: None)

        import tools.mcp_tool as mcp_tool

        monkeypatch.setattr(mcp_tool, "shutdown_mcp_servers", lambda *a, **k: None,
                            raising=False)
        monkeypatch.setattr(mcp_tool, "discover_mcp_tools", lambda *a, **k: None,
                            raising=False)

        with patch.dict(server._sessions, {"s-mcp": session}, clear=False):
            resp = server.handle_request(
                {"id": "r-mcp", "method": "reload.mcp",
                 "params": {"session_id": "s-mcp", "confirm": True}}
            )

        assert "error" not in resp
        assert resp["result"]["status"] == "reloaded"
        # The restriction is gone: the rebuild asked for every toolset.
        assert seen["enabled_toolsets"] is None
        assert agent.enabled_toolsets is None
