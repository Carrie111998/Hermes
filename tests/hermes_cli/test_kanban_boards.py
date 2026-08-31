"""Tests for the multi-board kanban layer (``hermes kanban boards …``).

Covers the pieces added when boards became a first-class concept:

* Slug validation and normalisation.
* Path resolution for ``default`` (legacy ``<root>/kanban.db``) vs
  named boards (``<root>/kanban/boards/<slug>/kanban.db``).
* Current-board persistence via ``<root>/kanban/current`` and
  ``HERMES_KANBAN_BOARD`` env var.
* ``connect(board=)`` isolation — writes on one board don't leak.
* ``create_board`` / ``list_boards`` / ``remove_board`` round trip.
* CLI surface: ``hermes kanban boards list/create/switch/rm``.
* ``_default_spawn`` injects ``HERMES_KANBAN_BOARD`` into worker env.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

# Ensure the worktree (not the stale global clone) is first on sys.path.
_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_containment as kc


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with no prior kanban state.

    The autouse hermetic conftest already nukes credentials + TZ; this
    fixture layers a per-test HERMES_HOME plus a path-init cache reset
    so each test sees a truly empty board set.
    """
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
    ):
        monkeypatch.delenv(var, raising=False)
    # Also reset hermes_constants cache so get_default_hermes_root() re-reads.
    try:
        import hermes_constants
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    # Kanban module-level init cache must not leak between tests.
    kb._INITIALIZED_PATHS.clear()
    return home


# ---------------------------------------------------------------------------
# Slug validation
# ---------------------------------------------------------------------------

class TestSlugValidation:
    @pytest.mark.parametrize("good", [
        "default", "atm10-server", "hermes-agent", "proj_1", "a",
        "very-long-but-still-ok-slug-with-hyphens-and-numbers-1234",
    ])
    def test_accepts_valid(self, good):
        assert kb._normalize_board_slug(good) == good


    def test_empty_returns_none(self):
        assert kb._normalize_board_slug(None) is None
        assert kb._normalize_board_slug("") is None
        assert kb._normalize_board_slug("   ") is None


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

class TestPathResolution:
    def test_default_board_legacy_path(self, fresh_home):
        """The default board's DB lives at ``<root>/kanban.db`` for back-compat."""
        assert kb.kanban_db_path() == fresh_home / "kanban.db"
        assert kb.kanban_db_path(board="default") == fresh_home / "kanban.db"

    def test_named_board_under_boards_dir(self, fresh_home):
        p = kb.kanban_db_path(board="atm10-server")
        assert p == fresh_home / "kanban" / "boards" / "atm10-server" / "kanban.db"


    def test_env_var_db_override_still_wins(self, fresh_home, tmp_path, monkeypatch):
        """``HERMES_KANBAN_DB`` pins the file regardless of board= arg."""
        forced = tmp_path / "custom.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(forced))
        assert kb.kanban_db_path() == forced
        assert kb.kanban_db_path(board="ignored") == forced


# ---------------------------------------------------------------------------
# Current-board resolution
# ---------------------------------------------------------------------------

class TestCurrentBoard:



    def test_stale_file_pointer_falls_back_to_default(self, fresh_home):
        current = fresh_home / "kanban" / "current"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.write_text("missing-board\n", encoding="utf-8")

        assert kb.get_current_board() == "default"
        assert not kb.board_exists("missing-board")
        assert [b["slug"] for b in kb.list_boards()] == ["default"]



    def test_kanban_db_path_reads_current(self, fresh_home):
        """kanban_db_path() with no args respects the on-disk pointer."""
        kb.create_board("my-proj")
        kb.set_current_board("my-proj")
        expected = fresh_home / "kanban" / "boards" / "my-proj" / "kanban.db"
        assert kb.kanban_db_path() == expected


# ---------------------------------------------------------------------------
# Board CRUD
# ---------------------------------------------------------------------------

class TestBoardCRUD:

    @staticmethod
    def _contained_task(board: str) -> tuple[str, int]:
        with kb.connect(board=board) as conn:
            task_id = kb.create_task(conn, title="contained", assignee="worker")
            task = kb.claim_task(conn, task_id, claimer="host:board-owner")
            assert task is not None and task.current_run_id is not None
            assert task.claim_lock is not None
            kb._register_worker_containment(
                conn,
                task_id,
                run_id=task.current_run_id,
                claim_lock=task.claim_lock,
                worker_pid=424242,
                cgroup_path="/sys/fs/cgroup/hermes-board-removal-test",
                cgroup_inode=818181,
            )
            return task_id, task.current_run_id

    def test_archive_task_retires_active_containment(
        self, fresh_home, monkeypatch,
    ):
        kb.create_board("task-archive")
        task_id, run_id = self._contained_task("task-archive")
        monkeypatch.setattr(
            kc,
            "kill_cgroup",
            lambda *_args: {
                "terminated": True,
                "containment_certified": True,
            },
        )

        with kb.connect(board="task-archive") as conn:
            assert kb.archive_task(conn, task_id)
            task = kb.get_task(conn, task_id)
            row = conn.execute(
                "SELECT termination_certified_at FROM worker_containments "
                "WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            assert task is not None and task.status == "archived"
            assert row["termination_certified_at"] is not None

    def test_archive_task_keeps_authority_when_termination_is_uncertain(
        self, fresh_home, monkeypatch,
    ):
        kb.create_board("task-uncertain")
        task_id, run_id = self._contained_task("task-uncertain")
        monkeypatch.setattr(
            kc,
            "kill_cgroup",
            lambda *_args: {
                "terminated": False,
                "containment_certified": False,
            },
        )

        with kb.connect(board="task-uncertain") as conn:
            assert not kb.archive_task(conn, task_id)
            task = kb.get_task(conn, task_id)
            row = conn.execute(
                "SELECT retirement_started_at, termination_certified_at "
                "FROM worker_containments WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            assert task is not None and task.status == "running"
            assert task.current_run_id == run_id
            assert row["retirement_started_at"] is not None
            assert row["termination_certified_at"] is None

    def test_archive_board_retires_and_cleans_all_containments(
        self, fresh_home, monkeypatch,
    ):
        kb.create_board("contained-archive")
        _task_id, run_id = self._contained_task("contained-archive")
        monkeypatch.setattr(
            kc,
            "kill_cgroup",
            lambda *_args: {
                "terminated": True,
                "containment_certified": True,
            },
        )
        monkeypatch.setattr(kc, "cgroup_absent", lambda *_args: False)
        monkeypatch.setattr(kc, "cleanup_cgroup", lambda *_args: True)

        result = kb.remove_board("contained-archive", archive=True)

        archived_db = Path(result["new_path"]) / "kanban.db"
        with sqlite3.connect(archived_db) as conn:
            row = conn.execute(
                "SELECT termination_certified_at, cleaned_at "
                "FROM worker_containments WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        assert row[0] is not None
        assert row[1] is not None

    def test_delete_board_fails_closed_when_termination_is_uncertain(
        self, fresh_home, monkeypatch,
    ):
        kb.create_board("contained-delete")
        task_id, run_id = self._contained_task("contained-delete")
        monkeypatch.setattr(
            kc,
            "kill_cgroup",
            lambda *_args: {
                "terminated": False,
                "containment_certified": False,
            },
        )

        with pytest.raises(ValueError, match="containment could not be retired"):
            kb.remove_board("contained-delete", archive=False)

        assert kb.board_dir("contained-delete").is_dir()
        with kb.connect(board="contained-delete") as conn:
            task = kb.get_task(conn, task_id)
            row = conn.execute(
                "SELECT termination_certified_at FROM worker_containments "
                "WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            assert task is not None and task.status == "running"
            assert task.current_run_id == run_id
            assert row["termination_certified_at"] is None

    def test_delete_task_retires_and_cleans_active_containment(
        self, fresh_home, monkeypatch,
    ):
        kb.create_board("task-delete")
        task_id, run_id = self._contained_task("task-delete")
        monkeypatch.setattr(
            kc,
            "kill_cgroup",
            lambda *_args: {
                "terminated": True,
                "containment_certified": True,
            },
        )
        monkeypatch.setattr(kc, "cgroup_absent", lambda *_args: False)
        monkeypatch.setattr(kc, "cleanup_cgroup", lambda *_args: True)

        with kb.connect(board="task-delete") as conn:
            assert kb.delete_task(conn, task_id)
            assert kb.get_task(conn, task_id) is None
            row = conn.execute(
                "SELECT termination_certified_at, cleaned_at "
                "FROM worker_containments WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            assert row["termination_certified_at"] is not None
            assert row["cleaned_at"] is not None

    def test_delete_archived_task_cleans_certified_containment(
        self, fresh_home, monkeypatch,
    ):
        kb.create_board("archived-delete")
        task_id, run_id = self._contained_task("archived-delete")
        monkeypatch.setattr(
            kc,
            "kill_cgroup",
            lambda *_args: {
                "terminated": True,
                "containment_certified": True,
            },
        )

        with kb.connect(board="archived-delete") as conn:
            assert kb.archive_task(conn, task_id)

        monkeypatch.setattr(kc, "cgroup_absent", lambda *_args: False)
        monkeypatch.setattr(kc, "cleanup_cgroup", lambda *_args: True)
        with kb.connect(board="archived-delete") as conn:
            assert kb.delete_archived_task(conn, task_id)
            assert kb.get_task(conn, task_id) is None
            row = conn.execute(
                "SELECT termination_certified_at, cleaned_at "
                "FROM worker_containments WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            assert row["termination_certified_at"] is not None
            assert row["cleaned_at"] is not None






    @pytest.mark.parametrize("archive", [True, False])
    def test_remove_requires_explicit_recreate_with_fresh_schema(
        self, fresh_home, archive,
    ):
        # Removal drops the init cache but stale pollers must fail closed rather
        # than recreating the namespace. Only create_board() may republish it.
        kb.create_board("recycle")
        # First connect populates _INITIALIZED_PATHS for this DB.
        with kb.connect(board="recycle") as conn:
            kb.create_task(conn, title="t1", assignee="dev")
        db_path = kb.board_dir("recycle") / "kanban.db"
        assert str(db_path.resolve()) in kb._INITIALIZED_PATHS

        kb.remove_board("recycle", archive=archive)
        # remove_board must drop the cache entry before any later explicit
        # recreation gets a fresh schema-init pass.
        assert str(db_path.resolve()) not in kb._INITIALIZED_PATHS

        with pytest.raises(FileNotFoundError, match="does not exist"):
            kb.connect(board="recycle")
        assert not kb.board_dir("recycle").exists()

        kb.create_board("recycle")
        with kb.connect(board="recycle") as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert "task_events" in tables
        assert "tasks" in tables

    def test_rename_updates_metadata(self, fresh_home):
        kb.create_board("slug-immutable")
        kb.write_board_metadata("slug-immutable", name="New Display Name")
        assert kb.read_board_metadata("slug-immutable")["name"] == "New Display Name"
        # Slug must not change.
        assert kb.board_exists("slug-immutable")


# ---------------------------------------------------------------------------
# Connection isolation
# ---------------------------------------------------------------------------

class TestConnectionIsolation:
    def test_tasks_do_not_leak_across_boards(self, fresh_home):
        kb.create_board("alpha")
        kb.create_board("beta")

        with kb.connect(board="alpha") as conn:
            kb.create_task(conn, title="alpha-task-1", assignee="dev")
            kb.create_task(conn, title="alpha-task-2", assignee="dev")

        with kb.connect(board="beta") as conn:
            kb.create_task(conn, title="beta-only", assignee="dev")

        with kb.connect(board="alpha") as conn:
            a = kb.list_tasks(conn)
        with kb.connect(board="beta") as conn:
            b = kb.list_tasks(conn)
        with kb.connect(board="default") as conn:
            d = kb.list_tasks(conn)

        assert {t.title for t in a} == {"alpha-task-1", "alpha-task-2"}
        assert {t.title for t in b} == {"beta-only"}
        assert d == []

    def test_connect_without_args_uses_current(self, fresh_home):
        kb.create_board("curr")
        kb.set_current_board("curr")
        with kb.connect() as conn:
            kb.create_task(conn, title="implicit", assignee="x")
        with kb.connect(board="curr") as conn:
            tasks = kb.list_tasks(conn)
        assert [t.title for t in tasks] == ["implicit"]

    def test_connect_env_var_overrides_current(self, fresh_home, monkeypatch):
        kb.create_board("persist")
        kb.create_board("envwin")
        kb.set_current_board("persist")
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "envwin")
        with kb.connect() as conn:
            kb.create_task(conn, title="via-env", assignee="x")
        with kb.connect(board="envwin") as conn:
            assert [t.title for t in kb.list_tasks(conn)] == ["via-env"]
        with kb.connect(board="persist") as conn:
            assert kb.list_tasks(conn) == []


# ---------------------------------------------------------------------------
# Worker spawn env injection
# ---------------------------------------------------------------------------

class TestWorkerSpawnEnv:
    """Ensure the dispatcher pins ``HERMES_KANBAN_BOARD`` / DB / workspaces on spawn.

    We monkey-patch ``subprocess.Popen`` to capture the child env without
    actually spawning anything.
    """

    def test_default_spawn_sets_env_vars(self, fresh_home, monkeypatch):
        captured = {}

        class FakeProc:
            pid = 12345

        def fake_popen(cmd, *args, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env", {})
            return FakeProc()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        kb.create_board("spawntest")

        task = kb.Task(
            id="t_abc",
            title="worker test",
            body=None,
            assignee="teknium",
            status="ready",
            priority=0,
            created_by="user",
            created_at=0,
            started_at=None,
            completed_at=None,
            workspace_kind="scratch",
            workspace_path=None,
            claim_lock=None,
            claim_expires=None,
            tenant=None,
        )

        kb._default_spawn(task, str(fresh_home / "ws"), board="spawntest")

        env = captured["env"]
        assert env["HERMES_KANBAN_BOARD"] == "spawntest"
        assert env["HERMES_KANBAN_TASK"] == "t_abc"
        # DB path should match the per-board DB, not the legacy default.
        expected_db = fresh_home / "kanban" / "boards" / "spawntest" / "kanban.db"
        assert env["HERMES_KANBAN_DB"] == str(expected_db)
        expected_ws = fresh_home / "kanban" / "boards" / "spawntest" / "workspaces"
        assert env["HERMES_KANBAN_WORKSPACES_ROOT"] == str(expected_ws)

    def test_default_spawn_uses_durable_cgroup_gate_when_enabled(
        self, fresh_home, monkeypatch
    ):
        captured = {}

        class FakeHandle:
            pid = 515151
            cgroup_path = "/run/a3d/docker.scope/hermes-kanban-r73-deadbeefdeadbeefdeadbeef"
            cgroup_inode = 7373

        def fake_spawn_gated(
            command, *, task_id, run_id, claim_lock, popen_kwargs
        ):
            captured["command"] = command
            captured["task_id"] = task_id
            captured["run_id"] = run_id
            captured["claim_lock"] = claim_lock
            captured["kwargs"] = popen_kwargs
            return FakeHandle()

        monkeypatch.setattr(kc, "enabled", lambda: True)
        monkeypatch.setattr(kc, "spawn_gated", fake_spawn_gated)
        kb.create_board("contained")
        task = kb.Task(
            id="t_contained",
            title="contained worker",
            body=None,
            assignee="teknium",
            status="running",
            priority=0,
            created_by="user",
            created_at=0,
            started_at=1,
            completed_at=None,
            workspace_kind="scratch",
            workspace_path=None,
            claim_lock="host:claim",
            claim_expires=999,
            tenant=None,
            current_run_id=73,
        )

        result = kb._default_spawn(
            task, str(fresh_home / "ws"), board="contained"
        )

        assert result.__class__ is FakeHandle
        assert captured["task_id"] == "t_contained"
        assert captured["run_id"] == 73
        assert captured["claim_lock"] == "host:claim"
        assert captured["kwargs"]["env"]["HERMES_KANBAN_RUN_ID"] == "73"
        assert captured["command"][-3:] == ["chat", "-q", "work kanban task t_contained"]


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------

def _cli(args: list[str], env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Run ``hermes kanban …`` with PYTHONPATH pinned to the worktree."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_WORKTREE)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban"] + args,
        env=env,
        capture_output=True,
        text=True,
        cwd=str(_WORKTREE),
        timeout=30,
    )


class TestCLI:
    def test_boards_list_default_only(self, tmp_path):
        env = {"HERMES_HOME": str(tmp_path)}
        res = _cli(["boards", "list", "--json"], env_extra=env)
        assert res.returncode == 0, res.stderr
        data = json.loads(res.stdout)
        slugs = [b["slug"] for b in data]
        assert slugs == ["default"]
        assert data[0]["is_current"] is True


    def test_per_board_task_isolation_via_cli(self, tmp_path):
        env = {"HERMES_HOME": str(tmp_path)}
        assert _cli(["boards", "create", "projA"], env_extra=env).returncode == 0
        assert _cli(["boards", "create", "projB"], env_extra=env).returncode == 0

        # Create one task on each via --board.
        r = _cli(["--board", "projA", "create", "Task A", "--assignee", "dev"], env_extra=env)
        assert r.returncode == 0, r.stderr
        r = _cli(["--board", "projB", "create", "Task B", "--assignee", "dev"], env_extra=env)
        assert r.returncode == 0, r.stderr

        # list on each board only shows its own.
        listA = _cli(["--board", "projA", "list", "--json"], env_extra=env)
        listB = _cli(["--board", "projB", "list", "--json"], env_extra=env)
        listD = _cli(["list", "--json"], env_extra=env)

        titlesA = [t["title"] for t in json.loads(listA.stdout)]
        titlesB = [t["title"] for t in json.loads(listB.stdout)]
        titlesD = [t["title"] for t in json.loads(listD.stdout)]

        assert titlesA == ["Task A"]
        assert titlesB == ["Task B"]
        assert titlesD == []


def _publish_late_contained_successor(board: str, task_id: str) -> None:
    other = sqlite3.connect(
        kb.kanban_db_path(board), isolation_level=None, timeout=0
    )
    try:
        other.execute("PRAGMA busy_timeout=0")
        other.execute("BEGIN IMMEDIATE")
        now = 1_700_000_000
        lock = "host:late-successor"
        cur = other.execute(
            "INSERT INTO task_runs "
            "(task_id, status, claim_lock, worker_pid, started_at) "
            "VALUES (?, 'running', ?, ?, ?)",
            (task_id, lock, 515151, now),
        )
        assert cur.lastrowid is not None
        run_id = int(cur.lastrowid)
        other.execute(
            "UPDATE tasks SET status='running', claim_lock=?, worker_pid=?, "
            "current_run_id=?, started_at=? WHERE id=?",
            (lock, 515151, run_id, now, task_id),
        )
        other.execute(
            "INSERT INTO worker_containments "
            "(run_id, task_id, claim_lock, backend, worker_pid, cgroup_path, "
            "cgroup_inode, created_at) VALUES (?, ?, ?, 'cgroup_v2', ?, ?, ?, ?)",
            (
                run_id,
                task_id,
                lock,
                515151,
                "/sys/fs/cgroup/late-successor",
                919191,
                now,
            ),
        )
        other.commit()
    finally:
        other.close()


def test_archive_task_blocks_successor_between_retirement_and_archive(
    fresh_home, monkeypatch,
):
    kb.create_board("task-archive-race")
    with kb.connect(board="task-archive-race") as conn:
        task_id = kb.create_task(conn, title="archive race", assignee="worker")
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))
    original = kb.ensure_task_containment_retired
    outcome = {"blocked": False, "committed": False}

    def retire_then_race(conn, target_task_id, **kwargs):
        assert original(conn, target_task_id, **kwargs)
        try:
            _publish_late_contained_successor("task-archive-race", target_task_id)
            outcome["committed"] = True
        except (sqlite3.IntegrityError, sqlite3.OperationalError):
            outcome["blocked"] = True
        return True

    monkeypatch.setattr(kb, "ensure_task_containment_retired", retire_then_race)
    with kb.connect(board="task-archive-race") as conn:
        assert kb.archive_task(conn, task_id)
        assert conn.execute(
            "SELECT COUNT(*) FROM worker_containments WHERE cleaned_at IS NULL"
        ).fetchone()[0] == 0
    assert outcome == {"blocked": True, "committed": False}


def test_remove_board_blocks_successor_after_containment_snapshot(
    fresh_home, monkeypatch,
):
    kb.create_board("board-remove-race")
    with kb.connect(board="board-remove-race") as conn:
        task_id = kb.create_task(conn, title="board race", assignee="worker")
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))
        task = kb.claim_task(conn, task_id, claimer="host:board-owner")
        assert task is not None and task.current_run_id is not None
        assert task.claim_lock is not None
        kb._register_worker_containment(
            conn,
            task_id,
            run_id=task.current_run_id,
            claim_lock=task.claim_lock,
            worker_pid=424242,
            cgroup_path="/sys/fs/cgroup/board-remove-race-old",
            cgroup_inode=818181,
        )
    monkeypatch.setattr(
        kc,
        "kill_cgroup",
        lambda *_args: {"terminated": True, "containment_certified": True},
    )
    monkeypatch.setattr(kc, "cgroup_absent", lambda *_args: False)
    monkeypatch.setattr(kc, "cleanup_cgroup", lambda *_args: True)
    original = kb.ensure_task_containment_retired
    outcome = {"blocked": False, "committed": False}

    def retire_then_race(conn, target_task_id, **kwargs):
        assert original(conn, target_task_id, **kwargs)
        try:
            _publish_late_contained_successor("board-remove-race", target_task_id)
            outcome["committed"] = True
        except (sqlite3.IntegrityError, sqlite3.OperationalError):
            outcome["blocked"] = True
        return True

    monkeypatch.setattr(kb, "ensure_task_containment_retired", retire_then_race)
    result = kb.remove_board("board-remove-race", archive=True)
    assert result["action"] == "archived"
    assert outcome == {"blocked": True, "committed": False}


def test_remove_board_rejects_authority_from_stale_open_connection(fresh_home):
    kb.create_board("stale-board-writer")
    stale = kb.connect(board="stale-board-writer")
    try:
        task_id = kb.create_task(stale, title="stale writer", assignee="worker")
        stale.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))

        result = kb.remove_board("stale-board-writer", archive=True)
        assert result["action"] == "archived"

        with pytest.raises(sqlite3.IntegrityError, match="board retirement"):
            kb.claim_task(stale, task_id, claimer="host:stale-writer")
    finally:
        stale.close()


@pytest.mark.parametrize("archive", [True, False], ids=["archive", "delete"])
@pytest.mark.parametrize(
    "publisher",
    [
        "metadata",
        "explicit-db",
        "env-db",
        "init-board",
        "init-explicit-db",
        "init-env-db",
        "attachment",
        "workspace",
    ],
)
def test_remove_board_excludes_named_namespace_publishers_until_removal_finishes(
    fresh_home, monkeypatch, archive, publisher,
):
    slug = f"remove-{publisher}-{archive}"
    kb.create_board(slug)
    stale = kb.connect(board=slug)
    task_id = kb.create_task(stale, title="namespace filesystem producer")
    task = kb.get_task(stale, task_id)
    assert task is not None
    board_path = kb.board_dir(slug)
    db_path = kb.kanban_db_path(slug)
    removed = threading.Event()
    release = threading.Event()
    published = threading.Event()
    remove_result = {}
    publication_result = {}

    if archive:
        original_remove = Path.rename

        def paused_archive(path, target):
            result = original_remove(path, target)
            if path == board_path:
                removed.set()
                assert release.wait(5), "timed out waiting to release archive"
            return result

        monkeypatch.setattr(Path, "rename", paused_archive)
    else:
        original_remove = shutil.rmtree

        def paused_delete(path, *args, **kwargs):
            result = original_remove(path, *args, **kwargs)
            if Path(path) == board_path:
                removed.set()
                assert release.wait(5), "timed out waiting to release delete"
            return result

        monkeypatch.setattr(shutil, "rmtree", paused_delete)

    def remove():
        try:
            remove_result["value"] = kb.remove_board(slug, archive=archive)
        except BaseException as exc:  # pragma: no cover - assertion reports it
            remove_result["error"] = exc

    def publish():
        try:
            if publisher == "metadata":
                kb.write_board_metadata(slug, name="must-not-resurrect")
            elif publisher.startswith("init-"):
                if publisher == "init-env-db":
                    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
                    kb.init_db()
                elif publisher == "init-explicit-db":
                    kb.init_db(db_path=db_path)
                else:
                    kb.init_db(board=slug)
            elif publisher == "attachment":
                kb.store_attachment_bytes(
                    stale, task_id, "late.txt", b"late", board=slug,
                )
            elif publisher == "workspace":
                kb.resolve_workspace(task, board=slug)
            else:
                if publisher == "env-db":
                    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
                    conn = kb.connect()
                else:
                    conn = kb.connect(db_path=db_path)
                conn.close()
        except BaseException as exc:
            publication_result["error"] = exc
        finally:
            published.set()

    remover = threading.Thread(target=remove)
    remover.start()
    assert removed.wait(5), "board removal did not reach namespace boundary"
    writer = threading.Thread(target=publish)
    writer.start()
    try:
        completed_while_remove_held_lock = published.wait(0.2)
    finally:
        release.set()
        remover.join(5)
        writer.join(5)
        stale.close()

    assert not remover.is_alive()
    assert not writer.is_alive()
    assert not completed_while_remove_held_lock, publication_result
    assert "error" not in remove_result
    assert remove_result["value"]["action"] == (
        "archived" if archive else "deleted"
    )
    assert isinstance(publication_result.get("error"), FileNotFoundError)
    assert not board_path.exists()


@pytest.mark.parametrize("archive", [True, False], ids=["archive", "delete"])
@pytest.mark.parametrize("producer", ["worker-log", "completion-artifact"])
def test_removed_board_post_removal_filesystem_producers_fail_without_residue(
    fresh_home, archive, producer,
):
    slug = f"removed-{producer}-{archive}"
    kb.create_board(slug)
    stale = kb.connect(board=slug)
    try:
        task_id = kb.create_task(
            stale, title="post-removal producer", assignee="worker",
        )
        task = kb.get_task(stale, task_id)
        assert task is not None
        kb.remove_board(slug, archive=archive)

        with pytest.raises(FileNotFoundError, match="does not exist"):
            if producer == "worker-log":
                getattr(kb, "_open_worker_log")(task, board=slug)
            else:
                kb.complete_task(
                    stale,
                    task_id,
                    result="must not complete",
                    metadata={"artifacts": [str(fresh_home / "missing.txt")]},
                )
        assert not kb.board_dir(slug).exists()
    finally:
        stale.close()


@pytest.mark.parametrize("link_kind", ["directory", "database"])
def test_named_board_symlink_storage_fails_closed(fresh_home, link_kind):
    slug = f"symlink-{link_kind}"
    external = fresh_home / f"external-{link_kind}"
    external.mkdir()
    (external / "board.json").write_text(
        json.dumps({"slug": slug, "name": slug}), encoding="utf-8",
    )
    kb.init_db(db_path=external / "kanban.db")
    kb.boards_root().mkdir(parents=True, exist_ok=True)

    if link_kind == "directory":
        kb.board_dir(slug).symlink_to(external, target_is_directory=True)
    else:
        directory = kb.board_dir(slug)
        directory.mkdir(parents=True)
        (directory / "board.json").write_text(
            json.dumps({"slug": slug, "name": slug}), encoding="utf-8",
        )
        (directory / "kanban.db").symlink_to(external / "kanban.db")

    with pytest.raises(ValueError, match="unsupported symlinked storage"):
        kb.connect(board=slug)
    with pytest.raises(ValueError, match="unsupported symlinked storage"):
        kb.init_db(board=slug)
    with pytest.raises(ValueError, match="unsupported symlinked storage"):
        kb.write_board_metadata(slug, name="must-not-follow")



