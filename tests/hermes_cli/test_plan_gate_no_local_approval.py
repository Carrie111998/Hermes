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
import textwrap
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
        gate_state = kb.gate_state_of(conn, tid)
        approvals = conn.execute("SELECT COUNT(*) FROM pm_approvals").fetchone()[0]
        plan = conn.execute(
            "SELECT approved_by, approved_at, rejected_at FROM pm_plans"
            " WHERE project_id='p1' AND revision=1").fetchone()
    finally:
        conn.close()
    assert task.status == "scheduled", f"task moved: {task.status}"
    # Read the gate itself, not a proxy for it. Asserting only on status would
    # pass a partial clear that dropped ``gate_state`` while leaving the
    # scheduled row otherwise untouched — today's release path happens to move
    # status too, but that is a property of the current implementation, not the
    # invariant this test claims to hold.
    assert gate_state == "plan", f"gate_state is {gate_state!r}, not 'plan'"
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


def _assert_process_gone(pid, timeout=15.0):
    """The orphan was reparented to init, so it cannot be waited on — poll it.

    Uses psutil rather than ``os.kill(pid, 0)`` so the check carries no
    platform-specific signal semantics of its own.
    """
    import psutil

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            proc = psutil.Process(pid)
            # A zombie is finished work waiting to be reaped by init; the
            # attacking process is no longer running either way.
            if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                return
        except psutil.NoSuchProcess:
            return
        time.sleep(0.05)
    raise AssertionError(
        f"the orphaned grandchild (pid {pid}) was still running {timeout}s "
        f"after publishing its result"
    )


# The grandchild's own ceiling on the CLI. Must stay BELOW the parent's
# marker deadline so a hung CLI is killed and reaped by the process that
# spawned it, before the parent gives up and starts cleaning up itself.
_ORPHAN_CLI_TIMEOUT = 60
_ORPHAN_MARKER_DEADLINE = 90


def _reap_orphan(pidfile, scratch):
    """Teardown that must run even when the test fails.

    The orphan is reparented to init, so pytest can neither wait on it nor
    reach it through process-group membership. If the CLI hangs, the
    double-forked process and its children would otherwise outlive the whole
    test session: the mutation probe that proved this test fails on an
    unfinished CLI had to be cleaned up by hand, which is the hole this closes.

    Best-effort by construction — it runs in a ``finally`` and must never
    replace the real failure with one of its own.
    """
    import psutil

    pid = created = None
    try:
        pid_text, created_text = pidfile.read_text().split()
        pid, created = int(pid_text), float(created_text)
    except Exception:
        pid = None

    if pid is not None:
        try:
            proc = psutil.Process(pid)
            # PID-reuse guard — the same identity check production uses in
            # OwnedProcess. A recycled pid must never be signalled.
            if abs(proc.create_time() - created) <= 0.05:
                # Descendants first: killing the leader can reparent them and
                # lose the relationship.
                victims = proc.children(recursive=True) + [proc]
                for victim in victims:
                    try:
                        victim.terminate()
                    except Exception:
                        pass
                _, alive = psutil.wait_procs(victims, timeout=5)
                for victim in alive:
                    try:
                        victim.kill()
                    except Exception:
                        pass
                psutil.wait_procs(alive, timeout=5)
        except Exception:
            # Broad on purpose. This runs in a ``finally`` after a test that
            # may already be failing, and the repository's own live-system
            # guard raises a bare RuntimeError (not a psutil error) for any
            # signal aimed outside the test subtree. Cleanup must never
            # replace the real failure with an error of its own.
            pass

    try:
        scratch.unlink()
    except FileNotFoundError:
        pass


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


# The orphan is deliberately OUTSIDE the test process subtree — that is the
# whole point of the double fork — so the conftest live-system guard refuses
# any signal aimed at it. Terminating it in cleanup therefore needs the
# conftest's own `live_system_guard_bypass` marker, the supported narrow
# escape already carried by the confinement suite's termination tests
# (tests/hermes_cli/test_dispatch_preflight.py). The guard is not weakened:
# the bypass is scoped to this one test, and the kanban write guard — the one
# that matters for a gate test — stays armed.
@requires_cli
@pytest.mark.live_system_guard_bypass
def test_an_orphaned_process_cannot_approve(board):
    """Orphaning shed the ancestry the previous design relied on.

    A real double-fork with ``setsid`` between: the grandchild is reparented to
    init, so no Hermes process remains anywhere in its ancestry, and it has left
    the session its parent belonged to. Under the ancestry design this was the
    escape. Here it changes nothing, because ancestry is never consulted.

    (``setsid`` is not a binary on macOS, hence ``os.setsid`` in Python.)

    The grandchild publishes its result ATOMICALLY. It writes the CLI's whole
    output to a scratch file, closes that file only after ``subprocess.run``
    has returned, and only then renames it onto ``marker`` — a rename within
    one directory is atomic on POSIX, so ``marker`` never exists half-written.
    Waiting on "the marker is non-empty" instead races the CLI's own writes: it
    prints the plan header before the refusal, so a poll landing between the
    two reads a header with no refusal and fails on a partial read rather than
    on behaviour. The rename also makes an unfinished CLI *fail* this test
    rather than silently weaken it — no marker is ever published.
    """
    tid = _seed(board)
    marker = board / "orphan.out"
    scratch = board / "orphan.out.partial"
    pidfile = board / "orphan.pid"
    script = board / "orphan.py"
    script.write_text(textwrap.dedent(f"""
        import os, sys, subprocess
        if os.fork():
            sys.exit(0)
        os.setsid()
        if os.fork():
            os._exit(0)
        import psutil
        me = psutil.Process()
        # The orphan cannot be waited on, so it names itself — pid plus start
        # time, so the parent's cleanup can tell it from a recycled pid.
        with open({str(pidfile)!r}, "w") as pf:
            pf.write("%d %.6f" % (os.getpid(), me.create_time()))
        env = dict(os.environ,
                   HERMES_HOME={str(board)!r},
                   HERMES_KANBAN_DB={str(board / 'kanban.db')!r})
        fh = open({str(scratch)!r}, "w")
        try:
            subprocess.run([{HERMES_BIN!r}, "project", "approve-plan", {tid!r}],
                           stdin=subprocess.DEVNULL, stdout=fh, stderr=fh,
                           env=env, timeout={_ORPHAN_CLI_TIMEOUT})
        except Exception:
            # A hung or failed CLI must NEVER publish a marker. Reap whatever
            # it left running before leaving, so nothing outlives this process.
            kids = me.children(recursive=True)
            for kid in kids:
                try:
                    kid.kill()
                except Exception:
                    pass
            psutil.wait_procs(kids, timeout=5)
            fh.close()
            os._exit(1)
        fh.flush()
        os.fsync(fh.fileno())
        fh.close()
        os.rename({str(scratch)!r}, {str(marker)!r})
        os._exit(0)
    """))
    _run_in_tool_pty(board, f"{sys.executable} {script}", timeout=45)

    try:
        deadline = time.time() + _ORPHAN_MARKER_DEADLINE
        while time.time() < deadline and not marker.exists():
            time.sleep(0.1)
        assert marker.exists(), (
            f"the orphaned grandchild never published a complete result within "
            f"{_ORPHAN_MARKER_DEADLINE}s — the CLI did not return, so this test "
            f"proved nothing about the gate"
        )
        output = marker.read_text(errors="replace")
        assert output, "orphaned child produced no output; test is not meaningful"
        _assert_refused_for_the_right_reason(output)
        _assert_gate_intact(board, tid)

        # On the success path the orphan tears itself down; assert that rather
        # than leaning on the cleanup below to hide a process that lingered.
        assert not scratch.exists(), "a partial result file was left behind"
        _assert_process_gone(int(pidfile.read_text().split()[0]))
    finally:
        # Runs on every path, including the ones that never reach the
        # assertions above. A failing security test must not contaminate the
        # rest of the session with a live orphan or its CLI child.
        _reap_orphan(pidfile, scratch)


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
