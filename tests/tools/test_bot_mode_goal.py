"""Tests for the Bot-Chat-only ``goal_manage`` native GoalManager bridge."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_cli import goals
from tools import bot_mode_goal, bot_mode_probe


@pytest.fixture(autouse=True)
def _clean_goal_context():
    bot_mode_probe._reset_cache_for_tests()
    goals._DB_CACHE.clear()
    goals._GOAL_DIRTY_SESSIONS.clear()
    yield
    bot_mode_probe._reset_cache_for_tests()
    goals._DB_CACHE.clear()
    goals._GOAL_DIRTY_SESSIONS.clear()


def _managed_home(tmp_path: Path) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    teammate = home / "profiles" / "researcher"
    teammate.mkdir(parents=True)
    (teammate / "profile.yaml").write_text(
        textwrap.dedent(
            """\
            description: teammate for tests
            ui_meta:
              hermes-bots:
                shape: cloud
            """
        ),
        encoding="utf-8",
    )
    return home


class _FakeDB:
    def __init__(self, home: Path, title: str):
        self.db_path = str(home / "state.db")
        self._title = title

    def get_session_title(self, _sid):
        return self._title


class _FakeAgent:
    def __init__(self, home: Path, title: str = "Bot Chat", sid: str = "sess-goal"):
        self._session_db = _FakeDB(home, title)
        self.session_id = sid
        self._session_title_hint = None
        self._bot_mode_protocol = True
        self.tools: list = []
        self.valid_tool_names: set = set()


def _call(home: Path, agent: _FakeAgent, **kwargs):
    token = set_hermes_home_override(home)
    try:
        raw = bot_mode_goal.goal_manage_tool(
            session_id=agent.session_id,
            agent=agent,
            **kwargs,
        )
        return json.loads(raw)
    finally:
        reset_hermes_home_override(token)


def test_injects_only_into_managed_bot_chat(tmp_path):
    home = _managed_home(tmp_path)
    agent = _FakeAgent(home)
    assert bot_mode_goal.ensure_goal_manage_tool(agent) is True
    assert [t["function"]["name"] for t in agent.tools] == ["goal_manage"]
    assert "goal_manage" in agent.valid_tool_names
    assert bot_mode_goal.ensure_goal_manage_tool(agent) is True
    assert len(agent.tools) == 1


@pytest.mark.parametrize("title", ["", "ordinary", "Group: test", "handoff-1"])
def test_never_injects_outside_canonical_bot_chat(tmp_path, title):
    home = _managed_home(tmp_path)
    agent = _FakeAgent(home, title=title)
    assert bot_mode_goal.ensure_goal_manage_tool(agent) is False
    assert agent.tools == []


def test_never_injects_on_unmanaged_install(tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    agent = _FakeAgent(home)
    assert bot_mode_goal.ensure_goal_manage_tool(agent) is False


def test_schema_never_in_global_registry_or_toolsets():
    from tools.registry import registry
    import toolsets

    assert bot_mode_goal.GOAL_MANAGE_TOOL_NAME not in getattr(registry, "_tools", {})
    for names in toolsets.TOOLSETS.values():
        assert bot_mode_goal.GOAL_MANAGE_TOOL_NAME not in names


def test_dispatch_refuses_outside_bot_chat(tmp_path):
    home = _managed_home(tmp_path)
    agent = _FakeAgent(home, title="ordinary")
    result = _call(home, agent, action="set", goal="ship it")
    assert result["success"] is False
    assert "Bot Chat" in result["error"]


def test_set_uses_native_goal_contract_and_persists(tmp_path):
    home = _managed_home(tmp_path)
    agent = _FakeAgent(home, sid="native-contract")
    result = _call(
        home,
        agent,
        action="set",
        goal="learn API authorization",
        max_turns=7,
        outcome="demonstrate authorization boundary reasoning",
        verification="pass a novel transfer review",
        constraints="minimum sufficient instruction",
        boundaries="learner-local writes only",
        stop_when="mastered or blocked",
    )
    assert result["success"] is True
    assert result["status"] == "active"
    assert result["max_turns"] == 7
    assert result["contract"]["verification"] == "pass a novel transfer review"

    token = set_hermes_home_override(home)
    try:
        persisted = goals.GoalManager("native-contract").state
        assert persisted is not None
        assert persisted.goal == "learn API authorization"
        assert persisted.max_turns == 7
        assert persisted.contract.outcome == "demonstrate authorization boundary reasoning"
        assert persisted.contract.boundaries == "learner-local writes only"
    finally:
        reset_hermes_home_override(token)


def test_set_cannot_replace_existing_goal(tmp_path):
    home = _managed_home(tmp_path)
    agent = _FakeAgent(home, sid="replace-guard")
    assert _call(home, agent, action="set", goal="first")["success"] is True
    refused = _call(home, agent, action="set", goal="second")
    assert refused["success"] is False
    assert refused["goal"] == "first"
    assert "cannot replace" in refused["error"]


def test_add_subgoal_uses_native_manager(tmp_path):
    home = _managed_home(tmp_path)
    agent = _FakeAgent(home, sid="mutations")
    assert _call(home, agent, action="set", goal="main")["success"] is True
    added = _call(home, agent, action="add_subgoal", criterion="prove tenant isolation")
    assert added["subgoals"] == ["prove tenant isolation"]


def test_schema_exposes_no_destructive_goal_controls():
    schema = bot_mode_goal.goal_manage_tool_schema()
    props = schema["function"]["parameters"]["properties"]
    actions = set(props["action"]["enum"])
    assert actions == {"set", "status", "add_subgoal"}
    assert "replace" not in props
    assert "reason" not in props


def test_dirty_marker_refreshes_cli_cached_goal_manager(tmp_path):
    """A goal set during an agent turn must wake the CLI's same native loop."""
    home = _managed_home(tmp_path)
    sid = "cli-cache-refresh"
    token = set_hermes_home_override(home)
    try:
        from cli import HermesCLI

        cli = HermesCLI.__new__(HermesCLI)
        cli.session_id = sid
        cli._goal_manager = None
        cached = cli._get_goal_manager()
        assert cached is not None
        assert cached.state is None

        agent = _FakeAgent(home, sid=sid)
        raw = bot_mode_goal.goal_manage_tool(
            action="set",
            session_id=sid,
            agent=agent,
            goal="persist from tool",
            verification="native state is visible to CLI hook",
        )
        assert json.loads(raw)["success"] is True

        refreshed = cli._get_goal_manager()
        assert refreshed is not cached
        assert refreshed.is_active()
        assert refreshed.state.goal == "persist from tool"
        assert refreshed.state.contract.verification == "native state is visible to CLI hook"
        assert goals.consume_goal_state_dirty(sid) is False
    finally:
        reset_hermes_home_override(token)


def test_cli_post_turn_hook_judges_goal_created_by_tool(tmp_path, monkeypatch):
    """The real CLI host hook must consume a goal created during the turn."""
    import queue
    from cli import HermesCLI

    home = _managed_home(tmp_path)
    sid = "cli-post-turn"
    token = set_hermes_home_override(home)
    try:
        cli = HermesCLI.__new__(HermesCLI)
        cli.session_id = sid
        cli._goal_manager = None
        cli._pending_input = queue.Queue()
        cli._last_turn_interrupted = False
        cli.conversation_history = [
            {"role": "assistant", "content": "HOOK_DONE"},
        ]
        cached = cli._get_goal_manager()
        assert cached.state is None

        agent = _FakeAgent(home, sid=sid)
        result = json.loads(
            bot_mode_goal.goal_manage_tool(
                action="set",
                session_id=sid,
                agent=agent,
                goal="prove host hook",
                verification="assistant response contains HOOK_DONE",
            )
        )
        assert result["success"] is True

        monkeypatch.setattr(
            "hermes_cli.goals.judge_goal",
            lambda *args, **kwargs: ("done", "verified", False, None, False),
        )
        monkeypatch.setattr(
            "hermes_cli.goals.gather_background_processes", lambda: []
        )
        cli._maybe_continue_goal_after_turn()

        persisted = goals.GoalManager(sid).state
        assert persisted is not None
        assert persisted.status == "done"
        assert persisted.last_verdict == "done"
        assert cli._pending_input.empty()
    finally:
        reset_hermes_home_override(token)


def test_missing_session_id_is_rejected(tmp_path):
    home = _managed_home(tmp_path)
    agent = _FakeAgent(home)
    token = set_hermes_home_override(home)
    try:
        result = json.loads(
            bot_mode_goal.goal_manage_tool(
                action="set",
                session_id="",
                agent=agent,
                goal="nope",
            )
        )
    finally:
        reset_hermes_home_override(token)
    assert result["success"] is False
    assert "session id" in result["error"].lower()
