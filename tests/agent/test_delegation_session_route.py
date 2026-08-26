"""User-owned session delegation routes — authorize, consume, never invent."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent.delegation_session_route import (
    DelegationRouteError,
    consume_route,
    handle_delegate_route_command,
    live_session_row_id,
    overlay_delegation_cfg,
    parse_delegate_route_args,
    peek_approved_route,
    route_metadata,
)
from hermes_state import SessionDB
from tools.delegate_tool import DELEGATE_TASK_SCHEMA, delegate_task


@pytest.fixture
def session_db(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("sess-route", source="cli", model="parent-model")
    return db


def test_parse_approve_and_clamp_unknown_effort():
    parsed = parse_delegate_route_args(
        "--provider nous --model ox-alpha --reasoning-effort nope --scope next"
    )
    assert parsed["action"] == "approve"
    assert parsed["provider"] == "nous"
    assert parsed["model"] == "ox-alpha"
    assert parsed["reasoning_effort"] == "nope"
    assert parsed["effective_reasoning_effort"] == "high"
    assert parsed["scope"] == "next"


def test_parse_requires_provider_and_model():
    with pytest.raises(DelegationRouteError):
        parse_delegate_route_args("--provider nous")


def test_live_session_row_id_prefers_agent_over_session_key():
    agent = SimpleNamespace(session_id="live-tip")
    assert live_session_row_id({"session_key": "stale-parent", "agent": agent}) == "live-tip"
    assert live_session_row_id(parent=agent) == "live-tip"


def test_approve_missing_session_row_fails(session_db):
    with pytest.raises(DelegationRouteError, match="No live session"):
        handle_delegate_route_command(
            "--provider nous --model ox-alpha --scope next",
            session_db,
            "missing-session",
        )
    assert peek_approved_route(session_db, "missing-session") is None


def test_store_and_peek_use_agent_session_id(session_db):
    handle_delegate_route_command(
        "--provider nous --model ox-alpha --scope next",
        session_db,
        live_session_row_id({"session_key": "stale-parent", "agent": SimpleNamespace(session_id="sess-route")}),
    )
    parent = SimpleNamespace(session_id="sess-route", _session_db=session_db)
    route = peek_approved_route(parent)
    assert route is not None
    assert route["requested"]["model"] == "ox-alpha"
    assert peek_approved_route(session_db, "stale-parent") is None


def test_approve_inspect_clear_round_trip(session_db):
    text = handle_delegate_route_command(
        "--provider nous --model ox-alpha --reasoning-effort max --scope session",
        session_db,
        "sess-route",
    )
    assert "Authorized for this session" in text
    route = peek_approved_route(session_db, "sess-route")
    assert route["requested"]["provider"] == "nous"
    assert route["requested"]["model"] == "ox-alpha"
    assert route["requested"]["reasoning_effort"] == "max"
    assert route["effective"]["reasoning_effort"] == "max"
    assert route["scope"] == "session"
    assert "Authorized delegation route" in handle_delegate_route_command(
        "", session_db, "sess-route"
    )
    assert "Cleared" in handle_delegate_route_command("clear", session_db, "sess-route")
    assert peek_approved_route(session_db, "sess-route") is None


def test_one_shot_consume_is_atomic(session_db):
    handle_delegate_route_command(
        "--provider nous --model ox-alpha --scope next",
        session_db,
        "sess-route",
    )
    route = peek_approved_route(session_db, "sess-route")
    assert consume_route(session_db, "sess-route", route["route_id"]) is True
    assert peek_approved_route(session_db, "sess-route") is None
    assert consume_route(session_db, "sess-route", route["route_id"]) is False


def test_session_scope_is_not_consumed(session_db):
    handle_delegate_route_command(
        "--provider nous --model ox-alpha --scope session",
        session_db,
        "sess-route",
    )
    route = peek_approved_route(session_db, "sess-route")
    assert consume_route(session_db, "sess-route", route["route_id"]) is False
    assert peek_approved_route(session_db, "sess-route")["route_id"] == route["route_id"]


def test_overlay_beats_global_pin():
    overlaid = overlay_delegation_cfg(
        {"provider": "openrouter", "model": "cheap", "reasoning_effort": "low"},
        {
            "requested": {"provider": "nous", "model": "ox-alpha", "reasoning_effort": "max"},
            "effective": {"provider": "nous", "model": "ox-alpha", "reasoning_effort": "max"},
        },
    )
    assert overlaid["provider"] == "nous"
    assert overlaid["model"] == "ox-alpha"
    assert overlaid["reasoning_effort"] == "max"


def test_schema_has_no_model_or_provider_fields():
    props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
    assert "model" not in props
    assert "provider" not in props
    assert "reasoning_effort" not in props
    assert "route_id" not in props


def test_delegate_task_applies_and_consumes_one_shot(session_db, monkeypatch):
    handle_delegate_route_command(
        "--provider nous --model ox-alpha --reasoning-effort max --scope next",
        session_db,
        "sess-route",
    )
    parent = SimpleNamespace(
        session_id="sess-route",
        _session_db=session_db,
        _delegate_depth=0,
        model="parent-model",
        provider="openrouter",
        enabled_toolsets=None,
    )
    captured = {}

    def fake_resolve(cfg, _parent):
        captured["cfg"] = dict(cfg)
        return {
            "model": cfg.get("model"),
            "provider": cfg.get("provider"),
            "base_url": None,
            "api_key": None,
            "api_mode": None,
        }

    def fake_build(**kwargs):
        captured["delegation_cfg"] = kwargs.get("delegation_cfg")
        child = SimpleNamespace(
            session_id="child-1",
            _subagent_id="sa-0-deadbeef",
            model=kwargs.get("model"),
        )
        return child

    monkeypatch.setattr("tools.delegate_tool._resolve_delegation_credentials", fake_resolve)
    monkeypatch.setattr("tools.delegate_tool._build_child_preserving_parent_tools", fake_build)
    monkeypatch.setattr(
        "tools.delegate_tool._run_single_child",
        lambda *_a, **_k: {"status": "ok", "summary": "done", "task_index": 0},
    )
    monkeypatch.setattr("tools.delegate_tool._finalize_child_results", lambda *_a, **_k: None)
    monkeypatch.setattr("tools.delegate_tool._get_max_spawn_depth", lambda: 2)
    monkeypatch.setattr("tools.delegate_tool._get_max_concurrent_children", lambda: 3)
    monkeypatch.setattr("tools.delegate_tool.is_spawn_paused", lambda: False)
    monkeypatch.setattr(
        "tools.delegate_tool._load_config",
        lambda: {"max_iterations": 10, "provider": "", "model": ""},
    )

    raw = delegate_task(goal="one off child", parent_agent=parent)
    payload = json.loads(raw)
    assert payload["results"][0]["status"] == "ok"
    assert payload["delegation_route"]["requested"]["model"] == "ox-alpha"
    assert payload["delegation_route"]["effective"]["provider"] == "nous"
    assert captured["cfg"]["model"] == "ox-alpha"
    assert captured["cfg"]["provider"] == "nous"
    assert captured["cfg"]["reasoning_effort"] == "max"
    assert peek_approved_route(session_db, "sess-route") is None


def test_delegate_task_cannot_invent_a_route_via_kwargs(session_db, monkeypatch):
    import inspect

    params = inspect.signature(delegate_task).parameters
    assert "model" not in params
    assert "provider" not in params
    assert "reasoning_effort" not in params

    parent = SimpleNamespace(
        session_id="sess-route",
        _session_db=session_db,
        _delegate_depth=0,
        model="parent-model",
        provider="openrouter",
        enabled_toolsets=None,
    )
    captured = {}

    def fake_resolve(cfg, _parent):
        captured["cfg"] = dict(cfg)
        return {
            "model": cfg.get("model"),
            "provider": cfg.get("provider"),
            "base_url": None,
            "api_key": None,
            "api_mode": None,
        }

    monkeypatch.setattr("tools.delegate_tool._resolve_delegation_credentials", fake_resolve)
    monkeypatch.setattr(
        "tools.delegate_tool._build_child_preserving_parent_tools",
        lambda **kwargs: SimpleNamespace(session_id="c", _subagent_id="sa-0-x", model=kwargs.get("model")),
    )
    monkeypatch.setattr(
        "tools.delegate_tool._run_single_child",
        lambda *_a, **_k: {"status": "ok", "summary": "done", "task_index": 0},
    )
    monkeypatch.setattr("tools.delegate_tool._finalize_child_results", lambda *_a, **_k: None)
    monkeypatch.setattr("tools.delegate_tool._get_max_spawn_depth", lambda: 2)
    monkeypatch.setattr("tools.delegate_tool._get_max_concurrent_children", lambda: 3)
    monkeypatch.setattr("tools.delegate_tool.is_spawn_paused", lambda: False)
    monkeypatch.setattr(
        "tools.delegate_tool._load_config",
        lambda: {"max_iterations": 10, "provider": "", "model": ""},
    )

    raw = delegate_task(goal="no invent", parent_agent=parent)
    payload = json.loads(raw)
    assert payload["results"][0]["status"] == "ok"
    assert "delegation_route" not in payload
    assert not captured["cfg"].get("model")
    assert not captured["cfg"].get("provider")


def test_route_metadata_prefers_resolved_creds():
    meta = route_metadata(
        {
            "route_id": "abc",
            "scope": "next",
            "requested": {"provider": "nous", "model": "ox-alpha"},
            "effective": {"provider": "nous", "model": "ox-alpha"},
        },
        {"provider": "nous", "model": "ox-alpha-clamped"},
    )
    assert meta["effective"]["model"] == "ox-alpha-clamped"
