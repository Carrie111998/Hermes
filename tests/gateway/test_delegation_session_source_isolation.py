"""Delegated children must not inherit a human messaging surface identity."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent.delegation_context import (
    DELEGATED_CHILD_ENV_MARKER,
    delegated_child_context,
)
from gateway.session_context import (
    _SESSION_PLATFORM,
    _SESSION_SOURCE,
    session_is_messaging_surface,
)
from hermes_state import SessionDB
from run_agent import AIAgent, _session_source_for_agent


@pytest.fixture
def discord_parent_context():
    platform_token = _SESSION_PLATFORM.set("discord")
    source_token = _SESSION_SOURCE.set("discord")
    try:
        yield
    finally:
        _SESSION_SOURCE.reset(source_token)
        _SESSION_PLATFORM.reset(platform_token)


def test_root_agent_keeps_ambient_discord_source(discord_parent_context):
    assert _session_source_for_agent("gateway") == "discord"
    assert session_is_messaging_surface() is True


def test_delegated_child_uses_subagent_source_not_parent_discord(
    discord_parent_context,
):
    with delegated_child_context("child-session"):
        assert _session_source_for_agent("subagent") == "subagent"
        assert _session_source_for_agent("discord") == "subagent"


def test_delegated_child_is_not_a_human_messaging_surface(
    discord_parent_context,
):
    with delegated_child_context("child-session"):
        assert session_is_messaging_surface() is False


def test_subprocess_marker_isolates_source_and_messaging_surface(
    discord_parent_context,
    monkeypatch,
):
    monkeypatch.setenv(DELEGATED_CHILD_ENV_MARKER, "1")

    assert _session_source_for_agent("discord") == "subagent"
    assert session_is_messaging_surface() is False


def test_delegated_child_persists_as_subagent_under_discord_parent(
    discord_parent_context,
    tmp_path,
):
    db = SessionDB(db_path=tmp_path / "state.db")
    child = SimpleNamespace(
        _persist_disabled=False,
        _session_db_created=False,
        _session_db=db,
        _session_init_model_config={"_delegate_from": "parent-session"},
        _cached_system_prompt="test prompt",
        _parent_session_id=None,
        platform="discord",
        session_id="child-session",
        model="test-model",
    )
    try:
        with delegated_child_context("child-session"):
            AIAgent._ensure_db_session(child)  # type: ignore[arg-type]

        row = db.get_session("child-session")
        assert row is not None
        assert row["source"] == "subagent"
        assert json.loads(row["model_config"])["_delegate_from"] == "parent-session"
    finally:
        db.close()
