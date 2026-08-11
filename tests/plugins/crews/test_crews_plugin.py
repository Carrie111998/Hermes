"""Tests for the crews plugin backend (plugins/crews/dashboard/plugin_api.py).

Focus on the pure logic — store CRUD, pydantic validation, DAG validation and
topological layering — without spawning real `hermes` workers.
"""

import importlib.util
import asyncio
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PLUGIN = _REPO_ROOT / "plugins" / "crews" / "dashboard" / "plugin_api.py"


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    hermes_home = tmp_path_factory.mktemp("hermes-home")
    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_crews_test", _PLUGIN)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    mod._store["crews"] = {}
    mod._workflow_store["workflows"] = {}
    # Point the stores at the tmp HERMES_HOME for later assertions.
    mod._hermes_home = lambda: hermes_home  # type: ignore[method-assign]
    return mod


@pytest.fixture(autouse=True)
def _fresh_stores(api):
    api._store["crews"] = {}
    api._workflow_store["workflows"] = {}
    yield
    api._store["crews"] = {}
    api._workflow_store["workflows"] = {}


def test_create_crew_roundtrip(api):
    result = asyncio.run(api.create_crew(api.CreateCrewBody(name="Squad", goal="Ship it", members=[
        api.MemberInput(persona="kai"),
        api.MemberInput(persona="ada", model="auto"),
    ])))
    crew = result["crew"]
    assert crew["name"] == "Squad"
    assert crew["status"] == "draft"
    assert len(crew["members"]) == 2
    assert crew["members"][0]["persona"] == "kai"
    assert crew["members"][0]["displayName"].startswith("⚡")
    assert crew["members"][1]["model"] == "auto"
    assert api.get_crew(crew["id"]) is not None


def test_create_crew_unknown_persona_rejected(api):
    with pytest.raises(Exception) as exc:
        asyncio.run(api.create_crew(api.CreateCrewBody(name="Bad", members=[api.MemberInput(persona="nope")])))
    assert "Unknown persona" in str(exc.value)


def test_crew_at_most_8_members(api):
    members = [api.MemberInput(persona="kai") for _ in range(9)]
    with pytest.raises(Exception) as exc:
        asyncio.run(api.create_crew(api.CreateCrewBody(name="Big", members=members)))
    assert "at most 8" in str(exc.value)


def test_clone_crew(api):
    crew = asyncio.run(api.create_crew(api.CreateCrewBody(name="Original", members=[api.MemberInput(persona="luna")])))["crew"]
    clone = asyncio.run(api.clone_crew(crew["id"]))["crew"]
    assert clone["id"] != crew["id"]
    assert clone["name"] == "Original (copy)"
    assert len(clone["members"]) == 1
    assert all(m["status"] == "idle" for m in clone["members"])


def test_member_status_patch(api):
    crew = asyncio.run(api.create_crew(api.CreateCrewBody(name="S", members=[api.MemberInput(persona="nova")])))["crew"]
    member_id = crew["members"][0]["id"]
    result = asyncio.run(api.patch_crew(crew["id"], api.PatchCrewBody(memberId=member_id, memberStatus="running")))
    assert result["crew"]["members"][0]["status"] == "running"


def test_workflow_upsert_and_cycle_rejection(api):
    crew = asyncio.run(api.create_crew(api.CreateCrewBody(name="W", members=[api.MemberInput(persona="kai")])))["crew"]
    tasks = [
        api.WorkflowTaskBody(id="a", label="A", prompt="do a"),
        api.WorkflowTaskBody(id="b", label="B", prompt="do b"),
    ]
    edges = [api.WorkflowEdgeBody(from_="a", to="b")]
    workflow = asyncio.run(api.upsert_workflow(crew["id"], api.UpsertWorkflowBody(tasks=tasks, edges=edges)))
    assert workflow["workflow"]["edges"] == [{"from": "a", "to": "b"}]

    # Cycle must be rejected server-side.
    cycle_edges = [api.WorkflowEdgeBody(from_="a", to="b"), api.WorkflowEdgeBody(from_="b", to="a")]
    with pytest.raises(Exception) as exc:
        asyncio.run(api.upsert_workflow(crew["id"], api.UpsertWorkflowBody(tasks=tasks, edges=cycle_edges)))
    assert "cycle" in str(exc.value)


def test_topo_layers(api):
    layers = api._topo_layers(
        [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}],
        [{"from": "a", "to": "c"}, {"from": "b", "to": "c"}, {"from": "c", "to": "d"}],
    )
    # a,b (parallel) → c → d
    assert set(layers[0]) == {"a", "b"}
    assert layers[1] == ["c"]
    assert layers[2] == ["d"]


def test_validate_dag_unknown_edge(api):
    with pytest.raises(Exception) as exc:
        api._validate_dag(
            [{"id": "a"}],
            [{"from": "a", "to": "ghost"}],
        )
    assert "unknown task" in str(exc.value)


def test_profile_name_sanitization(api):
    member = {"persona": "roger", "profileName": "../evil"}
    assert api._profile_name_for(member) == "evil"
    assert api._profile_name_for({"persona": "kai", "profileName": ""}) == "kai"


def test_personas_and_templates(api):
    personas = api.list_personas()["personas"]
    assert len(personas) == 8
    assert {p["id"] for p in personas} == {"roger", "sally", "bill", "ada", "max", "luna", "kai", "nova"}
    templates = api.list_templates()["templates"]
    assert len(templates) >= 3
