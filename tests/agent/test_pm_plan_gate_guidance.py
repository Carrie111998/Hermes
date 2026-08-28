"""Plan-gate guidance: who gets it, and what it is allowed to claim.

Commit 12's first attempt described the architecture the master specification
imagined — `awaiting_approval` and `ready_to_deploy` statuses, a delivered
deploy gate, a derived `SPAWNABLE_STATUSES`, five phases on every card. None of
that exists. `planning/M3B-ARCHITECTURE-RECONCILIATION.md` supersedes that
section, and these tests pin the guidance to what the code actually does by
driving the real gate API rather than writing statuses through raw SQL.

Two properties are load-bearing:

* the block reaches the orchestrator surface only, so an ordinary kanban worker
  on an ordinary board sees exactly the prompt it saw before commit 12;
* the built prompt does not move a byte when a real task is parked at, and
  released from, a real plan gate — G10, the prompt-cache invariant.
"""

from types import SimpleNamespace

import pytest

from agent.prompt_builder import KANBAN_GUIDANCE, PM_PLAN_GATE_GUIDANCE
from agent.system_prompt import build_system_prompt
from hermes_cli import approval_broker as ab
from hermes_cli import kanban_db as kb

GUIDANCE_CAP = 8000


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


WORKER_TOOLS = ["kanban_show", "kanban_complete", "kanban_block"]
ORCHESTRATOR_TOOLS = WORKER_TOOLS + ["kanban_list", "project_ensure", "plan_submit"]
CHAT_TOOLS = ["web_search"]


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return tmp_path


def _planned_task(conn, *, project_id="p_guidance", body="do the thing"):
    """A real project, a real plan revision, and a root task parked at the gate."""
    kb.ensure_pm_project(conn, project_id=project_id, name="guidance")
    plan = kb.submit_plan(conn, project_id=project_id, body=body)
    tid = kb.create_task(conn, title="root", assignee="coder")
    assert kb.park_for_plan_approval(
        conn, tid, project_id=project_id, revision=plan["revision"])
    return tid, plan


def _state(conn, tid):
    row = conn.execute(
        "SELECT status, gate_state FROM tasks WHERE id = ?", (tid,)).fetchone()
    return row["status"], row["gate_state"]


# --- who receives the block ------------------------------------------------

def test_an_ordinary_kanban_worker_sees_no_plan_gate_block(board):
    prompt = build_system_prompt(_agent(WORKER_TOOLS))
    assert "# Plan gates" not in prompt
    assert KANBAN_GUIDANCE in prompt, "its own guidance is untouched"


def test_the_orchestrator_surface_receives_it(board):
    prompt = build_system_prompt(_agent(ORCHESTRATOR_TOOLS))
    assert PM_PLAN_GATE_GUIDANCE in prompt


def test_an_ordinary_chat_session_receives_neither(board):
    prompt = build_system_prompt(_agent(CHAT_TOOLS))
    assert "# Plan gates" not in prompt
    assert "# Kanban task execution protocol" not in prompt


def test_a_profile_without_the_toolset_receives_neither(board):
    """Cron turns and profiles with no kanban toolset have no kanban tools."""
    prompt = build_system_prompt(_agent([]))
    assert "# Plan gates" not in prompt
    assert "# Kanban task execution protocol" not in prompt


def test_the_worker_block_is_unchanged_in_size(board):
    """Ordinary-board compatibility: commit 12 added nothing to this block."""
    assert len(KANBAN_GUIDANCE) == 6584
    assert len(KANBAN_GUIDANCE) <= GUIDANCE_CAP


# --- G10: real gate transitions do not move the prompt ---------------------

@pytest.mark.parametrize(
    "tools", [WORKER_TOOLS, ORCHESTRATOR_TOOLS],
    ids=["worker", "orchestrator"])
def test_the_prompt_is_byte_identical_across_a_real_gate_transition(
    board, monkeypatch, tools
):
    conn = kb.connect()
    try:
        before = build_system_prompt(_agent(tools))

        tid, plan = _planned_task(conn)
        assert _state(conn, tid) == ("scheduled", "plan"), "the real gate shape"
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        assert build_system_prompt(_agent(tools)) == before, "moved when parked"

        att = ab.issue_attestation_for_adapter(
            project_id="p_guidance", revision=plan["revision"],
            plan_body="do the thing", decision="approved",
            surface="test-adapter", operator_display="tester")
        released, err = kb.release_plan_gate(conn, tid, attestation=att)
        assert released, err
        status, gate = _state(conn, tid)
        assert gate is None, "the gate really cleared"
        assert status in kb.VALID_STATUSES, status
        assert build_system_prompt(_agent(tools)) == before, "moved when released"
    finally:
        conn.close()


def test_the_prompt_does_not_depend_on_plan_revisions(board):
    conn = kb.connect()
    try:
        before = build_system_prompt(_agent(ORCHESTRATOR_TOOLS))
        kb.ensure_pm_project(conn, project_id="p_rev", name="rev")
        for i in range(3):
            kb.submit_plan(conn, project_id="p_rev", body=f"revision {i}")
            assert build_system_prompt(_agent(ORCHESTRATOR_TOOLS)) == before
    finally:
        conn.close()


# --- what the block is allowed to claim ------------------------------------

def test_the_block_describes_the_real_gate_representation():
    assert "`scheduled`" in PM_PLAN_GATE_GUIDANCE
    assert "`gate_state` becomes `plan`" in PM_PLAN_GATE_GUIDANCE


@pytest.mark.parametrize("fiction", [
    "awaiting_approval", "ready_to_deploy", "SPAWNABLE_STATUSES",
    "pm-v1", "planning", "research", "building", "qa", "deploy gate",
])
def test_the_block_claims_nothing_that_is_not_built(fiction):
    assert fiction not in PM_PLAN_GATE_GUIDANCE, f"claims {fiction!r}"


@pytest.mark.parametrize("status", ["awaiting_approval", "ready_to_deploy"])
def test_those_statuses_really_do_not_exist(status):
    assert status not in kb.VALID_STATUSES


def test_there_is_no_spawnable_statuses_derivation():
    assert not hasattr(kb, "SPAWNABLE_STATUSES")


def test_no_writer_sets_a_deploy_gate(board):
    """`deploy` is a reserved name in VALID_GATE_STATES with no writer."""
    assert "deploy" in kb.VALID_GATE_STATES
    conn = kb.connect()
    try:
        tid, _ = _planned_task(conn, project_id="p_deploy")
        assert _state(conn, tid)[1] == "plan", "parking only ever writes 'plan'"
    finally:
        conn.close()


def test_the_block_says_no_approval_surface_ships():
    assert "No approval surface ships" in PM_PLAN_GATE_GUIDANCE
    assert "not a security boundary" in PM_PLAN_GATE_GUIDANCE
    assert "do not ask another agent to approve" in PM_PLAN_GATE_GUIDANCE


def test_that_claim_is_true(board):
    """The block says the local path fails closed; prove it does."""
    assert ab.resolve_plan_approval_adapter() is None
    with pytest.raises(ab.NoApprovalSurfaceError):
        ab.for_plan_decision(
            project_id="p", revision=1, plan_body="b", decision="approved")


def test_a_parked_task_is_not_dispatchable(board):
    """The block calls a parked task inert; prove the dispatcher agrees."""
    conn = kb.connect()
    try:
        tid, _ = _planned_task(conn, project_id="p_inert")
        ready = [t.id for t in kb.list_tasks(conn, status="ready")]
        assert tid not in ready
        assert kb.claim_task(conn, tid) is None, "a gated task cannot be claimed"
    finally:
        conn.close()
