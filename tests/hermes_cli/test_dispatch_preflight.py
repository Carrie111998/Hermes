"""Confinement preflight, both dispatch lanes, and truthful launch verification.

SCOPE: this protects against **accidental escape and cooperative execution**,
not arbitrary malicious same-UID activity. A running worker can `chdir`, read
outside its workspace, or write `kanban.db` directly. What is fixed here is the
*launch*, and the *evidence* about the launch.

WHAT COMMIT 7 GOT WRONG — every defect below has a test named after it:

1. The **review lane** never called preflight, so a custom review spawner
   reached the process unchecked.
2. Preflight ran **after `claim_task`**, so a refusal left a `spawn_failed`
   `task_runs` row behind — a direct C1 violation.
3. `observed_cwd` recorded the path *passed to* the spawner, so a spawner that
   ignored it and launched at `/` was still recorded as confined.
4. The audit write swallowed every error, so a "successful confined launch"
   could have no audit record at all.
5. Nothing revalidated the directory between check and use, so a retargeted
   symlink was followed.
"""

import json
import os
import subprocess
import sys
import threading
import time

import pytest

from hermes_cli import dispatch_confinement as dc
from hermes_cli import kanban_db as kb

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERMES_BIN = os.path.join(REPO, ".venv", "bin", "hermes")


def _task(tmp_path, workspace=None, task_id="t_pf", run_id=None, kind="worktree"):
    return kb.Task(
        id=task_id, title="x", body=None, assignee="coder", status="ready",
        priority=0, created_by=None, created_at=0, started_at=None,
        completed_at=None, workspace_kind=kind,
        workspace_path=str(workspace) if workspace else None,
        claim_lock=None, claim_expires=None, tenant=None, branch_name=None,
        current_run_id=run_id,
    )


@pytest.fixture
def dispatchable(tmp_path, monkeypatch):
    """A board whose assignee is a real profile, so dispatch actually spawns.

    Without this the dispatcher buckets the task as ``skipped_nonspawnable``
    and the end-to-end assertions below would silently prove nothing.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kb._INITIALIZED_PATHS.clear()
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _n: True)
    return tmp_path


def _ready_task(conn, tmp_path, name="ws", status="ready"):
    ws = tmp_path / name
    ws.mkdir(exist_ok=True)
    tid = kb.create_task(conn, title="w", assignee="coder",
                         workspace_kind="dir", workspace_path=str(ws))
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
    conn.commit()
    return tid, ws


# ===================== the path predicate ==================================


def test_a_real_directory_is_accepted(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    assert dc.preflight_workspace("t", str(ws)) == str(ws)


@pytest.mark.parametrize("workspace", [None, "", "   ", "relative/path"])
def test_unusable_workspaces_are_refused(workspace):
    with pytest.raises(dc.PreflightRefusal):
        dc.preflight_workspace("t", workspace)


def test_a_file_is_not_a_workspace(tmp_path):
    f = tmp_path / "notadir"
    f.write_text("x")
    with pytest.raises(dc.PreflightRefusal):
        dc.preflight_workspace("t", str(f))


def test_a_missing_directory_passes_the_predicate_but_not_authorization(tmp_path):
    """Provisioning happens between the two, which is why they are separate.

    Commit 7 conflated them: it required the directory to exist, so the
    dispatcher created an arbitrary path first and the check then accepted the
    directory it had just made.
    """
    missing = str(tmp_path / "not-yet")
    assert dc.preflight_workspace("t", missing) == missing
    with pytest.raises(dc.PreflightRefusal):
        dc.authorize_workspace("t", missing)


def test_there_is_no_override():
    """No warn tier, no force flag: a gate that can be waived is not a gate."""
    import inspect
    params = set(inspect.signature(dc.preflight_workspace).parameters)
    assert params == {"task_id", "intended_path"}
    for name in ("force", "warn", "allow_missing", "strict", "override"):
        assert name not in params


# ===================== planning has no side effects ========================


def test_planning_does_not_create_anything(tmp_path, monkeypatch):
    """C1 depends on this: the decision must precede provisioning."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    target = tmp_path / "never-created"
    planned = dc.plan_workspace_path(_task(tmp_path, target, kind="dir"))
    assert planned == str(target)
    assert not target.exists()


def test_a_worktree_without_an_anchor_is_refused(tmp_path, monkeypatch):
    """Rather than guessing an anchor from the dispatcher's own directory."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kb._INITIALIZED_PATHS.clear()
    monkeypatch.setattr(kb, "read_board_metadata", lambda *_a, **_k: {})
    with pytest.raises(dc.PreflightRefusal):
        dc.plan_workspace_path(_task(tmp_path, None, kind="worktree"))


# ===================== identity, not strings ===============================


def test_identity_is_pinned_by_dev_ino(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    auth = dc.authorize_workspace("t", str(ws))
    st = os.stat(ws)
    assert (auth.dev, auth.ino) == (st.st_dev, st.st_ino)


def test_a_symlinked_workspace_resolves_to_its_target(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert dc.authorize_workspace("t", str(link)).path == os.path.realpath(real)


def test_symlink_retargeting_between_check_and_spawn_is_caught(tmp_path):
    """DEFECT 5. The path string is identical; only identity changes."""
    good = tmp_path / "good"
    evil = tmp_path / "evil"
    good.mkdir()
    evil.mkdir()
    link = tmp_path / "ws"
    try:
        link.symlink_to(good)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    authorized = dc.authorize_workspace("t", str(link))
    link.unlink()
    link.symlink_to(evil)          # retargeted after validation

    with pytest.raises(dc.PreflightRefusal):
        dc.revalidate_at_spawn("t", dc.AuthorizedWorkspace(
            path=str(link), dev=authorized.dev, ino=authorized.ino))


def test_directory_replacement_between_check_and_spawn_is_caught(tmp_path):
    """DEFECT 5, without symlinks: the directory itself is swapped."""
    ws = tmp_path / "ws"
    ws.mkdir()
    authorized = dc.authorize_workspace("t", str(ws))
    ws.rmdir()
    ws.mkdir()                     # same path, different inode

    with pytest.raises(dc.PreflightRefusal):
        dc.revalidate_at_spawn("t", authorized)


def test_an_unchanged_directory_revalidates(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    dc.revalidate_at_spawn("t", dc.authorize_workspace("t", str(ws)))


# ===================== truthful observation ================================


def test_the_launch_directory_is_read_from_the_process(tmp_path):
    """Not from the path we asked for — that is the whole distinction."""
    ws = tmp_path / "ws"
    ws.mkdir()
    proc = subprocess.Popen(
        ["/bin/sh", "-c", "read x"], stdin=subprocess.PIPE, cwd=str(ws)
    )
    try:
        observed = dc.observe_process_cwd(proc.pid)
        assert observed is not None, "cannot observe a child; test is meaningless"
        assert os.path.realpath(observed) == os.path.realpath(ws)
    finally:
        proc.stdin.close()
        proc.wait()


def test_a_spawner_that_launches_elsewhere_is_detected(tmp_path):
    """DEFECT 3. The reproduced case: spawner ignores its workspace argument."""
    ws = tmp_path / "ws"
    ws.mkdir()
    authorized = dc.authorize_workspace("t", str(ws))
    proc = subprocess.Popen(
        ["/bin/sh", "-c", "read x"], stdin=subprocess.PIPE, cwd="/"
    )
    try:
        v = dc.verify_launch("t", authorized, proc.pid)
        assert v.status == dc.MISMATCH, v
        assert not v.is_verified
        assert "/" in (v.observed_cwd or "")
    finally:
        proc.stdin.close()
        proc.wait()


def test_a_correct_launch_verifies(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    authorized = dc.authorize_workspace("t", str(ws))
    proc = subprocess.Popen(
        ["/bin/sh", "-c", "read x"], stdin=subprocess.PIPE, cwd=str(ws)
    )
    try:
        v = dc.verify_launch("t", authorized, proc.pid)
        assert v.status == dc.VERIFIED, v
        assert os.path.realpath(v.observed_cwd) == authorized.path
    finally:
        proc.stdin.close()
        proc.wait()


def test_an_unobservable_launch_is_not_called_verified(tmp_path):
    """A legacy spawner returning only a pid must not be labelled confined."""
    ws = tmp_path / "ws"
    ws.mkdir()
    authorized = dc.authorize_workspace("t", str(ws))
    v = dc.verify_launch("t", authorized, 4242, _observer=lambda _pid: None)
    assert v.status == dc.UNOBSERVABLE
    assert v.observed_cwd is None
    assert not v.is_verified


def test_a_spawner_report_can_refuse_but_never_verify(tmp_path):
    """A self-report is telemetry, not evidence.

    THIS TEST PREVIOUSLY ASSERTED THE DEFECT. It required an "honest" report to
    produce VERIFIED, which is precisely what let a spawner that ignored its
    workspace name the authorized path and be believed. Taking a report at its
    word to REFUSE is safe; the reverse is not.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    authorized = dc.authorize_workspace("t", str(ws))

    lying = dc.verify_launch("t", authorized, 1, reported_cwd="/",
                             _observer=lambda _pid: None)
    assert lying.status == dc.MISMATCH

    honest = dc.verify_launch("t", authorized, 1, reported_cwd=str(ws),
                              _observer=lambda _pid: None)
    assert honest.status == dc.REPORTED_ONLY
    assert not honest.is_verified


def test_only_os_observation_can_verify(tmp_path):
    """The one route to VERIFIED."""
    ws = tmp_path / "ws"
    ws.mkdir()
    authorized = dc.authorize_workspace("t", str(ws))
    v = dc.verify_launch("t", authorized, 1, reported_cwd=None,
                         _observer=lambda _pid: str(ws))
    assert v.status == dc.VERIFIED
    assert "OS observation" in v.detail


@pytest.mark.parametrize("value,expected_pid", [
    (None, None), (0, None), (4242, 4242), (True, None),
    (dc.SpawnOutcome(pid=99, observed_cwd="/tmp"), 99),
])
def test_spawn_results_normalize(value, expected_pid):
    pid, _cwd = dc.normalize_spawn_result(value)
    assert pid == expected_pid


def test_structured_results_carry_their_report():
    _pid, cwd = dc.normalize_spawn_result(
        dc.SpawnOutcome(pid=1, observed_cwd="/tmp/x"))
    assert cwd == "/tmp/x"


# ===================== the audit column ====================================


def test_the_column_exists(tmp_path):
    conn = kb.connect(db_path=tmp_path / "k.db")
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(task_runs)")}
        assert "observed_cwd" in cols
    finally:
        conn.close()


def test_recording_and_reading_back(tmp_path):
    conn = kb.connect(db_path=tmp_path / "k.db")
    try:
        tid = kb.create_task(conn, title="w", assignee="coder")
        conn.execute("INSERT INTO task_runs (task_id, status, started_at)"
                     " VALUES (?, 'running', 1)", (tid,))
        run_id = conn.execute("SELECT id FROM task_runs WHERE task_id = ?",
                              (tid,)).fetchone()["id"]
        kb.record_observed_cwd(conn, run_id, "/tmp/somewhere")
        assert conn.execute("SELECT observed_cwd FROM task_runs WHERE id = ?",
                            (run_id,)).fetchone()["observed_cwd"] == "/tmp/somewhere"
    finally:
        conn.close()


@pytest.mark.parametrize("run_id,cwd", [(None, "/tmp/x"), (1, None), (1, ""), (0, "/tmp/x")])
def test_incomplete_audit_values_raise(tmp_path, run_id, cwd):
    """DEFECT 4. This used to be a silent no-op."""
    conn = kb.connect(db_path=tmp_path / "k.db")
    try:
        with pytest.raises((ValueError, RuntimeError)):
            kb.record_observed_cwd(conn, run_id, cwd)
    finally:
        conn.close()


def test_a_missing_run_row_raises(tmp_path):
    conn = kb.connect(db_path=tmp_path / "k.db")
    try:
        with pytest.raises(RuntimeError):
            kb.record_observed_cwd(conn, 987654, "/tmp/x")
    finally:
        conn.close()


def test_a_write_failure_is_not_swallowed():
    """DEFECT 4: a successful launch must never be silently unauditable."""
    class _Broken:
        def execute(self, *a, **k):
            raise RuntimeError("db is gone")

    with pytest.raises(Exception):
        kb.record_observed_cwd(_Broken(), 1, "/tmp/x")


def test_required_confinement_event_failure_is_not_swallowed():
    class _Broken:
        def execute(self, *a, **k):
            raise RuntimeError("event store is gone")

    with pytest.raises(Exception):
        kb._record_confinement_event(
            _Broken(), "t", "confinement_verified", {}, required=True
        )


def test_historical_runs_keep_null_rather_than_a_guess(tmp_path):
    conn = kb.connect(db_path=tmp_path / "k.db")
    try:
        tid = kb.create_task(conn, title="w", assignee="coder")
        conn.execute("INSERT INTO task_runs (task_id, status, started_at)"
                     " VALUES (?, 'done', 1)", (tid,))
        assert conn.execute("SELECT observed_cwd FROM task_runs WHERE task_id = ?",
                            (tid,)).fetchone()["observed_cwd"] is None
    finally:
        conn.close()


def test_a_legacy_db_without_the_column_migrates(tmp_path):
    import sqlite3
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(path)
    raw.executescript(
        "CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " task_id TEXT NOT NULL, status TEXT NOT NULL, started_at INTEGER NOT NULL);")
    raw.commit()
    raw.close()
    conn = kb.connect(db_path=path)
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(task_runs)")}
        assert "observed_cwd" in cols
    finally:
        conn.close()


# ===================== the spawn site ======================================


def test_spawn_refuses_before_creating_any_process(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("subprocess.Popen",
                        lambda *a, **k: calls.append(k) or pytest.fail("spawned"))
    with pytest.raises(dc.PreflightRefusal):
        kb._default_spawn(_task(tmp_path, kind="dir"), str(tmp_path / "missing"))
    assert calls == []


def test_spawn_never_passes_cwd_none(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    seen = {}

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            seen.update(kwargs)
            self.pid = 4242

    monkeypatch.setattr("subprocess.Popen", _FakePopen)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / ".hermes").mkdir(exist_ok=True)
    kb._default_spawn(_task(tmp_path, ws, kind="dir"), str(ws))
    assert seen["cwd"] is not None
    assert seen["cwd"] == os.path.realpath(ws)


# ===================== BOTH LANES, end to end ==============================


@pytest.mark.parametrize("status", ["ready", "review"])
def test_both_lanes_verify_the_launch(dispatchable, monkeypatch, status):
    """DEFECT 1: the review lane used to call the spawner with no checks."""
    tmp_path = dispatchable
    if status == "review":
        monkeypatch.setattr(kb, "review_dispatch_enabled", lambda: True)
    conn = kb.connect(db_path=tmp_path / "kanban.db")
    try:
        tid, ws = _ready_task(conn, tmp_path, status=status)
        procs = []

        def _spawn(task, workspace, **kwargs):
            p = subprocess.Popen(
                ["/bin/sh", "-c", "read x"], stdin=subprocess.PIPE,
                cwd=workspace,
            )
            procs.append(p)
            return p.pid

        result = kb.dispatch_once(conn, spawn_fn=_spawn)
        try:
            assert tid in [i for i, _, _ in result.spawned], (
                f"{status} lane did not dispatch: {result}")
            row = conn.execute(
                "SELECT observed_cwd FROM task_runs WHERE task_id = ?",
                (tid,)).fetchone()
            assert row["observed_cwd"] == os.path.realpath(ws)
        finally:
            for p in procs:
                p.stdin.close()
                p.wait()
    finally:
        conn.close()


@pytest.mark.parametrize("status", ["ready", "review"])
@pytest.mark.live_system_guard_bypass
def test_both_lanes_detect_a_spawner_that_launches_elsewhere(
    dispatchable, monkeypatch, status
):
    """DEFECT 3, end to end: the escaped worker must be killed and refused."""
    tmp_path = dispatchable
    if status == "review":
        monkeypatch.setattr(kb, "review_dispatch_enabled", lambda: True)
    conn = kb.connect(db_path=tmp_path / "kanban.db")
    try:
        tid, ws = _ready_task(conn, tmp_path, status=status)
        escaped = {}

        def _escaping_spawn(task, workspace, **kwargs):
            p = subprocess.Popen(
                ["/bin/sh", "-c", "read x"], stdin=subprocess.PIPE,
                cwd="/", start_new_session=True,
            )
            escaped["proc"] = p
            return p.pid

        result = kb.dispatch_once(conn, spawn_fn=_escaping_spawn)
        assert tid not in [i for i, _, _ in result.spawned], (
            "an escaped launch was reported as a successful spawn")

        # The worker was stopped, not left running outside its workspace.
        proc = escaped.get("proc")
        assert proc is not None
        proc.wait(timeout=10)

        # Nothing claims it was confined.
        rows = conn.execute(
            "SELECT observed_cwd FROM task_runs WHERE task_id = ?", (tid,)).fetchall()
        assert all(r["observed_cwd"] is None for r in rows)
        kinds = [r["kind"] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ?", (tid,))]
        assert "confinement_violation" in kinds, kinds
    finally:
        conn.close()


def test_a_refusal_creates_no_run_row_and_leaves_the_task_dispatchable(
    dispatchable, monkeypatch
):
    """DEFECT 2 / invariant C1.

    Commit 7 refused AFTER claiming, leaving `status=spawn_failed` rows for
    workers that never existed. Nothing may be created by a refusal.
    """
    tmp_path = dispatchable
    conn = kb.connect(db_path=tmp_path / "kanban.db")
    try:
        # A workspace that cannot be planned at all: dir kind, no path.
        tid = kb.create_task(conn, title="w", assignee="coder")
        conn.execute("UPDATE tasks SET status = 'ready', workspace_kind = 'dir',"
                     " workspace_path = NULL WHERE id = ?", (tid,))
        conn.commit()

        def _never(task, workspace, **kwargs):
            pytest.fail("a worker was spawned despite a refused preflight")

        result = kb.dispatch_once(conn, spawn_fn=_never)

        assert tid in result.refused_confinement, result
        runs = conn.execute("SELECT COUNT(*) c FROM task_runs WHERE task_id = ?",
                            (tid,)).fetchone()["c"]
        assert runs == 0, "C1 violated: a refused dispatch created a run row"

        task = kb.get_task(conn, tid)
        assert task.status == "ready", "task must remain dispatchable"
        assert task.claim_lock is None, "task must not stay claimed"
        assert task.current_run_id is None

        kinds = [r["kind"] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ?", (tid,))]
        assert "confinement_refused" in kinds, kinds
    finally:
        conn.close()


def test_sandbox_custom_spawner_is_refused_before_claim(dispatchable, monkeypatch):
    """Accepting env_vars is not proof a custom launcher passes the barrier."""
    from hermes_cli import workspace_policy as wp

    tmp_path = dispatchable
    policy = wp.WorkspacePolicy(
        board="eval", mode=wp.MODE_SANDBOX,
        allowed_roots=(str(tmp_path),), protected_paths=("*/protected*",),
        hermes_home_root=str(tmp_path), required_deny_globs=("*git*push*",),
        prohibited_commands=("git push",), allowed_commands=("git status",),
    )
    monkeypatch.setattr(wp, "resolve_policy", lambda *a, **k: policy)
    conn = kb.connect(db_path=tmp_path / "kanban.db")
    try:
        tid, _ws = _ready_task(conn, tmp_path)
        called = []

        def _custom(task, workspace, **kwargs):
            called.append((task, workspace, kwargs))
            pytest.fail("custom sandbox spawner ran before a trusted handshake")

        result = kb.dispatch_once(conn, spawn_fn=_custom)
        assert tid in result.refused_confinement
        assert called == []
        assert conn.execute(
            "SELECT COUNT(*) c FROM task_runs WHERE task_id = ?", (tid,)
        ).fetchone()["c"] == 0
        assert kb.get_task(conn, tid).status == "ready"
    finally:
        conn.close()


def test_policy_load_failure_refuses_before_claim(dispatchable, monkeypatch):
    tmp_path = dispatchable
    conn = kb.connect(db_path=tmp_path / "policy-load-failure.db")
    try:
        tid, _ws = _ready_task(conn, tmp_path)

        def _boom():
            raise RuntimeError("config unreadable")

        monkeypatch.setattr("hermes_cli.config.load_config", _boom)
        result = kb.dispatch_once(
            conn, spawn_fn=lambda *a, **k: pytest.fail("worker spawned")
        )
        assert tid in result.refused_confinement
        assert conn.execute(
            "SELECT COUNT(*) c FROM task_runs WHERE task_id = ?", (tid,)
        ).fetchone()["c"] == 0
        assert kb.get_task(conn, tid).status == "ready"
    finally:
        conn.close()


def test_an_unobservable_launch_is_reported_not_assumed(dispatchable, monkeypatch):
    """A legacy spawner whose process cannot be inspected."""
    tmp_path = dispatchable
    conn = kb.connect(db_path=tmp_path / "kanban.db")
    try:
        tid, ws = _ready_task(conn, tmp_path)
        monkeypatch.setattr(dc, "observe_process_cwd", lambda _pid: None)

        result = kb.dispatch_once(conn, spawn_fn=lambda t, w, **k: 424242)

        assert tid in result.unverified_launch, result
        row = conn.execute("SELECT observed_cwd FROM task_runs WHERE task_id = ?",
                           (tid,)).fetchone()
        assert row["observed_cwd"] is None, (
            "an unverified launch recorded a directory it never observed")
        kinds = [r["kind"] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ?", (tid,))]
        assert "confinement_unverified" in kinds, kinds
    finally:
        conn.close()


# ===========================================================================
# Ownership, cleanup, and the start barrier
#
# TEST-PROCESS HYGIENE: children here are held open by an stdin PIPE and exit
# when it closes, so ordinary cleanup needs no signals at all and cannot trip
# the repository's live-system guard. Tests that exercise TERMINATION carry the
# conftest's own `live_system_guard_bypass` marker — the supported narrow escape
# for tests that genuinely deliver signals. The guard is not weakened.
# ===========================================================================


import contextlib


@contextlib.contextmanager
def _held_child(cwd, *, own_session=True, spawn_descendant=False):
    """A child that lives until its stdin closes. No signals required."""
    if spawn_descendant:
        script = (
            "import subprocess,sys; "
            "p=subprocess.Popen([sys.executable,'-c','import sys;sys.stdin.read()'],"
            "stdin=subprocess.PIPE); print(p.pid, flush=True); sys.stdin.read()"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, text=True, cwd=str(cwd),
            start_new_session=own_session,
        )
        proc._hermes_test_descendant_pid = int(proc.stdout.readline().strip())
    else:
        proc = subprocess.Popen(
            ["/bin/sh", "-c", "read x"], stdin=subprocess.PIPE, cwd=str(cwd),
            start_new_session=own_session,
        )
    try:
        yield proc
    finally:
        with contextlib.suppress(Exception):
            proc.stdin.close()
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)


def test_ownership_binds_pid_creation_time_and_group(tmp_path):
    with _held_child(tmp_path) as proc:
        owned = dc.own_process(proc.pid)
        assert owned is not None
        assert owned.pid == proc.pid
        assert owned.create_time is not None, "no PID-reuse guard was captured"
        assert owned.has_tree_handle, "no process-group cleanup handle"
        assert owned.pgid == proc.pid, "start_new_session should make it a leader"
        assert owned.is_alive()


def test_pid_reuse_is_detected_by_creation_time(tmp_path):
    """A recycled PID must not be mistaken for the worker we owned."""
    with _held_child(tmp_path) as proc:
        owned = dc.own_process(proc.pid)
        impostor = dc.OwnedProcess(
            pid=owned.pid, create_time=(owned.create_time or 0) + 500.0,
            pgid=owned.pgid,
        )
        assert owned.is_alive()
        assert not impostor.is_alive(), "PID reuse was not detected"


def test_a_short_lived_child_is_not_owned(tmp_path):
    """A process that exits before inspection cannot be bound or verified."""
    proc = subprocess.Popen(["/bin/sh", "-c", "exit 0"], cwd=str(tmp_path))
    proc.wait()
    assert dc.observe_process_cwd(proc.pid) is None
    ws = tmp_path
    authorized = dc.authorize_workspace("t", str(ws))
    v = dc.verify_launch("t", authorized, proc.pid)
    assert v.status in (dc.UNOBSERVABLE, dc.MISMATCH)
    assert not v.is_verified


def test_observation_errors_do_not_become_verification(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    authorized = dc.authorize_workspace("t", str(ws))

    def _boom(_pid):
        raise RuntimeError("psutil exploded")

    v = dc.verify_launch("t", authorized, 1,
                         _observer=lambda p: dc.observe_process_cwd(None))
    assert not v.is_verified
    # And the observer itself swallowing an error yields None, not a pass.
    assert dc.observe_process_cwd(None) is None


@pytest.mark.parametrize("value", [None, 0, False, "nonsense", object()])
def test_malformed_spawn_results_yield_no_pid(value):
    pid, _cwd = dc.normalize_spawn_result(value)
    assert pid is None


@pytest.mark.live_system_guard_bypass
def test_termination_takes_the_whole_tree_not_just_the_leader(tmp_path):
    """A benign descendant must not survive its leader.

    Uses the conftest's supported marker because delivering real signals IS the
    behaviour under test.
    """
    with _held_child(tmp_path, spawn_descendant=True) as proc:
        time.sleep(0.2)
        owned = dc.own_process(proc.pid)
        descendant_pid = proc._hermes_test_descendant_pid
        assert dc._process_matches(descendant_pid, None), (
            "the announced descendant is not alive; test proves nothing")

        result = dc.terminate_worker_tree(owned)
        assert result.ok, result.detail
        assert not owned.is_alive()
        deadline = time.time() + 3
        while time.time() < deadline and dc._process_matches(descendant_pid, None):
            time.sleep(0.05)
        assert not dc._process_matches(descendant_pid, None), (
            "the worker descendant survived process-group termination")


@pytest.mark.live_system_guard_bypass
def test_termination_never_signals_the_dispatchers_own_group(tmp_path, monkeypatch):
    """Killing our own process group would take the dispatcher down with it."""
    own_group = os.getpgid(0)
    fake = dc.OwnedProcess(pid=os.getpid(), create_time=None, pgid=own_group)
    killed = []
    monkeypatch.setattr(os, "killpg", lambda pg, sig: killed.append(pg))
    monkeypatch.setattr(os, "kill", lambda *a, **k: None)
    monkeypatch.setattr(dc, "_process_matches", lambda *a, **k: False)
    dc.terminate_worker_tree(fake)
    assert own_group not in killed


def test_the_barrier_holds_until_released(tmp_path):
    barrier = dc.StartBarrier.create(str(tmp_path), "t1")
    result = []
    worker = threading.Thread(target=lambda: result.append(
        dc.wait_for_start_barrier(timeout_seconds=3, _env=barrier.env())))
    worker.start()
    assert barrier.wait_until_waiting(os.getpid(), timeout_seconds=2)
    assert result == []
    barrier.release()
    worker.join(timeout=3)
    assert result == [True]
    assert not os.path.exists(barrier.path)


def test_an_aborted_barrier_never_releases(tmp_path):
    barrier = dc.StartBarrier.create(str(tmp_path), "t1")
    result = []
    worker = threading.Thread(target=lambda: result.append(
        dc.wait_for_start_barrier(timeout_seconds=10, _env=barrier.env())))
    worker.start()
    assert barrier.wait_until_waiting(os.getpid(), timeout_seconds=2)
    barrier.abort()
    worker.join(timeout=2)
    assert result == [False]


def test_a_stale_success_cannot_release_a_later_attempt(tmp_path):
    first = dc.StartBarrier.create(str(tmp_path), "same-task")
    first.release()  # Simulate a crash before the worker consumed success.
    second = dc.StartBarrier.create(str(tmp_path), "same-task")
    assert first.path != second.path

    result = []
    worker = threading.Thread(target=lambda: result.append(
        dc.wait_for_start_barrier(timeout_seconds=3, _env=second.env())))
    worker.start()
    assert second.wait_until_waiting(os.getpid(), timeout_seconds=2)
    time.sleep(0.2)
    assert result == [], "the second attempt consumed the first attempt's GO"
    second.release()
    worker.join(timeout=3)
    assert result == [True]
    first.abort()


def test_barrier_acknowledgement_is_bound_to_the_worker_pid(tmp_path):
    barrier = dc.StartBarrier.create(str(tmp_path), "t1")
    with open(barrier.waiting_path, "w", encoding="utf-8") as fh:
        import json
        json.dump({"state": "WAITING", "token": barrier.token,
                   "pid": os.getpid() + 1000}, fh)
    assert not barrier.wait_until_waiting(os.getpid(), timeout_seconds=1)
    barrier.abort()


def test_partial_or_malformed_barrier_configuration_refuses(tmp_path):
    assert dc.wait_for_start_barrier(
        timeout_seconds=1, _env={dc.START_BARRIER_ENV: str(tmp_path / "x")}
    ) is False
    assert dc.wait_for_start_barrier(
        timeout_seconds=1,
        _env={dc.START_BARRIER_ENV: str(tmp_path / "missing"),
              dc.START_BARRIER_TOKEN_ENV: "wrong"},
    ) is False


def test_requested_barrier_evaluation_error_exits_nonzero(monkeypatch):
    import importlib

    main_mod = importlib.import_module("hermes_cli.main")
    monkeypatch.setenv(dc.START_BARRIER_ENV, "/unreadable/barrier")
    monkeypatch.setenv(dc.START_BARRIER_TOKEN_ENV, "token")

    def _boom():
        raise RuntimeError("barrier evaluator failed")

    monkeypatch.setattr(dc, "wait_for_start_barrier", _boom)
    with pytest.raises(SystemExit) as exc:
        main_mod.main()
    assert exc.value.code == 70


@pytest.mark.live_system_guard_bypass
def test_release_failure_terminates_the_acknowledged_worker(
    dispatchable, monkeypatch
):
    """GO-write failure stays inside the owned cleanup boundary."""
    from hermes_cli import workspace_policy as wp

    tmp_path = dispatchable
    ws = tmp_path / "ws-release-failure"
    ws.mkdir()
    conn = kb.connect(db_path=tmp_path / "release-failure.db")
    processes = []
    try:
        tid = kb.create_task(
            conn, title="w", assignee="coder", workspace_kind="dir",
            workspace_path=str(ws),
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        conn.commit()
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None

        policy = wp.WorkspacePolicy(
            board="eval", mode=wp.MODE_SANDBOX,
            allowed_roots=(str(tmp_path),), protected_paths=("*/protected*",),
            hermes_home_root=str(tmp_path),
            required_deny_globs=("*git*push*",),
            prohibited_commands=("git push",), allowed_commands=("git status",),
        )
        report = wp.PolicyReport(board="eval", mode=wp.MODE_SANDBOX)
        for assertion_id in range(1, 21):
            report.record(assertion_id, wp.PASS)

        monkeypatch.setattr(wp, "resolve_policy", lambda *a, **k: policy)
        monkeypatch.setattr(wp, "pin_allowed_roots", lambda *a, **k: {})
        monkeypatch.setattr(wp, "revalidate_allowed_roots", lambda *a, **k: None)
        monkeypatch.setattr(wp, "enforce_final", lambda *a, **k: report)

        def _shipped(task, workspace, *, board=None, env_vars=None):
            env = dict(os.environ)
            env.update(env_vars or {})
            script = (
                "from hermes_cli.dispatch_confinement import "
                "wait_for_start_barrier; "
                "raise SystemExit(0 if wait_for_start_barrier(timeout_seconds=10) "
                "else 70)"
            )
            proc = subprocess.Popen(
                [sys.executable, "-c", script], cwd=workspace, env=env,
                start_new_session=True,
            )
            processes.append(proc)
            return proc.pid

        monkeypatch.setattr(kb, "_default_spawn", _shipped)

        def _release_error(self):
            raise OSError("cannot write GO")

        monkeypatch.setattr(dc.StartBarrier, "release", _release_error)
        with pytest.raises(OSError, match="cannot write GO"):
            kb._spawn_verified(
                conn, claimed, str(ws), board="eval", spawn_fn=None,
                result=kb.DispatchResult(),
            )
        assert processes
        processes[0].wait(timeout=10)
        assert processes[0].returncode is not None
        kinds = [row["kind"] for row in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ?", (tid,)
        )]
        assert "confinement_violation" in kinds
    finally:
        for proc in processes:
            if proc.poll() is None:
                with contextlib.suppress(Exception):
                    proc.kill()
                    proc.wait(timeout=3)
        conn.close()


@pytest.mark.live_system_guard_bypass
def test_real_board_database_does_not_block_a_compliant_sandbox_dispatch(
    tmp_path, monkeypatch
):
    """Assertion 8 must be reachable with Hermes' own live kanban.db present."""
    from hermes_cli import workspace_policy as wp

    home = tmp_path / "hermes-home"
    home.mkdir()
    fixture = tmp_path / "fixture"
    subprocess.run(["git", "init", "-q", str(fixture)], check=True)
    (fixture / "src.txt").write_text("ordinary task-id and group_id content")
    subprocess.run(["git", "-C", str(fixture), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(fixture), "-c", "user.email=t@t",
         "-c", "user.name=t", "commit", "-qm", "fixture"],
        check=True,
    )
    wp.build_fixture_attestation(str(fixture), build_source="test")

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    kb._INITIALIZED_PATHS.clear()
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _n: True)

    policy = wp.WorkspacePolicy(
        board="default", mode=wp.MODE_SANDBOX,
        allowed_roots=(str(tmp_path),), protected_paths=("*/protected*",),
        hermes_home_root=str(home),
        required_deny_globs=("*git*push*", "*vercel*"),
        prohibited_commands=("git push origin main", "vercel --prod"),
        allowed_commands=("npm test", "git status"),
    )
    config = {
        "approvals": {
            "single_query_mode": "deny",
            "deny": ["*git*push*", "*vercel*"],
        }
    }
    monkeypatch.setattr(wp, "resolve_policy", lambda *a, **k: policy)
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly", lambda *a, **k: config
    )

    processes = []

    def _shipped(task, workspace, *, board=None, env_vars=None):
        env = dict(os.environ)
        env.update(env_vars or {})
        script = (
            "from hermes_cli.dispatch_confinement import "
            "wait_for_start_barrier; "
            "raise SystemExit(0 if wait_for_start_barrier(timeout_seconds=20) "
            "else 70)"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script], cwd=workspace, env=env,
            start_new_session=True,
        )
        processes.append(proc)
        return proc.pid

    monkeypatch.setattr(kb, "_default_spawn", _shipped)
    conn = kb.connect(db_path=home / "kanban.db")
    try:
        tid = kb.create_task(
            conn, title="ordinary task-id work", assignee="coder",
            workspace_kind="dir", workspace_path=str(fixture),
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        conn.commit()

        result = kb.dispatch_once(conn, spawn_fn=None)
        assert tid in [task_id for task_id, _assignee, _path in result.spawned], result
        assert result.refused_confinement == []

        run = conn.execute(
            "SELECT observed_cwd FROM task_runs WHERE task_id = ?", (tid,)
        ).fetchone()
        assert run["observed_cwd"] == os.path.realpath(fixture)
        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'confinement_verified' ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()
        assert event is not None
        assert json.loads(event["payload"])["contract_satisfied"] is True

        assert processes
        processes[0].wait(timeout=10)
        assert processes[0].returncode == 0
    finally:
        for proc in processes:
            if proc.poll() is None:
                with contextlib.suppress(Exception):
                    proc.kill()
                    proc.wait(timeout=3)
        conn.close()


def test_no_barrier_configured_means_no_wait():
    """Ordinary CLI use and open-mode boards are unaffected."""
    assert dc.wait_for_start_barrier(timeout_seconds=1, _env={}) is True


def test_no_work_happens_before_the_barrier_is_released(tmp_path):
    """The marker test: work must not begin, not merely be cut short.

    A real `hermes` process is launched with a barrier set and given time to do
    something. It must produce nothing until released.
    """
    hermes = HERMES_BIN
    if not os.path.exists(hermes):
        pytest.skip("built hermes console script not present")

    barrier = dc.StartBarrier.create(str(tmp_path), "t1")
    marker = tmp_path / "worker-ran.txt"
    env = dict(os.environ)
    env.update(barrier.env())
    env["HERMES_HOME"] = str(tmp_path)

    proc = subprocess.Popen(
        [hermes, "project", "--help"], env=env,
        stdout=open(marker, "w"), stderr=subprocess.STDOUT,
    )
    try:
        assert barrier.wait_until_waiting(proc.pid, timeout_seconds=30), (
            "real hermes worker never acknowledged waiting")
        time.sleep(0.5)
        assert proc.poll() is None, "worker ran before the barrier was released"
        assert marker.stat().st_size == 0, "worker produced output before release"

        barrier.release()
        proc.wait(timeout=90)
        assert marker.stat().st_size > 0, "worker never ran after release"
    finally:
        if proc.poll() is None:
            proc.stdin = None
            with contextlib.suppress(Exception):
                proc.wait(timeout=5)
