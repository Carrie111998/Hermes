"""Additive PM/plan/gate schema.

The whole point of this slice is that a board which never creates a
``pm_projects`` row is indistinguishable from one built before these tables
existed. These tests pin that, plus the legacy-upgrade path.
"""

import sqlite3

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def conn(tmp_path):
    c = kb.connect(db_path=tmp_path / "kanban.db")
    yield c
    c.close()


def _tables(c):
    return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _cols(c, table):
    return {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}


def _indexes(c):
    return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")}


# --- fresh DB ------------------------------------------------------------

def test_fresh_db_has_the_pm_tables(conn):
    assert {"pm_projects", "pm_plans", "pm_approvals"} <= _tables(conn)


def test_fresh_db_still_has_every_pre_existing_table(conn):
    assert {
        "tasks", "task_links", "task_comments", "task_events",
        "task_runs", "task_attachments", "kanban_notify_subs",
    } <= _tables(conn)


def test_tasks_gains_gate_state_defaulting_to_null(conn):
    assert "gate_state" in _cols(conn, "tasks")
    tid = kb.create_task(conn, title="t", assignee="a")
    row = conn.execute("SELECT gate_state FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert row["gate_state"] is None


def test_gate_index_exists(conn):
    assert "idx_tasks_gate" in _indexes(conn)


def test_valid_gate_states_vocabulary(conn):
    assert kb.VALID_GATE_STATES == {"plan", "deploy"}


def test_no_new_status_was_introduced(conn):
    """Option B's core promise: the status vocabulary is untouched."""
    assert kb.VALID_STATUSES == {
        "triage", "todo", "scheduled", "ready", "running",
        "blocked", "review", "done", "archived",
    }


# --- constraints ---------------------------------------------------------

def test_pm_plans_revision_is_unique_per_project(conn):
    conn.execute(
        "INSERT INTO pm_projects (id, slug, name, plan_revision, archived, created_at)"
        " VALUES ('p1','s','n',0,0,1)"
    )
    conn.execute(
        "INSERT INTO pm_plans (project_id, revision, body, proposed_at)"
        " VALUES ('p1', 1, 'b', 1)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO pm_plans (project_id, revision, body, proposed_at)"
            " VALUES ('p1', 1, 'other', 2)"
        )


def test_pm_approvals_rejects_a_replayed_subject_and_hash(conn):
    """The replay defence, at the storage layer."""
    conn.execute(
        "INSERT INTO pm_approvals (subject, binding_hash, decision, created_at)"
        " VALUES ('plan:p1:1', 'h', 'approved', 1)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO pm_approvals (subject, binding_hash, decision, created_at)"
            " VALUES ('plan:p1:1', 'h', 'approved', 2)"
        )


def test_pm_approvals_nonce_is_unique(conn):
    conn.execute(
        "INSERT INTO pm_approvals (subject, binding_hash, decision, nonce, created_at)"
        " VALUES ('a', 'h1', 'approved', 'n', 1)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO pm_approvals (subject, binding_hash, decision, nonce, created_at)"
            " VALUES ('b', 'h2', 'approved', 'n', 2)"
        )


def test_pm_projects_slug_is_unique(conn):
    conn.execute(
        "INSERT INTO pm_projects (id, slug, name, plan_revision, archived, created_at)"
        " VALUES ('p1','dup','n',0,0,1)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO pm_projects (id, slug, name, plan_revision, archived, created_at)"
            " VALUES ('p2','dup','n',0,0,1)"
        )


# --- legacy upgrade ------------------------------------------------------

def test_legacy_db_without_gate_state_upgrades_cleanly(tmp_path):
    """A pre-slice board must open, regain the column and index, and keep its rows.

    Built by creating a REAL board and then stripping exactly this slice's
    additions — rather than hand-rolling a fake legacy schema, which would test
    a shape that never shipped. This is the case that breaks if CREATE INDEX
    runs before ADD COLUMN.
    """
    path = tmp_path / "legacy.db"
    c = kb.connect(db_path=path)
    tid = kb.create_task(c, title="legacy row", assignee="a")
    c.close()

    raw = sqlite3.connect(path)
    raw.executescript(
        "DROP INDEX IF EXISTS idx_tasks_gate;"
        "DROP TABLE IF EXISTS pm_approvals;"
        "DROP TABLE IF EXISTS pm_plans;"
        "DROP TABLE IF EXISTS pm_projects;"
        "ALTER TABLE tasks DROP COLUMN gate_state;"
    )
    raw.commit()
    assert "gate_state" not in {
        r[1] for r in raw.execute("PRAGMA table_info(tasks)")
    }
    raw.close()

    # kanban_db caches initialised paths for the life of the process, so a
    # second connect() in the same process would skip init. Clearing the cache
    # models what actually happens on a legacy board: a NEW process opens it.
    kb._INITIALIZED_PATHS.clear()

    c = kb.connect(db_path=path)
    try:
        assert "gate_state" in _cols(c, "tasks")
        assert "idx_tasks_gate" in _indexes(c)
        assert {"pm_projects", "pm_plans", "pm_approvals"} <= _tables(c)
        row = c.execute(
            "SELECT title, gate_state FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        assert row["title"] == "legacy row"
        assert row["gate_state"] is None
    finally:
        c.close()


def test_reopening_is_idempotent(tmp_path):
    path = tmp_path / "k.db"
    for _ in range(3):
        c = kb.connect(db_path=path)
        assert {"pm_projects", "pm_plans", "pm_approvals"} <= _tables(c)
        c.close()


def test_existing_board_behaviour_is_unchanged_without_pm_rows(conn):
    """No pm_projects row ⇒ the board behaves exactly as before."""
    parent = kb.create_task(conn, title="parent", assignee="a")
    child = kb.create_task(conn, title="child", assignee="a", parents=[parent])
    assert kb.recompute_ready(conn) >= 0
    assert kb.get_task(conn, child).status in {"todo", "ready"}
    assert kb.complete_task(conn, parent, result="done") is True
    kb.recompute_ready(conn)
    assert kb.get_task(conn, child).status == "ready"
    assert conn.execute("SELECT COUNT(*) c FROM pm_projects").fetchone()["c"] == 0
