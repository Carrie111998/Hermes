from __future__ import annotations

import contextlib
import sqlite3
import threading
from pathlib import Path

from hermes_cli import kanban_db as kb


def _make_legacy_db(path: Path) -> None:
    """Write a kanban DB with the pre-AUTOINCREMENT (TEXT PK) schema for the
    four tables #35096 affects, keeping every other table current so the
    additive-column migration runs cleanly on top.
    """
    conn = sqlite3.connect(str(path))
    conn.executescript(kb.SCHEMA_SQL)
    conn.executescript(
        """
        DROP TABLE task_events;
        DROP TABLE task_comments;
        DROP TABLE task_runs;
        DROP TABLE kanban_notify_subs;
        CREATE TABLE task_comments (id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
            author TEXT NOT NULL, body TEXT NOT NULL, created_at INTEGER NOT NULL);
        CREATE TABLE task_events (id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
            kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL);
        CREATE TABLE task_runs (id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
            profile TEXT, step_key TEXT, status TEXT NOT NULL, claim_lock TEXT,
            claim_expires INTEGER, worker_pid INTEGER, max_runtime_seconds INTEGER,
            last_heartbeat_at INTEGER, started_at INTEGER NOT NULL, ended_at INTEGER,
            outcome TEXT, summary TEXT, metadata TEXT, error TEXT, owner_kind TEXT,
            owner TEXT, external_id TEXT, phase TEXT, progress_current INTEGER,
            progress_total INTEGER, log_ref TEXT, result_ref TEXT, pid_scope TEXT,
            host_start_time INTEGER, managed_process_session_id TEXT,
            durable_result_path TEXT);
        CREATE TABLE kanban_notify_subs (task_id TEXT NOT NULL, platform TEXT NOT NULL,
            chat_id TEXT NOT NULL, thread_id TEXT NOT NULL DEFAULT '', user_id TEXT,
            created_at INTEGER NOT NULL, last_event_id TEXT,
            PRIMARY KEY (task_id, platform, chat_id, thread_id));
        """
    )
    conn.execute("INSERT INTO tasks (id, title, status, current_run_id, created_at) VALUES ('task-1', 'T', 'running', 'r-1', 1000)")
    conn.execute("INSERT INTO task_comments VALUES ('c-1', 'task-1', 'agent', 'hi', 1500)")
    conn.execute("INSERT INTO task_events VALUES ('e-1', 'task-1', 'completed', NULL, 2000)")
    conn.execute("INSERT INTO task_events VALUES ('e-2', 'task-1', 'blocked', NULL, 2100)")
    conn.execute("INSERT INTO task_runs VALUES ('r-1', 'task-1', 'external-lane', 'publish', 'running', 'external:build-7', 1234, 4321, 5678, 1200, 1000, NULL, NULL, 'handoff', '{\"on_failure\":\"retry\"}', NULL, 'external', 'external-lane', 'build-7', 'upload', 2, 3, 'log://build-7', 'result://build-7', 'host', 999, 'proc_legacy', '/tmp/proc_legacy.json')")
    conn.execute(
        "INSERT INTO kanban_notify_subs (task_id, platform, chat_id, created_at, last_event_id) "
        "VALUES ('task-1', 'telegram', '123', 1000, 'e-1')"
    )
    conn.commit()
    conn.close()


def _setup_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="legacy")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    return db_path


def _table_struct(conn: sqlite3.Connection, table: str):
    cols = [
        (r["name"], (r["type"] or "").upper(), r["notnull"], r["pk"])
        for r in conn.execute(f"PRAGMA table_info({table})")
    ]
    idx = sorted(
        r["name"]
        for r in conn.execute(f"PRAGMA index_list({table})")
        if not r["name"].startswith("sqlite_")
    )
    return cols, idx




def test_legacy_text_pk_tables_rebuilt_to_integer_autoincrement(tmp_path, monkeypatch):
    """A pre-AUTOINCREMENT DB is migrated in place: id columns become INTEGER
    PKs, ``last_event_id`` becomes INTEGER, data is preserved, and indexes
    are recreated (DROP TABLE would otherwise take them down)."""
    db_path = _setup_home(tmp_path, monkeypatch)
    _make_legacy_db(db_path)

    with kb.connect(db_path) as conn:
        for table in ("task_events", "task_comments", "task_runs"):
            id_col = {r["name"]: r for r in conn.execute(f"PRAGMA table_info({table})")}["id"]
            assert id_col["type"].upper() == "INTEGER" and id_col["pk"] == 1

        lei = {r["name"]: r for r in conn.execute("PRAGMA table_info(kanban_notify_subs)")}
        assert lei["last_event_id"]["type"].upper() == "INTEGER"
        assert "delivery_metadata" in lei

        # Data preserved across the rebuild.
        assert len(conn.execute("SELECT * FROM task_events").fetchall()) == 2
        assert conn.execute("SELECT body FROM task_comments").fetchone()["body"] == "hi"
        run = conn.execute("SELECT * FROM task_runs").fetchone()
        assert run is not None
        assert {
            "profile": "external-lane", "step_key": "publish", "status": "running",
            "claim_lock": "external:build-7", "claim_expires": 1234, "worker_pid": 4321,
            "max_runtime_seconds": 5678, "last_heartbeat_at": 1200, "started_at": 1000,
            "summary": "handoff", "metadata": '{\"on_failure\":\"retry\"}',
            "owner_kind": "external", "owner": "external-lane", "external_id": "build-7",
            "phase": "upload", "progress_current": 2, "progress_total": 3,
            "log_ref": "log://build-7", "result_ref": "result://build-7",
            "pid_scope": "host", "host_start_time": 999,
            "managed_process_session_id": "proc_legacy",
            "durable_result_path": "/tmp/proc_legacy.json",
        }.items() <= dict(run).items()
        # A legacy TEXT pointer must be rewritten to the AUTOINCREMENT id.
        assert conn.execute("SELECT current_run_id FROM tasks WHERE id='task-1'").fetchone()["current_run_id"] == run["id"]
        # Non-numeric legacy cursor ("e-1") casts to 0.
        assert conn.execute("SELECT last_event_id FROM kanban_notify_subs").fetchone()["last_event_id"] == 0

        # Indexes restored, including idx_events_run (added by the additive pass).
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        for name in ("idx_events_task", "idx_events_run", "idx_comments_task",
                     "idx_runs_task", "idx_runs_status", "idx_notify_task"):
            assert name in indexes

        # AUTOINCREMENT actually works after the rebuild.
        conn.execute("INSERT INTO task_events (task_id, kind, created_at) VALUES ('task-1', 'completed', 3000)")
        new_id = conn.execute("SELECT id FROM task_events ORDER BY id DESC LIMIT 1").fetchone()["id"]
        assert isinstance(new_id, int) and new_id >= 1





def test_owner_provenance_columns_backfill_legacy_executable_tasks_conservatively(tmp_path, monkeypatch):
    """Assigned executable legacy work keeps its dispatch intent without inventing profiles.

    The migration may use durable historical facts (assignee plus executable
    status/current run), but it must not probe the currently installed profile
    roster: an old profile can be restored later and remains a valid agent lane.
    """
    db_path = _setup_home(tmp_path, monkeypatch)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        conn.executescript(kb.SCHEMA_SQL)
        columns = [
            row for row in conn.execute("PRAGMA table_info(tasks)")
            if row["name"] not in {
                "owner_kind", "task_kind", "purpose", "created_by_task_id",
                "created_by_run_id", "creation_authority",
            }
        ]
        conn.execute("DROP TABLE tasks")
        definitions = []
        for row in columns:
            definition = f'"{row["name"]}" {row["type"]}'
            if row["pk"]:
                definition += " PRIMARY KEY"
            elif row["notnull"]:
                definition += " NOT NULL"
            if row["dflt_value"] is not None:
                definition += f" DEFAULT {row['dflt_value']}"
            definitions.append(definition)
        conn.execute(f"CREATE TABLE tasks ({', '.join(definitions)})")
        conn.executemany(
            "INSERT INTO tasks (id, title, status, assignee, created_at) VALUES (?, ?, ?, ?, 1)",
            [
                ("legacy-todo", "blocked-by-parent executable", "todo", "old-worker"),
                ("legacy-scheduled", "scheduled executable", "scheduled", "old-worker"),
                ("legacy-ready", "queued executable", "ready", "old-worker"),
                ("legacy-running", "active executable", "running", "old-worker"),
                ("legacy-blocked", "blocked executable", "blocked", "old-worker"),
                ("legacy-review", "review executable", "review", "old-worker"),
                ("legacy-triage", "triage executable", "triage", "old-worker"),
                ("legacy-done", "completed work", "done", "old-worker"),
                ("legacy-archived", "archived work", "archived", "old-worker"),
                ("legacy-unassigned", "unassigned queue", "ready", None),
                ("legacy-manual", "manual lane", "todo", "release-manager"),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect(db_path) as migrated:
        fields = {row["name"] for row in migrated.execute("PRAGMA table_info(tasks)")}
        assert {"owner_kind", "task_kind", "purpose", "created_by_task_id", "created_by_run_id", "creation_authority"} <= fields
        owners = {
            task_id: kb.get_task(migrated, task_id).owner_kind
            for task_id in (
                "legacy-todo", "legacy-scheduled", "legacy-ready", "legacy-running",
                "legacy-blocked", "legacy-review", "legacy-triage", "legacy-done",
                "legacy-archived", "legacy-unassigned", "legacy-manual",
            )
        }
        assert owners == {
            "legacy-todo": "agent", "legacy-scheduled": "agent",
            "legacy-ready": "agent", "legacy-running": "agent",
            "legacy-blocked": "agent", "legacy-review": "agent",
            "legacy-triage": "agent", "legacy-done": "agent",
            "legacy-archived": "agent", "legacy-unassigned": "no_agent",
            "legacy-manual": "agent",
        }
        assert all(
            kb.get_task(migrated, task_id).owner_kind_explicit is False
            for task_id in owners
        )
        assert kb.claim_task(migrated, "legacy-ready", claimer="test") is not None
        ready = kb.get_task(migrated, "legacy-ready")
        assert ready is not None and ready.task_kind == "ordinary" and ready.purpose is None

    # Reopening cannot undo the selected ownership classification.
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect(db_path) as migrated:
        assert kb.get_task(migrated, "legacy-review").owner_kind == "agent"
        assert kb.get_task(migrated, "legacy-manual").owner_kind == "agent"


def test_legacy_agent_backfill_defers_missing_profile_preflight_until_dispatch(tmp_path, monkeypatch):
    """Migration never auto-spawns an invented profile; dispatch preflights it later."""
    db_path = _setup_home(tmp_path, monkeypatch)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(kb.SCHEMA_SQL)
        conn.execute("ALTER TABLE tasks DROP COLUMN owner_kind")
        conn.execute(
            "INSERT INTO tasks (id, title, status, assignee, created_at) "
            "VALUES ('legacy-ready', 'queued executable', 'ready', 'restorable-profile', 1)"
        )
        conn.commit()
    finally:
        conn.close()

    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: False)
    spawned = []
    with kb.connect(db_path) as migrated:
        assert kb.get_task(migrated, "legacy-ready").owner_kind == "agent"
        kb.dispatch_once(migrated, spawn_fn=lambda task, workspace: spawned.append(task.id) or 1)
        assert kb.get_task(migrated, "legacy-ready").status == "ready"
    assert spawned == []


def test_candidate_uses_completed_producing_run_profile_not_reassigned_task(tmp_path, monkeypatch):
    """A reassignment cannot turn the original implementer into its reviewer."""
    db_path = _setup_home(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_PROFILE", "coordinator")
    monkeypatch.setattr(kb, "_configured_coordinator_principal", lambda: "coordinator")
    with kb.connect(db_path) as conn:
        task_id = kb.create_task(conn, title="source", assignee="implementer")
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at, summary) "
            "VALUES (?, 'implementer', 'done', 'completed', 1, 2, 'produced')",
            (task_id,),
        )
        conn.execute("UPDATE tasks SET assignee='reviewer' WHERE id=?", (task_id,))
        candidate = kb.create_candidate(
            conn, task_id, sha="a" * 40,
            source_receipt={"candidate_sha": "a" * 40, "subject": "artifact", "provenance": "test", "producer_profile": "implementer"},
        )
    assert candidate["implementer"] == "implementer"


def test_migration_preserves_assigned_dependency_graph_dispatch_without_spawning_unknown_profiles(tmp_path, monkeypatch):
    """A legacy todo child retains its agent lane until its parent completes."""
    db_path = _setup_home(tmp_path, monkeypatch)
    with sqlite3.connect(str(db_path)) as legacy:
        legacy.executescript(kb.SCHEMA_SQL)
        legacy.execute("ALTER TABLE tasks DROP COLUMN owner_kind")
        legacy.executemany(
            "INSERT INTO tasks (id, title, status, assignee, created_at) VALUES (?, ?, ?, ?, 1)",
            [
                ("parent", "legacy parent", "ready", "known"),
                ("child", "legacy dependent", "todo", "known"),
                ("unknown", "legacy unknown profile", "ready", "absent-profile"),
            ],
        )
        legacy.execute("INSERT INTO task_links (parent_id, child_id) VALUES ('parent', 'child')")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name == "known")
    spawned = []
    with kb.connect(db_path) as migrated:
        assert kb.get_task(migrated, "child").owner_kind == "agent"
        assert kb.get_task(migrated, "child").owner_kind_explicit is False
        kb.dispatch_once(migrated, spawn_fn=lambda task, *_: spawned.append(task.id) or 1)
        assert spawned == ["parent"]
        assert kb.complete_task(migrated, "parent", result="done")
        kb.dispatch_once(migrated, spawn_fn=lambda task, *_: spawned.append(task.id) or 1)
        assert spawned == ["parent", "child"]
        assert kb.get_task(migrated, "unknown").status == "ready"
    assert spawned.count("child") == 1
    assert "unknown" not in spawned


def test_candidate_evidence_and_verdict_tables_are_added_to_legacy_db(tmp_path, monkeypatch):
    """Schema initialization adds all immutable-evidence tables to an old DB."""
    db_path = _setup_home(tmp_path, monkeypatch)
    with sqlite3.connect(str(db_path)) as legacy:
        legacy.executescript(kb.SCHEMA_SQL)
        legacy.execute("DROP TABLE task_gate_verdicts")
        legacy.execute("DROP TABLE task_evidence_receipts")
        legacy.execute("DROP TABLE task_candidates")
        legacy.execute(
            "INSERT INTO tasks (id, title, status, created_at) "
            "VALUES ('legacy-evidence', 'legacy', 'done', 1)"
        )
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect(db_path) as migrated:
        tables = {
            row["name"] for row in migrated.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"task_candidates", "task_evidence_receipts", "task_gate_verdicts", "task_release_barriers"} <= tables
        assert migrated.execute(
            "SELECT title FROM tasks WHERE id='legacy-evidence'"
        ).fetchone()["title"] == "legacy"


def test_migration_is_idempotent(tmp_path, monkeypatch):
    """Re-opening an already-migrated DB is a no-op and leaves data intact."""
    db_path = _setup_home(tmp_path, monkeypatch)
    _make_legacy_db(db_path)

    with kb.connect(db_path):
        pass
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    with kb.connect(db_path) as conn:
        id_col = {r["name"]: r for r in conn.execute("PRAGMA table_info(task_events)")}["id"]
        assert id_col["type"].upper() == "INTEGER"
        assert len(conn.execute("SELECT * FROM task_events").fetchall()) == 2


def test_unseen_events_for_sub_survives_migrated_db(tmp_path, monkeypatch):
    """The crash that motivated #35096 — ``int(None)`` on a NULL cursor — is
    gone after migration; the notifier query returns an integer cursor."""
    db_path = _setup_home(tmp_path, monkeypatch)
    _make_legacy_db(db_path)

    with kb.connect(db_path) as conn:
        cursor, events = kb.unseen_events_for_sub(
            conn, task_id="task-1", platform="telegram", chat_id="123"
        )
        assert isinstance(cursor, int)
        assert isinstance(events, list)


def _default_board_db(tmp_path, monkeypatch) -> Path:
    """Point the kanban root at a temp home and return the default board's DB
    (the back-compat top-level ``<root>/kanban.db`` #83445 reports on)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    return db_path


def _tables(path: Path) -> set[str]:
    conn = sqlite3.connect(str(path))
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def test_connect_reinitializes_schema_when_db_file_vanished(tmp_path, monkeypatch):
    """#83445: the schema cache is process-local, but the schema is on disk.

    A long-lived process (gateway, dispatcher, dashboard API) that already
    initialized a path keeps taking the ``_INITIALIZED_PATHS`` fast path after
    the file is deleted underneath it. SQLite recreates an empty DB on the next
    open, so every query then fails with ``no such table: tasks`` and the board
    renders empty until that process itself is restarted.
    """
    db_path = _default_board_db(tmp_path, monkeypatch)

    with kb.connect_closing(db_path) as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at) VALUES ('t-1', 'T', 'ready', 1000)"
        )
        conn.commit()
    assert str(db_path.resolve()) in kb._INITIALIZED_PATHS

    # External deletion (manual cleanup, restore, sync tool) while the process
    # that cached this path is still alive.
    for suffix in ("", "-wal", "-shm"):
        db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)

    with kb.connect_closing(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert "tasks" in _tables(db_path)


def test_connect_reinitializes_schema_when_db_replaced_by_empty_file(tmp_path, monkeypatch):
    """Same defect, restore shape: the file still exists and passes both the
    header and the integrity probes, but carries no schema at all."""
    db_path = _default_board_db(tmp_path, monkeypatch)

    with kb.connect_closing(db_path):
        pass

    for suffix in ("", "-wal", "-shm"):
        db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)
    sqlite3.connect(str(db_path)).close()
    assert "tasks" not in _tables(db_path)

    with kb.connect_closing(db_path) as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at) VALUES ('t-2', 'T', 'ready', 1000)"
        )
        conn.commit()
    assert "tasks" in _tables(db_path)


def test_healthy_fast_path_stays_lock_free(tmp_path, monkeypatch):
    """The self-heal must cost nothing in steady state: an intact cached path
    still skips the cross-process init lock (#36644), and only pays for it when
    the schema is actually gone."""
    db_path = _default_board_db(tmp_path, monkeypatch)

    with kb.connect_closing(db_path):
        pass

    locks: list[Path] = []
    real_lock = kb._cross_process_init_lock

    @contextlib.contextmanager
    def recording_lock(path):
        locks.append(path)
        with real_lock(path):
            yield

    monkeypatch.setattr(kb, "_cross_process_init_lock", recording_lock)

    with kb.connect_closing(db_path):
        pass
    assert locks == []

    db_path.unlink()
    with kb.connect_closing(db_path):
        pass
    assert len(locks) == 1
