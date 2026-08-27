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

import os
import subprocess

import pytest

from hermes_cli import dispatch_confinement as dc
from hermes_cli import kanban_db as kb


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
    proc = subprocess.Popen(["/bin/sh", "-c", "sleep 3"], cwd=str(ws))
    try:
        observed = dc.observe_process_cwd(proc.pid)
        assert observed is not None, "cannot observe a child; test is meaningless"
        assert os.path.realpath(observed) == os.path.realpath(ws)
    finally:
        proc.kill()
        proc.wait()


def test_a_spawner_that_launches_elsewhere_is_detected(tmp_path):
    """DEFECT 3. The reproduced case: spawner ignores its workspace argument."""
    ws = tmp_path / "ws"
    ws.mkdir()
    authorized = dc.authorize_workspace("t", str(ws))
    proc = subprocess.Popen(["/bin/sh", "-c", "sleep 3"], cwd="/")
    try:
        v = dc.verify_launch("t", authorized, proc.pid)
        assert v.status == dc.MISMATCH, v
        assert not v.is_verified
        assert "/" in (v.observed_cwd or "")
    finally:
        proc.kill()
        proc.wait()


def test_a_correct_launch_verifies(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    authorized = dc.authorize_workspace("t", str(ws))
    proc = subprocess.Popen(["/bin/sh", "-c", "sleep 3"], cwd=str(ws))
    try:
        v = dc.verify_launch("t", authorized, proc.pid)
        assert v.status == dc.VERIFIED, v
        assert os.path.realpath(v.observed_cwd) == authorized.path
    finally:
        proc.kill()
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


def test_a_spawner_report_is_cross_checked_not_trusted(tmp_path):
    """A spawner may report where it launched; it is still judged on identity."""
    ws = tmp_path / "ws"
    ws.mkdir()
    authorized = dc.authorize_workspace("t", str(ws))

    lying = dc.verify_launch("t", authorized, 1, reported_cwd="/",
                             _observer=lambda _pid: None)
    assert lying.status == dc.MISMATCH

    honest = dc.verify_launch("t", authorized, 1, reported_cwd=str(ws),
                              _observer=lambda _pid: None)
    assert honest.status == dc.VERIFIED


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
            p = subprocess.Popen(["/bin/sh", "-c", "sleep 5"], cwd=workspace)
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
                p.kill()
                p.wait()
    finally:
        conn.close()


@pytest.mark.parametrize("status", ["ready", "review"])
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
            p = subprocess.Popen(["/bin/sh", "-c", "sleep 30"], cwd="/")
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
