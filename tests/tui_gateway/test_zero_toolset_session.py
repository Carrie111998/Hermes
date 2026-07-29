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

    def _reload_mcp(self, server, monkeypatch, agent_toolsets):
        """Drive reload.mcp on a session and return refresh's enabled_override."""
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
        assert self._reload_mcp(server, monkeypatch, [])["enabled_override"] == []

    def test_reload_mcp_without_selection_re_resolves_global(
        self, server, monkeypatch
    ) -> None:
        seen = self._reload_mcp(server, monkeypatch, None)
        assert seen["enabled_override"] == ["terminal", "file"]

    def test_background_agent_without_selection_uses_global(self, server, monkeypatch) -> None:
        monkeypatch.setattr(server, "_load_cfg", lambda *a, **k: {})
        monkeypatch.setattr(server, "_load_enabled_toolsets", lambda: ["terminal", "file"])
        agent = SimpleNamespace(enabled_toolsets=None)
        kwargs = server._background_agent_kwargs(agent, "task-2")
        assert kwargs["enabled_toolsets"] == ["terminal", "file"]
