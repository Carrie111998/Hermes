"""Tests for hermes_cli.kanban_v2_migration — guarded scratch migration."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_v2_migration as migration


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_PRE_INTEGRATION_SQL = (
    Path(__file__).resolve().parent.parent.parent
    / "fixtures" / "kanban" / "v2_migration" / "pre_integration.sql"
)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB (same as test_kanban_db)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb
    kb.init_db()
    return home


@pytest.fixture
def pre_integration_db(tmp_path: Path) -> Path:
    """Create a scratch SQLite DB from the pre-integration fixture."""
    src = tmp_path / "kanban.db"
    with sqlite3.connect(str(src)) as conn:
        conn.executescript(_PRE_INTEGRATION_SQL.read_text(encoding="utf-8"))
    return src


@pytest.fixture
def empty_scratch_db(tmp_path: Path) -> Path:
    """Create an empty scratch SQLite DB with the kanban schema."""
    db = tmp_path / "empty.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            """CREATE TABLE tasks (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT,
                assignee TEXT, status TEXT NOT NULL, created_at INTEGER NOT NULL,
                workflow_template_id TEXT, current_step_key TEXT,
                work_item_kind TEXT NOT NULL DEFAULT 'card',
                running INTEGER NOT NULL DEFAULT 0,
                blocked INTEGER NOT NULL DEFAULT 0,
                source_commit_required INTEGER NOT NULL DEFAULT 0,
                source_commit_forbidden INTEGER NOT NULL DEFAULT 0
            )"""
        )
        conn.execute(
            "INSERT INTO board_governance (id, qualification_required) VALUES (1, 0)"
        )
    return db


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def test_rejects_relative_path() -> None:
    with pytest.raises(migration.MigrationBlocked, match="refusing relative path"):
        migration.audit_db("relative/path/to/db.sqlite")


def test_rejects_nonexistent_path() -> None:
    with pytest.raises(migration.MigrationBlocked, match="database file does not exist"):
        migration.audit_db("/nonexistent/path/to/kanban.db")


def test_rejects_live_board_db(kanban_home) -> None:
    """A path that matches a live board DB must be rejected."""
    from hermes_cli import kanban_db as kb
    # The kanban_home fixture creates a DB at the live path.
    live_path = kb.kanban_db_path(board="default")
    with pytest.raises(migration.MigrationBlocked, match="refusing live board"):
        migration.audit_db(str(live_path))


# ---------------------------------------------------------------------------
# Audit (dry-run)
# ---------------------------------------------------------------------------


def test_audit_pre_integration(pre_integration_db: Path) -> None:
    result = migration.audit_db(str(pre_integration_db))

    assert result["mode"] == "dry-run"
    assert "manifest_digest" in result
    assert len(result["manifest_digest"]) == 64  # SHA-256 hex

    # Integrity must pass
    assert result["integrity"] == "ok"

    counts = result["counts"]
    assert counts["total"] == 8  # 7 tasks + 1 epic
    assert counts["already_product"] == 2  # t_004 + epic t_e8
    assert counts["needs_migration"] == 6
    assert counts["epics"] == 1  # t_e8

    # Verify specific items
    items = {item["id"]: item for item in result["items"]}

    # t_004 is already a product task
    assert items["t_004"]["already_product"] is True
    assert not items["t_004"]["needs_migration"]

    # Legacy tasks need migration
    assert items["t_001"]["needs_migration"]
    assert items["t_001"]["inferred_v2_step"] == "development"  # assignee=developer

    assert items["t_002"]["inferred_v2_step"] == "architecture"  # assignee=architect
    assert items["t_003"]["inferred_v2_step"] == "review"  # status=review
    assert items["t_007"]["inferred_v2_step"] == "test"  # assignee=tester

    # t_005 has no assignee, status todo → backlog
    assert items["t_005"]["inferred_v2_step"] == "backlog"
    assert items["t_005"]["needs_migration"]

    # t_006 is done → done
    assert items["t_006"]["inferred_v2_step"] == "done"

    # Epic should be detected
    assert "t_e8" in result["epics"]


def test_audit_empty_db(empty_scratch_db: Path) -> None:
    result = migration.audit_db(str(empty_scratch_db))

    assert result["counts"]["total"] == 0
    assert result["counts"]["needs_migration"] == 0
    assert result["integrity"] == "ok"
    assert len(result["items"]) == 0


def test_audit_produces_stable_manifest(pre_integration_db: Path) -> None:
    """Repeated audits of the same DB must produce identical manifest digests."""
    first = migration.audit_db(str(pre_integration_db))
    second = migration.audit_db(str(pre_integration_db))

    assert first["manifest_digest"] == second["manifest_digest"]
    assert first["counts"] == second["counts"]


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def test_apply_pre_integration(pre_integration_db: Path, tmp_path: Path) -> None:
    recovery = tmp_path / "recovery"
    recovery.mkdir()

    result = migration.apply_db(
        str(pre_integration_db),
        recovery_root=str(recovery),
    )

    assert result["changed"] == 6  # 6 non-product non-epic tasks migrated
    assert result["receipt_path"]
    assert result["manifest_digest"]

    # Receipt must exist and be readable
    receipt_path = Path(result["receipt_path"])
    assert receipt_path.is_file()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "applied"
    assert receipt["changed"] == 6

    # Post-migration audit: all tasks should now be product
    post = migration.audit_db(str(pre_integration_db))
    assert post["counts"]["already_product"] == 8  # All 8 now
    assert post["counts"]["needs_migration"] == 0


def test_apply_idempotent(pre_integration_db: Path, tmp_path: Path) -> None:
    """Applying twice must be safe — second apply changes nothing."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()

    first = migration.apply_db(
        str(pre_integration_db),
        recovery_root=str(recovery),
    )
    assert first["changed"] == 6

    second = migration.apply_db(
        str(pre_integration_db),
        recovery_root=str(recovery),
    )
    assert second["changed"] == 0  # Idempotent — nothing to change


def test_apply_preserves_history(pre_integration_db: Path, tmp_path: Path) -> None:
    """Comments and events from pre-migration must survive."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()

    migration.apply_db(str(pre_integration_db), recovery_root=str(recovery))

    with sqlite3.connect(str(pre_integration_db)) as conn:
        conn.row_factory = sqlite3.Row

        # Comments survive
        comments = conn.execute(
            "SELECT * FROM task_comments WHERE task_id = ? ORDER BY id", ("t_001",)
        ).fetchall()
        assert len(comments) == 2
        assert comments[0]["body"] == "I can reproduce this on staging — the timeout is exactly 30s."
        assert comments[1]["author"] == "bob"

        # Events survive
        events = conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id", ("t_001",)
        ).fetchall()
        assert len(events) >= 3  # created + assigned + v2_migrated
        kinds = {e["kind"] for e in events}
        assert "created" in kinds
        assert "assigned" in kinds
        assert "v2_migrated" in kinds

        # t_004 (already product) must NOT get a v2_migrated event
        events_t4 = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ?", ("t_004",)
        ).fetchall()
        kinds_t4 = {e["kind"] for e in events_t4}
        assert "v2_migrated" not in kinds_t4


def test_apply_task_workflow_metadata(pre_integration_db: Path, tmp_path: Path) -> None:
    """Migrated tasks must have correct workflow_template_id and step."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()

    migration.apply_db(str(pre_integration_db), recovery_root=str(recovery))

    with sqlite3.connect(str(pre_integration_db)) as conn:
        conn.row_factory = sqlite3.Row

        # t_001: developer → development
        t1 = conn.execute("SELECT * FROM tasks WHERE id = ?", ("t_001",)).fetchone()
        assert t1["workflow_template_id"] == "product"
        assert t1["current_step_key"] == "development"

        # t_002: architect → architecture
        t2 = conn.execute("SELECT * FROM tasks WHERE id = ?", ("t_002",)).fetchone()
        assert t2["workflow_template_id"] == "product"
        assert t2["current_step_key"] == "architecture"

        # t_003: review → review
        t3 = conn.execute("SELECT * FROM tasks WHERE id = ?", ("t_003",)).fetchone()
        assert t3["workflow_template_id"] == "product"
        assert t3["current_step_key"] == "review"

        # t_004: already product — unchanged
        t4 = conn.execute("SELECT * FROM tasks WHERE id = ?", ("t_004",)).fetchone()
        assert t4["workflow_template_id"] == "product"
        assert t4["current_step_key"] == "development"

        # t_005: no assignee → backlog
        t5 = conn.execute("SELECT * FROM tasks WHERE id = ?", ("t_005",)).fetchone()
        assert t5["workflow_template_id"] == "product"
        assert t5["current_step_key"] == "backlog"

        # t_006: done → done (release_measure)
        t6 = conn.execute("SELECT * FROM tasks WHERE id = ?", ("t_006",)).fetchone()
        assert t6["workflow_template_id"] == "product"
        assert t6["current_step_key"] == "done"

        # t_007: tester → test
        t7 = conn.execute("SELECT * FROM tasks WHERE id = ?", ("t_007",)).fetchone()
        assert t7["workflow_template_id"] == "product"
        assert t7["current_step_key"] == "test"

        # Epic is untouched — epics are skipped by the migration
        t8 = conn.execute("SELECT * FROM tasks WHERE id = ?", ("t_e8",)).fetchone()
        assert t8["workflow_template_id"] is None  # Epics not migrated


def test_apply_rejects_active_run(tmp_path: Path) -> None:
    """Apply must fail if the DB has an active running task."""
    db = tmp_path / "active.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            """CREATE TABLE tasks (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT,
                assignee TEXT, status TEXT NOT NULL, created_at INTEGER NOT NULL,
                workflow_template_id TEXT, current_step_key TEXT,
                work_item_kind TEXT NOT NULL DEFAULT 'card',
                running INTEGER NOT NULL DEFAULT 0,
                blocked INTEGER NOT NULL DEFAULT 0,
                source_commit_required INTEGER NOT NULL DEFAULT 0,
                source_commit_forbidden INTEGER NOT NULL DEFAULT 0
            )"""
        )
        conn.execute("INSERT INTO board_governance (id, qualification_required) VALUES (1, 0)")
        conn.execute(
            "INSERT INTO tasks (id, title, assignee, status, created_at, running) "
            "VALUES ('t_run', 'Running task', 'developer', 'running', 1, 1)"
        )

    with pytest.raises(migration.MigrationBlocked, match="active running"):
        migration.apply_db(str(db), recovery_root=str(tmp_path / "recovery"))


def test_apply_zero_change_on_rerun(pre_integration_db: Path, tmp_path: Path) -> None:
    """After apply, re-running the audit must show zero needs_migration."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()

    migration.apply_db(str(pre_integration_db), recovery_root=str(recovery))

    # Re-audit: nothing left to migrate
    post = migration.audit_db(str(pre_integration_db))
    assert post["counts"]["needs_migration"] == 0

    # Re-apply: zero change
    second = migration.apply_db(str(pre_integration_db), recovery_root=str(recovery))
    assert second["changed"] == 0


def test_verify_pre_integration(pre_integration_db: Path) -> None:
    """verify_db returns an audit without modifying the DB."""
    before = migration.audit_db(str(pre_integration_db))
    verification = migration.verify_db(str(pre_integration_db))

    # verify_db is just an audit alias
    assert verification["counts"] == before["counts"]
    assert verification["integrity"] == "ok"


# ---------------------------------------------------------------------------
# Snapshot integrity
# ---------------------------------------------------------------------------


def test_snapshot_is_restorable(pre_integration_db: Path, tmp_path: Path) -> None:
    """The snapshot DB created during apply must pass integrity check."""
    recovery = tmp_path / "recovery_snap"
    recovery.mkdir()

    result = migration.apply_db(
        str(pre_integration_db),
        recovery_root=str(recovery),
    )

    receipt_path = Path(result["receipt_path"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    snapshot_db = Path(receipt["snapshot"]["db"])
    assert snapshot_db.is_file()

    # Verify the snapshot itself
    with sqlite3.connect(str(snapshot_db)) as conn:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        assert integrity == "ok"
        count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        assert count == 8  # Same number as the original


# ---------------------------------------------------------------------------
# API-level dry-run via the audit function exercised above.
# apply/verify/idempotent/zero-change/snapshot tests cover the full
# lifecycle without subprocess dependency.
# ---------------------------------------------------------------------------