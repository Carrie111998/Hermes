"""Dispatch preflight guard and ``task_runs.observed_cwd``.

WHAT THIS IS: a workflow-safety feature. It decides where a worker starts, and
records where it started. It is **not** a security boundary against arbitrary
same-user execution — a running worker can change directory, read outside its
workspace, and ignore every convention established here. What it fixes is the
*launch*: a worker starting somewhere nobody chose.

THE FAILURE IT EXISTS FOR
``_default_spawn`` passed ``cwd=workspace if os.path.isdir(workspace) else None``
to ``Popen``, and ``cwd=None`` means "inherit the parent's directory". A task
whose workspace was missing, relative, or empty therefore launched its worker in
the *dispatcher's* directory — silently, with nothing recorded.

During M2a that is exactly what happened: a worker launched from the wrong
directory did not find its test command, searched the filesystem, found a live
production checkout whose default branch auto-deploys, and ran a command there.
No damage, verified — and luck. Path denials were added and the issue declared
closed; a later run still *started* in the wrong place, because denials bound the
blast radius without fixing the launch.
"""

import os

import pytest

from hermes_cli import kanban_db as kb


def _task(tmp_path, workspace=None, task_id="t_pf", run_id=None):
    return kb.Task(
        id=task_id,
        title="x",
        body=None,
        assignee="coder",
        status="ready",
        priority=0,
        created_by=None,
        created_at=0,
        started_at=None,
        completed_at=None,
        workspace_kind="worktree",
        workspace_path=str(workspace) if workspace else None,
        claim_lock=None,
        claim_expires=None,
        tenant=None,
        branch_name=None,
        current_run_id=run_id,
    )


@pytest.fixture
def dispatchable(tmp_path, monkeypatch):
    """A board whose assignee is a real profile, so dispatch actually spawns.

    Without this the dispatcher buckets the task as ``skipped_nonspawnable``
    (``profile_exists`` is false for an invented assignee) and the end-to-end
    assertions below would silently prove nothing.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kb._INITIALIZED_PATHS.clear()
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _n: True)
    return tmp_path


# ===================== the guard ===========================================


def test_a_real_directory_is_accepted(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    assert kb.dispatch_preflight(_task(tmp_path, ws), str(ws)) == os.path.realpath(ws)


@pytest.mark.parametrize("workspace,why", [
    (None, "None"),
    ("", "empty"),
    ("   ", "whitespace"),
    ("relative/path", "relative"),
])
def test_unusable_workspaces_are_refused(tmp_path, workspace, why):
    with pytest.raises(kb.DispatchPreflightError):
        kb.dispatch_preflight(_task(tmp_path), workspace)


def test_a_missing_directory_is_refused(tmp_path):
    """The M2a case: the path is fine, the directory just is not there."""
    with pytest.raises(kb.DispatchPreflightError) as exc:
        kb.dispatch_preflight(_task(tmp_path), str(tmp_path / "gone"))
    assert "refusing to launch" in str(exc.value)


def test_a_file_is_not_a_workspace(tmp_path):
    f = tmp_path / "notadir"
    f.write_text("x")
    with pytest.raises(kb.DispatchPreflightError):
        kb.dispatch_preflight(_task(tmp_path), str(f))


def test_the_resolved_path_is_a_realpath(tmp_path):
    """An audit trail that needs interpreting is not much of an audit trail."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert kb.dispatch_preflight(_task(tmp_path), str(link)) == os.path.realpath(real)


def test_there_is_no_override(tmp_path):
    """No warn tier, no force flag: a gate that can be waived is not a gate."""
    import inspect
    params = set(inspect.signature(kb.dispatch_preflight).parameters)
    assert params == {"task", "workspace"}
    for name in ("force", "warn", "allow_missing", "strict"):
        assert name not in params


# ===================== the spawn cannot skip it ============================


def test_spawn_refuses_before_creating_any_process(tmp_path, monkeypatch):
    """The check must run BEFORE Popen, not alongside it."""
    calls = []
    monkeypatch.setattr("subprocess.Popen",
                        lambda *a, **k: calls.append(k) or pytest.fail("spawned"))
    with pytest.raises(kb.DispatchPreflightError):
        kb._default_spawn(_task(tmp_path), str(tmp_path / "missing"))
    assert calls == []


def test_spawn_never_passes_cwd_none(tmp_path, monkeypatch):
    """``cwd=None`` is the inherit-the-dispatcher's-directory case."""
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
    kb._default_spawn(_task(tmp_path, ws), str(ws))

    assert seen["cwd"] is not None
    assert seen["cwd"] == os.path.realpath(ws)


# ===================== observed_cwd ========================================


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
        conn.execute(
            "INSERT INTO task_runs (task_id, status, started_at)"
            " VALUES (?, 'running', 1)", (tid,))
        run_id = conn.execute(
            "SELECT id FROM task_runs WHERE task_id = ?", (tid,)).fetchone()["id"]

        kb.record_observed_cwd(conn, run_id, "/tmp/somewhere")
        row = conn.execute(
            "SELECT observed_cwd FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        assert row["observed_cwd"] == "/tmp/somewhere"
    finally:
        conn.close()


@pytest.mark.parametrize("run_id,cwd", [
    (None, "/tmp/x"),
    (1, None),
    (1, ""),
    (0, "/tmp/x"),
])
def test_recording_is_a_no_op_without_both_values(tmp_path, run_id, cwd):
    """A missing audit value must never take down a dispatch that succeeded."""
    conn = kb.connect(db_path=tmp_path / "k.db")
    try:
        kb.record_observed_cwd(conn, run_id, cwd)   # must not raise
    finally:
        conn.close()


def test_recording_survives_a_bad_connection():
    """Best-effort: an audit write cannot break a spawn that already worked."""
    class _Broken:
        def execute(self, *a, **k):
            raise RuntimeError("db is gone")

    kb.record_observed_cwd(_Broken(), 1, "/tmp/x")   # must not raise


def test_historical_runs_keep_null_rather_than_a_guess(tmp_path):
    """The column's purpose is that the directory is recorded, not inferred."""
    conn = kb.connect(db_path=tmp_path / "k.db")
    try:
        tid = kb.create_task(conn, title="w", assignee="coder")
        conn.execute(
            "INSERT INTO task_runs (task_id, status, started_at)"
            " VALUES (?, 'done', 1)", (tid,))
        row = conn.execute(
            "SELECT observed_cwd FROM task_runs WHERE task_id = ?", (tid,)).fetchone()
        assert row["observed_cwd"] is None
    finally:
        conn.close()


def test_a_legacy_db_without_the_column_migrates(tmp_path):
    """Opening an old board must add the column, not fail."""
    import sqlite3

    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(path)
    raw.executescript(
        "CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " task_id TEXT NOT NULL, status TEXT NOT NULL, started_at INTEGER NOT NULL);"
    )
    raw.commit()
    raw.close()

    conn = kb.connect(db_path=path)
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(task_runs)")}
        assert "observed_cwd" in cols
    finally:
        conn.close()


# ===================== the dispatcher records it ===========================


def test_dispatch_records_where_the_worker_started(dispatchable):
    """End to end through the real dispatch path, with a stub spawn."""
    tmp_path = dispatchable
    conn = kb.connect(db_path=tmp_path / "kanban.db")
    try:
        ws = tmp_path / "ws"
        ws.mkdir()
        tid = kb.create_task(conn, title="w", assignee="coder",
                             workspace_kind="dir", workspace_path=str(ws))
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        conn.commit()

        spawned = {}

        def _stub_spawn(task, workspace, **kwargs):
            spawned["workspace"] = workspace
            return 9999

        result = kb.dispatch_once(conn, spawn_fn=_stub_spawn)
        assert tid in [t_id for t_id, _, _ in result.spawned], (
            f"task was not dispatched, so this proves nothing: {result}")

        row = conn.execute(
            "SELECT observed_cwd FROM task_runs WHERE task_id = ?", (tid,)).fetchone()
        assert row is not None
        assert spawned.get("workspace") == os.path.realpath(ws)
        assert row["observed_cwd"] == os.path.realpath(ws)
    finally:
        conn.close()


def test_a_refused_dispatch_is_recorded_as_a_spawn_failure(dispatchable, monkeypatch):
    """A refusal must not crash the daemon, and must not reach the spawn.

    The workspace is forced to fail the guard rather than being left missing on
    disk: the dispatcher PROVISIONS the workspace directory before spawning, so
    a task pointed at a non-existent path simply gets one created. (That is
    worth knowing — it means the guard's real value is catching the cases
    provisioning cannot fix: an empty, relative, or non-directory workspace.)
    """
    tmp_path = dispatchable
    conn = kb.connect(db_path=tmp_path / "kanban.db")
    try:
        ws = tmp_path / "ws"
        ws.mkdir()
        tid = kb.create_task(conn, title="w", assignee="coder",
                             workspace_kind="dir", workspace_path=str(ws))
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        conn.commit()

        def _refuse(task, workspace):
            raise kb.DispatchPreflightError("forced refusal")

        monkeypatch.setattr(kb, "dispatch_preflight", _refuse)

        def _never(task, workspace, **kwargs):
            pytest.fail("a worker was spawned despite a refused preflight")

        result = kb.dispatch_once(conn, spawn_fn=_never)   # must not raise
        assert tid not in [t_id for t_id, _, _ in result.spawned]

        # Audited, not silent, and no run left claiming to be running.
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert row["status"] != "running"
        runs = conn.execute(
            "SELECT status, outcome, observed_cwd FROM task_runs"
            " WHERE task_id = ?", (tid,)).fetchall()
        assert all(r["observed_cwd"] is None for r in runs), (
            "a refused dispatch recorded a launch directory")
    finally:
        conn.close()
