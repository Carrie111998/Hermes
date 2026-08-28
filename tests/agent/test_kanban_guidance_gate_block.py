"""The gate/phase block in ``KANBAN_GUIDANCE`` (M3b commit 12).

Two properties matter here and they pull in opposite directions. The block has
to teach a worker that gates exist and that it cannot cross one — and it has to
do that without ever mentioning *this* card's gate or phase, because G10 makes
the system prompt byte-identical across any gate transition. A block that
personalised itself would read better and silently destroy the prompt cache on
every approval.

So the tests below drive a real board through both gates and assert the built
prompt does not move by a single byte, alongside the H.8 size cap.
"""

from types import SimpleNamespace

import pytest

from agent.prompt_builder import KANBAN_GUIDANCE
from agent.system_prompt import build_system_prompt
from hermes_cli import kanban_db as kb

# The plan's budget for this block: 8,000 chars, of which 6,584 were already
# spent before commit 12.
GUIDANCE_CAP = 8000


def _make_agent(**overrides):
    base = dict(
        load_soul_identity=False,
        skip_context_files=True,
        valid_tool_names=["kanban_show", "kanban_complete"],
        _task_completion_guidance=False,
        _tool_use_enforcement=False,
        _environment_probe=False,
        _kanban_worker_guidance=None,   # resolve the way agent_init does
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


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return tmp_path


# --- H.8: the block fits the budget ----------------------------------------

def test_the_guidance_still_fits_its_cap():
    assert len(KANBAN_GUIDANCE) <= GUIDANCE_CAP, (
        f"{len(KANBAN_GUIDANCE)} chars exceeds the {GUIDANCE_CAP} cap")


# --- the block says the things a worker acts on ----------------------------

@pytest.mark.parametrize("status", ["awaiting_approval", "ready_to_deploy"])
def test_both_gate_statuses_are_named(status):
    assert status in KANBAN_GUIDANCE


@pytest.mark.parametrize(
    "phase", ["planning", "research", "building", "qa", "deploy"])
def test_every_phase_is_named(phase):
    assert phase in KANBAN_GUIDANCE


def test_the_worker_is_told_it_cannot_approve():
    block = KANBAN_GUIDANCE[KANBAN_GUIDANCE.index("## Plan gates and phases"):]
    assert "cannot approve" in block
    assert "never be spawned on a gated card" in block
    assert "never wake you" in block, "G11, in the worker's own words"


def test_the_block_does_not_promise_an_approval_route():
    """No phrasing a worker could read as 'ask someone with the tool'."""
    block = KANBAN_GUIDANCE[KANBAN_GUIDANCE.index("## Plan gates and phases"):]
    assert "do not ask another agent to" in block
    for tempting in ("approve_plan(", "kanban_approve", "project approve"):
        assert tempting not in block, f"names an approval route: {tempting}"


# --- G10: the prompt is byte-identical across every gate transition --------

def _prompt(agent_overrides=None):
    return build_system_prompt(_make_agent(**(agent_overrides or {})))


def test_the_prompt_is_byte_identical_across_a_plan_gate_transition(board, monkeypatch):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="planned work", assignee="coder")
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)

        before = _prompt()
        for status in ("awaiting_approval", "ready_to_deploy"):
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
            conn.commit()
            assert _prompt() == before, f"the prompt moved at {status}"

        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        conn.commit()
        assert _prompt() == before, "and moved back"
    finally:
        conn.close()


def test_the_prompt_is_byte_identical_across_a_phase_change(board, monkeypatch):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="phased work", assignee="coder")
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        before = _prompt()
        for phase in ("planning", "research", "building", "qa", "deploy"):
            conn.execute(
                "UPDATE tasks SET current_step_key = ? WHERE id = ?", (phase, tid))
            conn.commit()
            assert _prompt() == before, f"the prompt moved at {phase}"
    finally:
        conn.close()


def test_an_ordinary_chat_session_never_sees_the_block(board):
    """The block is worker guidance; it must not leak into normal chat."""
    chat = build_system_prompt(_make_agent(valid_tool_names=["web_search"]))
    assert "## Plan gates and phases" not in chat
