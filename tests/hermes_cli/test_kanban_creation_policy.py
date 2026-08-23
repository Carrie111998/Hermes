"""Behavior tests for the principal-aware Kanban creation gate."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_decompose as decompose
from hermes_cli import kanban_policy as policy
from hermes_cli import kanban_swarm
from tools import kanban_tools


RESTRICTED_PROFILES = ("colbert", "julien", "ruth")


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _set_policy(
    monkeypatch,
    *,
    profile: str,
    can_create: bool,
    dashboard_policy: str = "authenticated",
) -> None:
    monkeypatch.setenv("HERMES_PROFILE", profile)
    monkeypatch.setattr(
        policy,
        "_load_policy_config",
        lambda: {
            "kanban": {
                "can_create": can_create,
                "dashboard_create_policy": dashboard_policy,
            }
        },
    )


@pytest.mark.parametrize("profile", RESTRICTED_PROFILES)
def test_profile_policy_denies_restricted_experts(profile):
    decision = policy.creation_decision(
        policy.KanbanPrincipal(policy.PROFILE_PRINCIPAL, profile),
        config={"kanban": {"can_create": False}},
    )
    assert decision.allowed is False
    assert profile in decision.reason


@pytest.mark.parametrize("profile", ("amber", "architect"))
def test_profile_policy_allows_orchestrators_and_root_workers(profile):
    decision = policy.creation_decision(
        policy.KanbanPrincipal(policy.PROFILE_PRINCIPAL, profile),
        config={"kanban": {"can_create": True}},
    )
    assert decision.allowed is True


def test_policy_load_error_fails_closed(monkeypatch):
    def broken_config():
        raise OSError("unreadable")

    monkeypatch.setattr(policy, "_load_policy_config", broken_config)
    decision = policy.creation_decision(
        policy.KanbanPrincipal(policy.PROFILE_PRINCIPAL, "amber")
    )
    assert decision.allowed is False
    assert "fail-closed" in decision.reason


def test_authenticated_dashboard_policy_is_independent_of_profile_gate():
    principal = policy.dashboard_human_principal("human-1")
    allowed = policy.creation_decision(
        principal,
        config={
            "kanban": {
                "can_create": False,
                "dashboard_create_policy": "authenticated",
            }
        },
    )
    disabled = policy.creation_decision(
        principal,
        config={"kanban": {"dashboard_create_policy": "disabled"}},
    )
    assert allowed.allowed is True
    assert disabled.allowed is False


@pytest.mark.parametrize("profile", RESTRICTED_PROFILES)
def test_cli_create_link_and_swarm_deny_before_database_calls(
    monkeypatch, profile, capsys
):
    _set_policy(monkeypatch, profile=profile, can_create=False)
    connect = MagicMock(side_effect=AssertionError("database must not open"))
    monkeypatch.setattr(kb, "connect_closing", connect)

    assert kanban_cli._cmd_create(SimpleNamespace()) == 1
    assert kanban_cli._cmd_link(SimpleNamespace()) == 1
    assert kanban_cli._cmd_swarm(SimpleNamespace()) == 1
    connect.assert_not_called()

    err = capsys.readouterr().err
    assert err.count("kanban.can_create=false") == 3


@pytest.mark.parametrize("profile", RESTRICTED_PROFILES)
def test_swarm_programmatic_entry_denies_before_write(monkeypatch, profile):
    _set_policy(monkeypatch, profile=profile, can_create=False)
    conn = MagicMock()

    with pytest.raises(policy.KanbanCreationDenied):
        kanban_swarm.create_swarm(
            conn,
            goal="deny this graph",
            workers=[],
            verifier_assignee="architect",
            synthesizer_assignee="amber",
        )

    conn.execute.assert_not_called()


@pytest.mark.parametrize("profile", RESTRICTED_PROFILES)
def test_agent_tool_create_and_link_deny_before_database_calls(
    monkeypatch, profile
):
    _set_policy(monkeypatch, profile=profile, can_create=False)
    monkeypatch.setattr(kanban_tools, "_check_kanban_mode", lambda: True)
    connect = MagicMock(side_effect=AssertionError("database must not open"))
    monkeypatch.setattr(kb, "connect_closing", connect)

    create_result = kanban_tools._handle_create(
        {"title": "must not exist", "assignee": "worker"}
    )
    link_result = kanban_tools._handle_link(
        {"parent_id": "t_parent", "child_id": "t_child"}
    )

    assert "refused" in create_result
    assert "refused" in link_result
    connect.assert_not_called()


def _fake_aux_response(payload: dict) -> MagicMock:
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = json.dumps(payload)
    return response


@pytest.mark.parametrize("profile", RESTRICTED_PROFILES)
def test_decompose_fanout_denies_without_creating_children(
    kanban_home, monkeypatch, profile
):
    _set_policy(monkeypatch, profile=profile, can_create=False)
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="restricted fanout", triage=True)

    roster = [
        SimpleNamespace(
            name="amber",
            is_default=True,
            description="orchestrator",
            description_auto=False,
            model="m",
            provider="p",
            skill_count=1,
        )
    ]
    monkeypatch.setattr(decompose.profiles_mod, "list_profiles", lambda: roster)
    monkeypatch.setattr(decompose.profiles_mod, "profile_exists", lambda name: name == "amber")
    monkeypatch.setattr(decompose.profiles_mod, "get_active_profile_name", lambda: "amber")
    monkeypatch.setattr(
        "agent.auxiliary_client.call_llm",
        lambda **kwargs: _fake_aux_response(
            {
                "fanout": True,
                "rationale": "parallel work",
                "tasks": [
                    {
                        "title": "child",
                        "body": "must not be persisted",
                        "assignee": "amber",
                        "parents": [],
                    }
                ],
            }
        ),
    )

    outcome = decompose.decompose_task(task_id, author=profile)

    assert outcome.ok is False
    assert "kanban.can_create=false" in outcome.reason
    with kb.connect_closing() as conn:
        rows = kb.list_tasks(conn, include_archived=True, limit=20)
        root = kb.get_task(conn, task_id)
    assert [task.id for task in rows] == [task_id]
    assert root is not None and root.status == "triage"


def _dashboard_client() -> TestClient:
    plugin_file = (
        Path(__file__).resolve().parents[2]
        / "plugins"
        / "kanban"
        / "dashboard"
        / "plugin_api.py"
    )
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_creation_policy_test",
        plugin_file,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/kanban")
    return TestClient(app)


@pytest.mark.parametrize("profile", RESTRICTED_PROFILES)
def test_dashboard_human_can_create_and_link_independently(
    kanban_home, monkeypatch, profile
):
    _set_policy(monkeypatch, profile=profile, can_create=False)
    client = _dashboard_client()

    parent = client.post(
        "/api/plugins/kanban/tasks", json={"title": "parent"}
    )
    child = client.post(
        "/api/plugins/kanban/tasks", json={"title": "child"}
    )
    assert parent.status_code == 200
    assert child.status_code == 200
    link = client.post(
        "/api/plugins/kanban/links",
        json={
            "parent_id": parent.json()["task"]["id"],
            "child_id": child.json()["task"]["id"],
        },
    )
    assert link.status_code == 200


def test_dashboard_disabled_policy_denies_before_database_write(
    kanban_home, monkeypatch
):
    _set_policy(
        monkeypatch,
        profile="amber",
        can_create=True,
        dashboard_policy="disabled",
    )
    client = _dashboard_client()

    response = client.post(
        "/api/plugins/kanban/tasks", json={"title": "must not exist"}
    )
    assert response.status_code == 403
    with kb.connect_closing() as conn:
        assert kb.list_tasks(conn, include_archived=True, limit=20) == []


def test_root_worker_tool_create_is_allowed(kanban_home, monkeypatch):
    _set_policy(monkeypatch, profile="architect", can_create=True)
    monkeypatch.setattr(kanban_tools, "_check_kanban_mode", lambda: True)

    result = kanban_tools._handle_create(
        {"title": "root-created", "assignee": "worker"}
    )

    assert json.loads(result)["ok"] is True
    with kb.connect_closing() as conn:
        tasks = kb.list_tasks(conn, limit=20)
    assert [task.title for task in tasks] == ["root-created"]
