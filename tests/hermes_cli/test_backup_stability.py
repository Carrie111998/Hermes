from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from hermes_cli.backup import (
    BackupInProgressError,
    _atomic_output_path,
    _backup_operation_lock,
    _write_full_zip_backup,
    create_quick_snapshot,
    list_quick_snapshots,
)


def test_backup_lock_rejects_a_second_operation(tmp_path) -> None:
    home = tmp_path / ".hermes"
    home.mkdir()

    with _backup_operation_lock(home):
        with pytest.raises(BackupInProgressError):
            with _backup_operation_lock(home, timeout_seconds=0):
                raise AssertionError("second backup unexpectedly acquired the lock")


def test_atomic_output_publishes_only_after_clean_close(tmp_path) -> None:
    final = tmp_path / "backup.zip"
    final.write_bytes(b"previous")

    with _atomic_output_path(final) as partial:
        partial.write_bytes(b"complete")
        assert final.read_bytes() == b"previous"

    assert final.read_bytes() == b"complete"
    assert not partial.exists()


def test_atomic_output_keeps_previous_file_after_failure(tmp_path) -> None:
    final = tmp_path / "backup.zip"
    final.write_bytes(b"previous")

    with pytest.raises(RuntimeError):
        with _atomic_output_path(final) as partial:
            partial.write_bytes(b"incomplete")
            raise RuntimeError("compression failed")

    assert final.read_bytes() == b"previous"
    assert not partial.exists()


def test_quick_snapshot_is_published_with_manifest(tmp_path, monkeypatch) -> None:
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    published: list[tuple[Path, Path]] = []

    from hermes_cli import backup

    real_replace = backup.os.replace

    def replace(source, destination) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        if destination_path.parent == home / "state-snapshots":
            assert source_path.name.endswith(".partial")
            assert (source_path / "manifest.json").is_file()
            assert not destination_path.exists()
            published.append((source_path, destination_path))
        real_replace(source, destination)

    monkeypatch.setattr(backup.os, "replace", replace)
    snapshot_id = create_quick_snapshot(hermes_home=home)

    assert snapshot_id is not None
    assert len(published) == 1
    manifest = json.loads(
        (home / "state-snapshots" / snapshot_id / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["id"] == snapshot_id
    assert manifest["files"] == {"config.yaml": 10}


def test_quick_snapshot_listing_ignores_partial_directories(tmp_path) -> None:
    home = tmp_path / ".hermes"
    partial = home / "state-snapshots" / ".unfinished.1.partial"
    partial.mkdir(parents=True)
    (partial / "manifest.json").write_text('{"id":"unfinished"}', encoding="utf-8")

    assert list_quick_snapshots(hermes_home=home) == []


def test_failed_automatic_backup_preserves_previous_archive(tmp_path, monkeypatch) -> None:
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "state.db").write_bytes(b"not-a-database")
    archive = tmp_path / "automatic.zip"
    archive.write_bytes(b"previous-valid-backup")

    monkeypatch.setattr("hermes_cli.backup._safe_copy_db", lambda _src, _dst: False)

    assert _write_full_zip_backup(archive, home) is None
    assert archive.read_bytes() == b"previous-valid-backup"
    assert list(tmp_path.glob(".*.partial")) == []


def test_full_backup_excludes_retained_quick_snapshots_and_keeps_nested_skill_repos(
    tmp_path, monkeypatch
) -> None:
    """The automatic full-zip walk must agree with ``_should_exclude``.

    Retained quick snapshots are backup artifacts with their own keep-N policy:
    re-shipping them can inflate a small rollback archive by gigabytes and can
    fail on historical SQLite files that are intentionally read-only. Nested
    skill repos that merely share the ``hermes-agent`` name are user content and
    must survive -- only the root checkout is excluded.
    """
    home = tmp_path / ".hermes"
    home.mkdir()

    live_db = home / "state.db"
    with sqlite3.connect(live_db) as conn:
        conn.execute("create table live_state (value text)")
        conn.execute("insert into live_state values ('preserved')")

    retained_db = home / "state-snapshots" / "old" / "state.db"
    retained_db.parent.mkdir(parents=True)
    retained_db.write_bytes(b"old snapshot that must not be reopened")

    retained_profile_db = (
        home / "profiles" / "coder" / "state-snapshots" / "old" / "state.db"
    )
    retained_profile_db.parent.mkdir(parents=True)
    retained_profile_db.write_bytes(b"old profile snapshot that must not be reopened")
    profile_config = home / "profiles" / "coder" / "config.yaml"
    profile_config.write_text("model: test\n", encoding="utf-8")

    nested_note = home / "skills" / "demo" / "state-snapshots" / "notes.txt"
    nested_note.parent.mkdir(parents=True)
    nested_note.write_text("nested snapshot dir", encoding="utf-8")

    nested_repo = home / "skills" / "autonomous-ai-agents" / "hermes-agent" / "README.md"
    nested_repo.parent.mkdir(parents=True)
    nested_repo.write_text("skill repo content", encoding="utf-8")

    root_repo = home / "hermes-agent" / "run_agent.py"
    root_repo.parent.mkdir(parents=True)
    root_repo.write_text("the codebase itself", encoding="utf-8")

    from hermes_cli import backup

    real_safe_copy = backup._safe_copy_db

    def safe_copy(src: Path, dst: Path) -> bool:
        # Tripwire: retained snapshot DBs must never be reopened by the walk.
        if src in {retained_db, retained_profile_db}:
            return False
        return real_safe_copy(src, dst)

    monkeypatch.setattr(backup, "_safe_copy_db", safe_copy)
    archive = tmp_path / "automatic.zip"

    assert _write_full_zip_backup(archive, home) == archive
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())

    assert "state.db" in names
    assert "profiles/coder/config.yaml" in names

    # Retained quick snapshots never re-ship, at the default root or per profile.
    assert not any(name.startswith("state-snapshots/") for name in names)
    assert not any(
        name.startswith("profiles/coder/state-snapshots/") for name in names
    )
    # ``state-snapshots`` is in ``_EXCLUDED_DIRS``, so it is pruned at any depth.
    assert "skills/demo/state-snapshots/notes.txt" not in names

    # The root checkout is excluded, but a nested skill repo of the same name is
    # user content: this walk must not prune it where ``_should_exclude`` keeps it.
    assert "hermes-agent/run_agent.py" not in names
    assert "skills/autonomous-ai-agents/hermes-agent/README.md" in names

    # Tripwire BOTH predicates: the archive assertions above pin the full-zip
    # walk; pin the incremental path's ``_should_exclude`` directly so the two
    # implementations cannot silently drift apart.
    assert backup._should_exclude(Path("hermes-agent/run_agent.py")) is True
    assert backup._should_exclude(Path("skills/x/hermes-agent/y")) is False
