"""The CLI plan surface, and the advisory tool-handler flag.

THE CLI IS NO LONGER AN APPROVAL AUTHORITY. It displays the authoritative plan
and refuses to decide: every approval attempt fails closed because no separately
authenticated approval surface is configured.

That refusal does not depend on recognising the caller, which is the point. An
earlier design confirmed a phrase on /dev/tty and tried to detect agent origin
through a ContextVar, an environment marker and a PID registry. A reproduction
showed a same-user process can erase every local marker, orphan itself and
allocate its own PTY — kernel-indistinguishable from a human shell. So the tests
below assert the refusal holds in a *clean* context too, not only in contexts a
heuristic can spot.

The ContextVar tests remain because the flag remains — as an advisory signal for
logging and for a future adapter's own courtesy checks, explicitly not a
boundary.
"""

import argparse
import concurrent.futures
import threading

import pytest

from agent.delegation_context import in_tool_handler, tool_handler_context
from hermes_cli import approval_broker as ab
from hermes_cli import kanban_db as kb
from hermes_cli import projects_cmd

REAL_BODY = "REAL PLAN: ship the thing"


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb._INITIALIZED_PATHS.clear()
    return tmp_path


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for v in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_CRON_SESSION",
              "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_SESSION_SOURCE",
              "HERMES_SESSION_PLATFORM"):
        monkeypatch.delenv(v, raising=False)


def _seed(board, body=REAL_BODY):
    conn = kb.connect(db_path=board / "kanban.db")
    tid = kb.create_task(conn, title="ship it", assignee="a")
    conn.execute(
        "INSERT INTO pm_projects (id, slug, name, plan_revision, archived, created_at)"
        " VALUES ('p1','p1','Proj',1,0,1)"
    )
    conn.execute(
        "INSERT INTO pm_plans (project_id, revision, body, proposed_by, proposed_at)"
        " VALUES ('p1',1,?, 'pm', 1)", (body,),
    )
    assert kb.park_for_plan_approval(conn, tid, project_id="p1", revision=1) is True
    conn.close()
    return tid


def _state(board, tid):
    conn = kb.connect(db_path=board / "kanban.db")
    try:
        return kb.get_task(conn, tid).status, kb.gate_state_of(conn, tid)
    finally:
        conn.close()


def _run(action, tid, reason=None):
    """Drive the real CLI handler. There is no terminal to stub any more."""
    args = argparse.Namespace(task_id=tid, reason=reason)
    fn = (projects_cmd._cmd_approve_plan if action == "approve"
          else projects_cmd._cmd_reject_plan)
    return fn(args)


# ===================== the ContextVar ======================================

def test_flag_is_false_outside_a_tool_call():
    assert in_tool_handler() is False


def test_flag_is_set_inside_and_cleared_after():
    with tool_handler_context():
        assert in_tool_handler() is True
    assert in_tool_handler() is False


def test_flag_survives_nesting_correctly():
    """An inner tool finishing must not unmark the outer one."""
    with tool_handler_context():
        with tool_handler_context():
            assert in_tool_handler() is True
        assert in_tool_handler() is True
    assert in_tool_handler() is False


def test_flag_is_cleared_after_an_exception():
    with pytest.raises(RuntimeError):
        with tool_handler_context():
            raise RuntimeError("tool blew up")
    assert in_tool_handler() is False


def test_flag_is_cleared_after_a_cancellation():
    with pytest.raises(KeyboardInterrupt):
        with tool_handler_context():
            raise KeyboardInterrupt
    assert in_tool_handler() is False


def test_flag_does_not_leak_between_threads():
    """The gateway runs turns concurrently; a global flag would cross-talk."""
    seen = {}
    started = threading.Event()
    release = threading.Event()

    def marked():
        with tool_handler_context():
            started.set()
            release.wait(2)
            seen["marked"] = in_tool_handler()

    def unmarked():
        started.wait(2)
        seen["unmarked"] = in_tool_handler()
        release.set()

    t1, t2 = threading.Thread(target=marked), threading.Thread(target=unmarked)
    t1.start(); t2.start(); t1.join(3); t2.join(3)
    assert seen["marked"] is True
    assert seen["unmarked"] is False


def test_flag_does_not_leak_across_pooled_workers():
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        def marked():
            with tool_handler_context():
                return in_tool_handler()
        assert pool.submit(marked).result() is True
        # Same worker thread, next task: must start clean.
        assert pool.submit(in_tool_handler).result() is False


def test_the_dispatcher_marks_and_unmarks(monkeypatch):
    """handle_function_call is the chokepoint every tool path funnels through."""
    import model_tools
    seen = {}

    def fake_inner(name, args_, *a, **k):
        seen["inside"] = in_tool_handler()
        return "{}"

    monkeypatch.setattr(model_tools, "_handle_function_call_inner", fake_inner)
    model_tools.handle_function_call("x", {})
    assert seen["inside"] is True
    assert in_tool_handler() is False


def test_the_dispatcher_unmarks_after_a_raising_tool(monkeypatch):
    import model_tools

    def boom(name, args_, *a, **k):
        raise RuntimeError("tool raised past its own cleanup")

    monkeypatch.setattr(model_tools, "_handle_function_call_inner", boom)
    with pytest.raises(RuntimeError):
        model_tools.handle_function_call("x", {})
    assert in_tool_handler() is False


def test_a_tool_context_cannot_mint_an_attestation():
    with tool_handler_context():
        with pytest.raises(ab.NoApprovalSurfaceError):
            ab.for_plan_decision(project_id="p1", revision=1, plan_body="b",
                                 decision="approved")


def test_a_tool_that_shells_out_is_still_marked(monkeypatch):
    """The signal is contextual, so it survives into whatever the tool calls."""
    import model_tools
    captured = {}

    def inner(name, args_, *a, **k):
        # Whatever this tool invokes runs in the same context.
        captured["nested"] = in_tool_handler()
        try:
            ab.deny_agent_provenance()
            captured["denied"] = False
        except ab.ApprovalProvenanceError:
            captured["denied"] = True
        return "{}"

    monkeypatch.setattr(model_tools, "_handle_function_call_inner", inner)
    model_tools.handle_function_call("x", {})
    assert captured == {"nested": True, "denied": True}


# ===================== the CLI surface =====================================
#
# It shows the plan. It does not decide.


def test_cli_approve_fails_closed(board):
    tid = _seed(board)
    assert _run("approve", tid) == 3
    assert _state(board, tid) == ("scheduled", "plan")


def test_cli_reject_fails_closed(board):
    """Rejection releases the gate too, so it gets no local authority either."""
    tid = _seed(board)
    assert _run("reject", tid, reason="too broad") == 3
    assert _state(board, tid) == ("scheduled", "plan")


def test_the_refusal_names_the_missing_surface(board, capsys):
    tid = _seed(board)
    _run("approve", tid)
    err = capsys.readouterr().err
    assert "no separately authenticated approval surface" in err
    assert "deliberate" in err


def test_the_cli_still_displays_the_authoritative_plan(board, capsys):
    """Requirement: show the operator exactly what is pending."""
    tid = _seed(board, body="UNIQUE-PLAN-MARKER-42")
    _run("approve", tid)
    out = capsys.readouterr().out
    assert "UNIQUE-PLAN-MARKER-42" in out
    assert "p1" in out and "Revision : 1" in out
    assert tid in out


def test_the_displayed_plan_comes_from_the_database(board, capsys):
    """Multi-line bodies are shown whole, from the authoritative row."""
    body = "line one\nline two\nline three"
    tid = _seed(board, body=body)
    _run("approve", tid)
    out = capsys.readouterr().out
    for line in body.split("\n"):
        assert line in out


def test_no_approval_row_is_written(board):
    tid = _seed(board)
    _run("approve", tid)
    _run("reject", tid)
    conn = kb.connect(db_path=board / "kanban.db")
    try:
        assert conn.execute("SELECT COUNT(*) c FROM pm_approvals").fetchone()["c"] == 0
        row = conn.execute(
            "SELECT approved_by, approved_at, rejected_at FROM pm_plans"
            " WHERE project_id='p1' AND revision=1").fetchone()
        assert all(v in (None, "") for v in tuple(row))
    finally:
        conn.close()


def test_the_refusal_does_not_depend_on_detecting_the_caller(board):
    """The property the redesign rests on.

    A clean, unmarked, human-looking context is refused identically to a worker
    context. There is nothing to spoof into, because nothing is being detected.
    """
    tid = _seed(board)
    assert _run("approve", tid) == 3
    assert _state(board, tid) == ("scheduled", "plan")


@pytest.mark.parametrize("var,val", [
    ("HERMES_KANBAN_TASK", "t_x"),
    ("HERMES_CRON_SESSION", "1"),
    ("HERMES_DELEGATED_CHILD_CONTEXT", "1"),
    ("HERMES_SESSION_SOURCE", "kanban"),
    ("HERMES_SESSION_PLATFORM", "telegram"),
])
def test_cli_refuses_from_every_non_human_context(board, monkeypatch, var, val):
    """Telegram, gateway, cron, worker and delegated contexts: all refused."""
    tid = _seed(board)
    monkeypatch.setenv(var, val)
    assert _run("approve", tid) == 3
    assert _state(board, tid) == ("scheduled", "plan")


def test_cli_refuses_inside_a_tool_handler(board):
    tid = _seed(board)
    with tool_handler_context():
        assert _run("approve", tid) == 3
    assert _state(board, tid) == ("scheduled", "plan")


def test_stdin_cannot_satisfy_the_gate(board, monkeypatch):
    """A piped 'approve' reaches nothing: nothing reads an answer any more."""
    import io
    tid = _seed(board)
    monkeypatch.setattr("sys.stdin", io.StringIO("approve\napprove\n"))
    assert _run("approve", tid) == 3
    assert _state(board, tid) == ("scheduled", "plan")


def test_there_is_no_flag_that_forces_approval(board):
    """No --yes, --force or override reaches the gate."""
    tid = _seed(board)
    # Even if a caller fabricates override attributes, nothing reads them.
    args = argparse.Namespace(task_id=tid, reason=None, yes=True, force=True,
                              approve=True, non_interactive=True)
    assert projects_cmd._cmd_approve_plan(args) == 3
    assert _state(board, tid) == ("scheduled", "plan")


def test_cli_resolves_the_artifact_from_the_db_not_the_caller(board):
    """The only caller input is a task id; everything else is looked up."""
    import inspect
    sig = inspect.signature(projects_cmd._gate_decision)
    assert list(sig.parameters) == ["args", "decision"]
    tid = _seed(board)
    conn = kb.connect(db_path=board / "kanban.db")
    ctx = kb.plan_gate_context(conn, tid)
    conn.close()
    assert ctx["project_id"] == "p1" and ctx["revision"] == 1
    assert ctx["body"] == REAL_BODY


def test_cli_refuses_an_ungated_task(board):
    conn = kb.connect(db_path=board / "kanban.db")
    tid = kb.create_task(conn, title="plain", assignee="a")
    conn.close()
    assert _run("approve", tid) == 1


def test_cli_refuses_an_unknown_task(board):
    _seed(board)
    assert _run("approve", "t_nope") == 1


def test_cli_requires_a_task_id(board):
    assert _run("approve", "") == 2
