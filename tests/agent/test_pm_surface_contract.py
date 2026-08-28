"""What a PM orchestrator is told it can do, across every surface that tells it.

TWO CLAIMS, ONE MODEL
---------------------
Commit 12's correction gave the orchestrator a ``PM_PLAN_GATE_GUIDANCE`` block
saying no approval surface ships. The ``plan_submit`` schema and its success
note — inherited from commit 10, reaching the same model in the same context —
still said a human approves through "a separately authenticated surface". Both
cannot be true, and the reachable failure is the capable one: an agent that
believes the surface exists goes looking for it, or routes around the warning
to find it.

So the three things a PM orchestrator actually receives are treated here as one
contract:

* the built system prompt,
* the registered ``plan_submit`` schema the model is sent, and
* the string ``plan_submit`` returns on success.

None of these is source text. The prompt is assembled by
``build_system_prompt``; the schema is read out of the live tool registry
*after* the real orchestrator ``check_fn`` has run; the result is the return
value of the real handler against a real board.

PARKING IS A KERNEL, NOT A WORKFLOW
-----------------------------------
The plan gate's representation, release path, refusals and audit are all
implemented and tested. Nothing shipped puts a task into it. Tests elsewhere —
including the prompt byte-stability tests next door — park by calling
``kanban_db.park_for_plan_approval`` directly. That is legitimate evidence
about the kernel and is not evidence of a usable workflow, so the workflow
claim is asserted separately below: the orchestrator's real resolved tool
surface can author a plan and cannot gate one, and driving that surface for
real leaves the board ungated.
"""

from __future__ import annotations

import argparse
import json
import re
from types import SimpleNamespace

import pytest

from agent.system_prompt import build_system_prompt

# Verb shapes a gate-crossing tool would have to be named something like. A
# name check is necessary, not sufficient — see the docstring on the test.
GATE_VERBS = ("park", "unpark", "approve", "reject", "release", "gate")

# Every orchestrator-visible surface that talks about approval must say the
# surface does not exist.
DENIES_THE_SURFACE = re.compile(
    r"no\s+(?:\w+\s+){0,2}approval surface[^.]*ships", re.IGNORECASE)

# ...and none of them may describe how approval happens today. These name the
# *mechanism*, which is the claim that sends an agent looking.
CLAIMS_A_SURFACE = (
    re.compile(r"authenticated (?:human )?(?:surface|step)", re.IGNORECASE),
    re.compile(r"a human approves", re.IGNORECASE),
    re.compile(r"approves it through", re.IGNORECASE),
)


@pytest.fixture
def orchestrator(tmp_path, monkeypatch):
    """A real orchestrator process: kanban toolset, no owned task, real board."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("toolsets:\n  - kanban\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)

    import tools.kanban_tools  # noqa: F401  (registers the tools)
    from hermes_cli import kanban_db as kb
    from model_tools import _clear_tool_defs_cache
    from tools.registry import invalidate_check_fn_cache

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    invalidate_check_fn_cache()
    _clear_tool_defs_cache()
    return tmp_path


def _surface() -> dict:
    """Every kanban tool this process actually resolves, keyed by name.

    ``skip_tool_search_assembly=True`` is the pre-collapse catalog. Once the
    catalog is large enough ``get_tool_definitions`` folds tools behind
    ``tool_search``, and a folded tool is still reachable — so the complete
    reachable set is the honest thing to make a "there is no such verb" claim
    about.
    """
    from model_tools import get_tool_definitions

    defs = get_tool_definitions(
        enabled_toolsets=["kanban"], quiet_mode=True,
        skip_tool_search_assembly=True)
    return {d["function"]["name"]: d["function"] for d in defs if "function" in d}


def _agent(tool_names, **overrides):
    base = dict(
        load_soul_identity=False,
        skip_context_files=True,
        valid_tool_names=list(tool_names),
        _task_completion_guidance=False,
        _tool_use_enforcement=False,
        _environment_probe=False,
        _kanban_worker_guidance=None,   # resolve the way agent_init does
        _pm_plan_gate_guidance=None,
        _memory_store=None,
        _memory_manager=None,
        model="",
        provider="",
        platform="",
        pass_session_id=False,
        session_id="",
        tools=None,
        _emit_status=lambda *_a, **_k: None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _call(name: str, args: dict) -> str:
    from tools.registry import registry

    return registry._tools[name].handler(args)   # type: ignore[attr-defined]


def _rows(sql: str, params=()) -> list[dict]:
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _gated_task_ids() -> list[str]:
    return [r["id"] for r in _rows(
        "SELECT id FROM tasks WHERE gate_state IS NOT NULL ORDER BY id")]


def _project_verbs() -> set[str]:
    """The real ``hermes project`` verb list, read off the real subparser."""
    from hermes_cli import projects_cmd

    root = argparse.ArgumentParser()
    projects_cmd.build_parser(root.add_subparsers())
    verbs: set[str] = set()
    for action in root._subparsers._group_actions:
        for name, parser in action.choices.items():
            if name != "project":
                continue
            for sub in parser._subparsers._group_actions:
                verbs |= set(sub.choices)
    return verbs


# ---------------------------------------------------------------------------
# One contract across three surfaces
# ---------------------------------------------------------------------------

def _orchestrator_surfaces() -> dict[str, str]:
    """The three strings a PM orchestrator receives, all built for real."""
    surface = _surface()
    assert "plan_submit" in surface, sorted(surface)
    prompt = build_system_prompt(_agent(sorted(surface)))

    _call("project_ensure", {"project_id": "p_contract", "name": "contract"})
    result = _call("plan_submit", {"project_id": "p_contract", "body": "a plan"})
    assert json.loads(result)["ok"] is True, result

    return {
        "system prompt": prompt,
        "plan_submit schema": surface["plan_submit"]["description"],
        "plan_submit result": result,
    }


SURFACES = ["system prompt", "plan_submit schema", "plan_submit result"]


@pytest.mark.parametrize("which", SURFACES)
def test_each_orchestrator_surface_denies_that_an_approval_surface_ships(
    orchestrator, which
):
    text = _orchestrator_surfaces()[which]
    assert DENIES_THE_SURFACE.search(text), (
        f"the {which} never tells the model no approval surface ships; "
        f"a surface that stays silent while another denies it is the "
        f"contradiction this test exists for")


@pytest.mark.parametrize("which", SURFACES)
def test_no_orchestrator_surface_describes_a_working_approval_path(
    orchestrator, which
):
    """The defect: one surface denies the path while another explains it."""
    text = _orchestrator_surfaces()[which]
    hits = [p.pattern for p in CLAIMS_A_SURFACE if p.search(text)]
    assert hits == [], (
        f"the {which} claims approval happens through a surface that does not "
        f"exist: {hits}")


def test_the_contradiction_check_has_teeth():
    """A vacuous contract test is worse than none; prove the predicate fires."""
    contradiction = "A human approves it through a separately authenticated surface."
    assert not DENIES_THE_SURFACE.search(contradiction)
    assert [p.pattern for p in CLAIMS_A_SURFACE if p.search(contradiction)]

    honest = "No authenticated approval surface ships today."
    assert DENIES_THE_SURFACE.search(honest)
    assert not [p.pattern for p in CLAIMS_A_SURFACE if p.search(honest)]


# ---------------------------------------------------------------------------
# The orchestrator can author. It cannot gate.
# ---------------------------------------------------------------------------

def test_the_orchestrator_surface_can_submit_a_plan(orchestrator):
    surface = _surface()
    assert {"project_ensure", "plan_submit"} <= set(surface)

    _call("project_ensure", {"project_id": "p1", "name": "P"})
    out = json.loads(_call("plan_submit", {"project_id": "p1", "body": "do it"}))
    assert out["ok"] is True and out["revision"] == 1
    assert _rows("SELECT revision FROM pm_plans") == [{"revision": 1}]


def test_the_orchestrator_surface_names_no_park_or_release_verb(orchestrator):
    """Name-level contract over the tool surface the model can actually call.

    Necessary rather than sufficient — a parking tool could be named anything —
    so the behavioural claim is the next test. This one pins what a model
    scanning its own toolbox for a way through the gate would find.
    """
    offenders = sorted(
        n for n in _surface() if any(v in n.lower() for v in GATE_VERBS))
    assert offenders == [], (
        f"a gate verb appeared on the orchestrator surface: {offenders}. If a "
        f"parking or release tool now ships, PM_PLAN_GATE_GUIDANCE and "
        f"docs/pm.md both say it does not — update them together")


def test_driving_the_real_orchestrator_surface_never_gates_a_task(orchestrator):
    """The behavioural claim, with a positive control so it can actually fail."""
    from hermes_cli import kanban_db as kb

    _call("kanban_create", {"title": "root", "assignee": "coder"})
    tid = _rows("SELECT id FROM tasks")[0]["id"]
    _call("project_ensure", {"project_id": "p1", "name": "P"})
    _call("plan_submit", {"project_id": "p1", "body": "the plan"})
    _call("kanban_list", {})
    _call("kanban_show", {"task_id": tid})

    assert _gated_task_ids() == [], (
        "the shipped authoring surface put a task at a gate; if that is now "
        "intended, the docs saying nothing parks are wrong")
    assert _rows("SELECT COUNT(*) c FROM pm_approvals")[0]["c"] == 0

    # Positive control: the kernel really can gate this board, so the
    # assertion above is a fact about the surface, not about the fixture.
    conn = kb.connect()
    try:
        assert kb.park_for_plan_approval(
            conn, tid, project_id="p1", revision=1) is True
    finally:
        conn.close()
    assert _gated_task_ids() == [tid]


# ---------------------------------------------------------------------------
# The CLI has no park verb either, and its decision verbs do not decide
# ---------------------------------------------------------------------------

def test_the_project_command_exposes_no_park_verb():
    verbs = _project_verbs()
    assert verbs, "the project verb list must be discoverable"
    assert [v for v in verbs if "park" in v] == [], (
        f"`hermes project` grew a parking verb: {sorted(verbs)}")


def test_the_release_shaped_cli_verbs_do_not_release(orchestrator, capsys):
    """`approve-plan` is the closest thing that ships, and it fails closed."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import projects_cmd

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="root", assignee="coder")
        kb.ensure_pm_project(conn, project_id="p1", name="P")
        plan = kb.submit_plan(conn, project_id="p1", body="the plan")
        assert kb.park_for_plan_approval(
            conn, tid, project_id="p1", revision=plan["revision"]) is True
    finally:
        conn.close()

    root = argparse.ArgumentParser()
    projects_cmd.build_parser(root.add_subparsers())
    args = root.parse_args(["project", "approve-plan", tid])
    rc = projects_cmd.projects_command(args)

    assert rc != 0, "approve-plan must not report success"
    assert _gated_task_ids() == [tid], "the gate must still be closed"
    assert _rows("SELECT COUNT(*) c FROM pm_approvals")[0]["c"] == 0
