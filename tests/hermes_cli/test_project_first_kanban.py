"""Regression coverage for project-first Kanban contracts."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import projects_db as pdb


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    return root


def _project(root: Path, name: str = "Widget"):
    repo = root / name.lower()
    repo.mkdir()
    with pdb.connect_closing() as conn:
        pid = pdb.create_project(conn, name=name, primary_path=str(repo))
        return pdb.get_project(conn, pid)


def _filesystem_snapshot(root: Path) -> dict[str, bytes | None]:
    """Capture tree entries and file bytes without hashing away diagnostics."""
    return {
        str(path.relative_to(root)): None if path.is_dir() else path.read_bytes()
        for path in root.rglob("*")
    }


def test_board_stores_canonical_project_snapshot(isolated_home):
    project = _project(isolated_home)

    board = kb.create_board("widget", project_id=project.id, legacy_unscoped=False)

    assert board["project_id"] == project.id
    assert board["project_slug"] == project.slug
    assert board["project_name"] == project.name
    assert board["project_primary_path"] == project.primary_path
    assert board["default_workdir"] == project.primary_path
    assert kb.read_board_metadata("widget")["project_slug"] == project.slug


def test_new_named_board_requires_explicit_legacy_escape(isolated_home):
    with pytest.raises(ValueError, match="legacy_unscoped"):
        kb.create_board("unscoped", legacy_unscoped=False)

    board = kb.create_board("unscoped", legacy_unscoped=True)
    assert board["project_id"] is None


def test_scoped_board_rejects_mismatched_task_and_allows_legacy_escape(isolated_home):
    first = _project(isolated_home, "First")
    second = _project(isolated_home, "Second")
    kb.create_board("first", project_id=first.id, legacy_unscoped=False)

    with kb.connect(board="first") as conn:
        with pytest.raises(ValueError, match="TASK_PROJECT_MISMATCH"):
            kb.create_task(conn, title="wrong", board="first", project_id=second.id)

        task_id = kb.create_task(
            conn,
            title="legacy",
            board="first",
            legacy_unscoped=True,
        )
        assert kb.get_task(conn, task_id).project_id is None


def test_cross_profile_task_creation_uses_board_snapshot(isolated_home, monkeypatch, tmp_path):
    project = _project(isolated_home)
    kb.create_board("shared", project_id=project.id, legacy_unscoped=False)

    other_profile = tmp_path / "profiles" / "worker"
    other_profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(other_profile))

    with kb.connect(board="shared") as conn:
        task_id = kb.create_task(conn, title="from worker", board="shared")
        task = kb.get_task(conn, task_id)

    assert task.project_id == project.id
    assert task.workspace_path == str(Path(project.primary_path) / ".worktrees" / task_id)
    assert task.branch_name.startswith(f"{project.slug}/{task_id}-")


def test_audit_reports_per_board_status_and_required_codes(isolated_home):
    project = _project(isolated_home, "Audited")
    kb.create_board("scoped", project_id=project.id, legacy_unscoped=False)
    kb.create_board("legacy", legacy_unscoped=True)
    report = kb.audit_boards()

    scoped = next(board for board in report["boards"] if board["slug"] == "scoped")
    legacy = next(board for board in report["boards"] if board["slug"] == "legacy")
    assert scoped["status"] == "scoped"
    assert legacy["status"] == "legacy_unscoped"
    assert any(issue["code"] == "UNSCOPED_LEGACY" for issue in legacy["issues"])
    assert report["read_only"] is True


def test_binding_rejects_conflicting_existing_legacy_task(isolated_home):
    (isolated_home / "config.yaml").write_text(
        "database:\n  journal_mode: delete\n",
        encoding="utf-8",
    )
    first = _project(isolated_home, "First")
    second = _project(isolated_home, "Second")
    kb.create_board("legacy", legacy_unscoped=True)
    # A compatibility row created before board binding is allowed to exist.
    conn = kb.connect(board="default")
    try:
        db_path = kb.kanban_db_path("default")
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert not Path(f"{db_path}-wal").exists()
        assert not Path(f"{db_path}-shm").exists()
        task_id = kb.create_task(conn, title="old linked work", project_id=first.id)
        assert task_id
        before = _filesystem_snapshot(isolated_home)

        with pytest.raises(kb.BoardProjectError, match="TASK_PROJECT_MISMATCH"):
            kb.bind_board_project("default", project_ref=second.id)
        assert before == _filesystem_snapshot(isolated_home)
    finally:
        conn.close()


def test_board_project_binding_rejects_duplicate_project(isolated_home):
    project = _project(isolated_home)
    kb.create_board("one", project_id=project.id, legacy_unscoped=False)
    kb.create_board("two", legacy_unscoped=True)

    with pytest.raises(ValueError, match="PROJECT_ALREADY_BOUND"):
        kb.bind_board_project("two", project.id)


def test_boards_audit_is_read_only_and_reports_malformed_metadata(isolated_home):
    kb.create_board("healthy", legacy_unscoped=True)
    metadata = kb.board_metadata_path("broken")
    metadata.parent.mkdir(parents=True, exist_ok=True)
    metadata.write_text("not json", encoding="utf-8")
    # This is intentionally unguarded: DELETE fallback and WAL runtimes must
    # both leave the isolated home tree and every file byte unchanged.
    before = _filesystem_snapshot(isolated_home)

    report = kb.audit_boards()

    after = _filesystem_snapshot(isolated_home)
    assert before == after
    assert any(issue["code"] == "MALFORMED_BOARD_METADATA" for issue in report["issues"])
    assert report["read_only"] is True


@pytest.mark.requires_wal
def test_boards_audit_reads_uncheckpointed_wal_without_source_sidecars(isolated_home):
    project = _project(isolated_home, "WalAudited")
    kb.create_board("scoped", project_id=project.id, legacy_unscoped=False)
    db_path = kb.kanban_db_path("scoped")
    writer = sqlite3.connect(db_path, isolation_level=None)
    try:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        task_id = kb.create_task(writer, title="WAL task", board="scoped")
        writer.execute(
            "UPDATE tasks SET project_id = ? WHERE id = ?",
            ("different-project", task_id),
        )
        shm_path = Path(f"{db_path}-shm")
        assert shm_path.exists()
        # Simulate a WAL database copied while its shared-memory index is not
        # available. The audit must read the WAL through its private snapshot,
        # not recreate this source-side index.
        shm_path.unlink()
        before = _filesystem_snapshot(isolated_home)

        report = kb.audit_boards()

        after = _filesystem_snapshot(isolated_home)
        assert before == after
        assert any(
            issue["code"] == "TASK_PROJECT_MISMATCH" and issue["task_id"] == task_id
            for issue in report["issues"]
        )
    finally:
        writer.close()


def test_known_assignees_include_global_profile_description(isolated_home):
    profile = isolated_home / "profiles" / "writer"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("model:\n  name: test\n", encoding="utf-8")
    (profile / "profile.yaml").write_text(
        json.dumps({"description": "Writes clear release notes."}), encoding="utf-8"
    )

    with kb.connect() as conn:
        assignees = kb.known_assignees(conn)

    writer = next(row for row in assignees if row["name"] == "writer")
    assert writer["description"] == "Writes clear release notes."
    assert writer["has_description"] is True
