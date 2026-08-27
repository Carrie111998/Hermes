"""The confirmed attacks, re-run against a surface that no longer exists.

WHY THESE TESTS LOOK STRANGE
----------------------------
Each one performs a real, previously-successful attack and asserts it fails.
But the assertion is deliberately NOT "a heuristic caught it" — it is "there was
nothing to attack". The refusal message must name the missing approval surface,
because a refusal that named a detected provenance signal would mean the old,
forgeable design had quietly come back.

WHAT THESE ATTACKS DID BEFORE
-----------------------------
The design under review before this one confirmed a phrase on ``/dev/tty`` and
tried to establish tool origin from a ContextVar, an environment marker, and a
PID registry checked through process ancestry. An independent review defeated
the ContextVar with a tool-allocated PTY; the registry then fell to:

    rm -rf <registry roots>; unset HERMES_TOOL_ORIGIN;
    env -u HERMES_TOOL_ORIGIN hermes project approve-plan <task>

which approved a plan from inside a model tool call. That is reproduced below
and must now fail for a structural reason rather than a detected one.

THE UNDERLYING FINDING
----------------------
Software running under the same macOS user cannot establish trustworthy human
provenance by process inspection alone. A process can erase any marker it can
read, ``fork`` twice to orphan itself and shed its ancestry, and allocate its own
PTY — leaving nothing that distinguishes it from a human's shell. Approval
authority therefore has to live somewhere the agent holds no credential.
"""

import os
import subprocess
import sys
import time

import pytest

from hermes_cli import approval_broker as ab
from hermes_cli import kanban_db as kb

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERMES_BIN = os.path.join(REPO, ".venv", "bin", "hermes")

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX pty surface")

requires_cli = pytest.mark.skipif(
    not os.path.exists(HERMES_BIN),
    reason="built hermes console script not present in this environment",
)


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb._INITIALIZED_PATHS.clear()
    return tmp_path


def _seed(board, body="GATED-PLAN-BODY"):
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


def _assert_gate_intact(board, tid):
    """Every durable trace an approval would leave, asserted absent."""
    conn = kb.connect(db_path=board / "kanban.db")
    try:
        task = kb.get_task(conn, tid)
        approvals = conn.execute("SELECT COUNT(*) FROM pm_approvals").fetchone()[0]
        plan = conn.execute(
            "SELECT approved_by, approved_at, rejected_at FROM pm_plans"
            " WHERE project_id='p1' AND revision=1").fetchone()
    finally:
        conn.close()
    assert task.status == "scheduled", f"task moved: {task.status}"
    assert kb.gate_state_of.__name__          # sanity: helper imported
    assert approvals == 0, "an approval row was written"
    assert all(v in (None, "") for v in tuple(plan)), "a plan decision was recorded"


def _assert_refused_for_the_right_reason(output):
    """Refused because there is no surface — NOT because something was detected."""
    assert "no separately authenticated approval surface" in output, output
    for heuristic in ("registered", "descendant of", "tool-origin",
                      "ancestry", "controlling terminal"):
        assert heuristic not in output, (
            f"refusal cited a provenance heuristic ({heuristic!r}); the "
            f"forgeable design has returned:\n{output}"
        )


def _run_in_tool_pty(board, command, timeout=60):
    """Run *command* through the real terminal-tool PTY, inside a real tool call.

    Answers any confirmation prompt that appears, so a surface that merely asked
    would be approved.
    """
    from agent.delegation_context import tool_handler_context
    from tools.process_registry import ProcessRegistry

    registry = ProcessRegistry()
    sent = False
    with tool_handler_context():
        session = registry.spawn_local(
            command, use_pty=True,
            env_vars={"HERMES_HOME": str(board),
                      "HERMES_KANBAN_DB": str(board / "kanban.db")},
        )
        assert session._pty is not None, "PTY unavailable; test is not meaningful"
        deadline = time.time() + timeout
        while time.time() < deadline:
            out = getattr(session, "output_buffer", "") or ""
            if not sent and "confirm" in out:
                try:
                    session._pty.write(b"approve\r")
                    sent = True
                except Exception:
                    pass
            if not session._pty.isalive():
                break
            time.sleep(0.15)
        out = getattr(session, "output_buffer", "") or ""
    return out, sent


# ===========================================================================
# The reproduction that retired the previous design
# ===========================================================================


@requires_cli
def test_the_registry_clearing_attack_no_longer_approves(board):
    """The exact command that approved a plan under the previous design."""
    tid = _seed(board)
    roots = f"'{REPO}/.hermes-run' '/tmp/.hermes-tool-origin-{os.getuid()}'"
    command = (
        f"rm -rf {roots}; unset HERMES_TOOL_ORIGIN; "
        f"env -u HERMES_TOOL_ORIGIN HERMES_HOME={board} "
        f"HERMES_KANBAN_DB={board}/kanban.db "
        f"{HERMES_BIN} project approve-plan {tid}"
    )
    output, sent = _run_in_tool_pty(board, command)

    _assert_refused_for_the_right_reason(output)
    assert not sent, "a confirmation prompt was offered to a model tool"
    _assert_gate_intact(board, tid)


@requires_cli
def test_environment_stripping_no_longer_matters(board):
    """`env -i` was the attack on the environment marker. There is no marker."""
    tid = _seed(board)
    command = (
        f"env -i HERMES_HOME={board} HERMES_KANBAN_DB={board}/kanban.db "
        f"{HERMES_BIN} project approve-plan {tid}"
    )
    output, sent = _run_in_tool_pty(board, command)
    _assert_refused_for_the_right_reason(output)
    assert not sent
    _assert_gate_intact(board, tid)


@requires_cli
def test_an_orphaned_process_cannot_approve(board):
    """Orphaning shed the ancestry the previous design relied on.

    A real double-fork with ``setsid`` between: the grandchild is reparented to
    init, so no Hermes process remains anywhere in its ancestry, and it has left
    the session its parent belonged to. Under the ancestry design this was the
    escape. Here it changes nothing, because ancestry is never consulted.

    (``setsid`` is not a binary on macOS, hence ``os.setsid`` in Python.)
    """
    tid = _seed(board)
    marker = board / "orphan.out"
    script = board / "orphan.py"
    script.write_text(
        "import os, sys, subprocess\n"
        "if os.fork():\n"
        "    sys.exit(0)\n"
        "os.setsid()\n"
        "if os.fork():\n"
        "    os._exit(0)\n"
        f"env = dict(os.environ, HERMES_HOME={str(board)!r},\n"
        f"           HERMES_KANBAN_DB={str(board / 'kanban.db')!r})\n"
        f"with open({str(marker)!r}, 'w') as fh:\n"
        f"    subprocess.run([{HERMES_BIN!r}, 'project', 'approve-plan', {tid!r}],\n"
        "                   stdin=subprocess.DEVNULL, stdout=fh, stderr=fh,\n"
        "                   env=env)\n"
        "os._exit(0)\n"
    )
    _run_in_tool_pty(board, f"{sys.executable} {script}", timeout=45)

    deadline = time.time() + 90
    while time.time() < deadline and not (marker.exists() and marker.stat().st_size):
        time.sleep(0.5)
    output = marker.read_text(errors="replace") if marker.exists() else ""
    assert output, "orphaned child produced no output; test is not meaningful"
    _assert_refused_for_the_right_reason(output)
    _assert_gate_intact(board, tid)


@requires_cli
def test_a_model_controlled_pty_cannot_approve(board):
    """The original escape: a real PTY the model allocates and drives."""
    tid = _seed(board)
    output, sent = _run_in_tool_pty(
        board, f"{HERMES_BIN} project approve-plan {tid}")
    _assert_refused_for_the_right_reason(output)
    assert not sent
    _assert_gate_intact(board, tid)


@requires_cli
def test_a_private_pty_allocated_by_the_attacker_cannot_approve(board):
    """Even a PTY Hermes never created gets no approval.

    This is the case no local design could have caught: the attacker builds its
    own terminal, so there is nothing about the terminal to distinguish.
    """
    tid = _seed(board)
    script = board / "own_pty.py"
    script.write_text(
        "import pty, sys\n"
        f"sys.exit(pty.spawn([{HERMES_BIN!r}, 'project', 'approve-plan', {tid!r}]))\n"
    )
    command = (
        f"HERMES_HOME={board} HERMES_KANBAN_DB={board}/kanban.db "
        f"{sys.executable} {script}"
    )
    output, _ = _run_in_tool_pty(board, command)
    _assert_refused_for_the_right_reason(output)
    _assert_gate_intact(board, tid)


@requires_cli
def test_a_plain_subprocess_cannot_approve(board):
    """No PTY at all — the ordinary case, for completeness."""
    tid = _seed(board)
    env = dict(os.environ)
    env.update({"HERMES_HOME": str(board),
                "HERMES_KANBAN_DB": str(board / "kanban.db")})
    proc = subprocess.run(
        [HERMES_BIN, "project", "approve-plan", tid],
        capture_output=True, text=True, env=env, timeout=120,
    )
    _assert_refused_for_the_right_reason(proc.stdout + proc.stderr)
    _assert_gate_intact(board, tid)


# ===========================================================================
# In-process routes
# ===========================================================================


def test_execute_code_child_cannot_approve(board):
    """A code-execution child importing the broker directly."""
    from agent.delegation_context import tool_handler_context
    from tools import code_execution_tool

    tid = _seed(board)
    code = (
        f"import sys; sys.path.insert(0, {REPO!r})\n"
        f"import os; os.environ['HERMES_HOME'] = {str(board)!r}\n"
        f"os.environ['HERMES_KANBAN_DB'] = {str(board / 'kanban.db')!r}\n"
        "from hermes_cli import approval_broker as ab\n"
        "try:\n"
        "    ab.for_plan_decision(project_id='p1', revision=1,\n"
        "                         plan_body='GATED-PLAN-BODY', decision='approved')\n"
        "    print('RESULT approved')\n"
        "except ab.NoApprovalSurfaceError as e:\n"
        "    print('RESULT refused:', str(e).splitlines()[0])\n"
    )
    with tool_handler_context():
        result = code_execution_tool.execute_code(code)
    text = result if isinstance(result, str) else str(result)
    assert "RESULT refused" in text, text
    assert "RESULT approved" not in text
    _assert_gate_intact(board, tid)


@pytest.mark.parametrize("var,val", [
    ("HERMES_DESKTOP_TOKEN", "loopback-token-value"),
    ("HERMES_LOOPBACK_TOKEN", "loopback-token-value"),
    ("HERMES_GATEWAY_TOKEN", "loopback-token-value"),
])
def test_the_loopback_dashboard_token_is_not_authentication(monkeypatch, var, val):
    """Holding a loopback token confers nothing.

    The token is served unauthenticated in ``GET /`` HTML, so any local process
    can read it; it attests that *a process on this machine* called, which is
    exactly what a worker is. Asserted behaviourally — presenting one changes
    nothing — rather than by reading the docstring.
    """
    monkeypatch.setenv(var, val)
    assert ab.resolve_plan_approval_adapter() is None
    with pytest.raises(ab.NoApprovalSurfaceError):
        ab.for_plan_decision(project_id="p1", revision=1, plan_body="b",
                             decision="approved")


def test_no_adapter_exists_to_be_driven():
    """The single fact every test above depends on."""
    assert ab.resolve_plan_approval_adapter() is None
    with pytest.raises(ab.NoApprovalSurfaceError):
        ab.for_plan_decision(project_id="p1", revision=1, plan_body="b",
                             decision="approved")


def test_release_plan_gate_still_requires_a_real_attestation(board):
    """Commit 5's consumer is intact: it is the adapter that is missing."""
    tid = _seed(board)
    conn = kb.connect(db_path=board / "kanban.db")
    try:
        with pytest.raises(Exception):
            kb.release_plan_gate(conn, tid, attestation=None)
    finally:
        conn.close()
    _assert_gate_intact(board, tid)
