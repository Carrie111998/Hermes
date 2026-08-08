from __future__ import annotations

import json
import sqlite3
import time
import zipfile
from pathlib import Path

import pytest

from hermes_cli.backup import (
    BackupInProgressError,
    _atomic_output_path,
    _backup_operation_lock,
    _safe_copy_db,
    _should_exclude,
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


def test_chrome_runtime_caches_are_excluded_but_profile_state_is_preserved() -> None:
    assert _should_exclude(Path("chrome-debug/Default/GPUCache/cache.db"))
    assert _should_exclude(Path("chrome-debug/Default/Code Cache/js/index"))
    assert _should_exclude(Path("chrome-debug/ShaderCache/GPUCache/data_0"))
    assert _should_exclude(
        Path("profiles/coder/chrome-debug/Default/GPUCache/cache.db")
    )
    assert not _should_exclude(Path("chrome-debug/Default/Cookies"))
    assert not _should_exclude(Path("chrome-debug/Default/Login Data"))
    assert not _should_exclude(
        Path("profiles/coder/chrome-debug/Default/Login Data")
    )


def test_locked_database_stall_is_bounded_and_cleans_destination(tmp_path) -> None:
    src = tmp_path / "locked-cache.db"
    dst = tmp_path / "copy.db"
    holder = sqlite3.connect(src, timeout=0)
    holder.execute("CREATE TABLE cache (key TEXT)")
    holder.commit()
    holder.execute("BEGIN EXCLUSIVE")

    started = time.monotonic()
    try:
        result = _safe_copy_db(src, dst, timeout_seconds=0.1)
    finally:
        holder.rollback()
        holder.close()

    assert result is False
    assert time.monotonic() - started < 2.0
    assert not dst.exists()


def test_nonadvancing_sqlite_ok_progress_is_bounded(tmp_path, monkeypatch) -> None:
    from hermes_cli import backup

    src = tmp_path / "growing.db"
    dst = tmp_path / "copy.db"
    src.write_bytes(b"source")
    dst.write_bytes(b"partial")

    class SourceConnection:
        def backup(self, _destination, *, progress, **_kwargs) -> None:
            until = time.monotonic() + 0.2
            while time.monotonic() < until:
                progress(sqlite3.SQLITE_OK, 10, 10)
                time.sleep(0.01)

        def close(self) -> None:
            pass

    class DestinationConnection:
        def close(self) -> None:
            pass

    connections = iter((SourceConnection(), DestinationConnection()))
    monkeypatch.setattr(
        backup.sqlite3,
        "connect",
        lambda *_args, **_kwargs: next(connections),
    )

    assert _safe_copy_db(src, dst, timeout_seconds=0.05) is False
    assert not dst.exists()


def test_locked_chrome_cache_is_skipped_without_losing_profile_state(tmp_path) -> None:
    home = tmp_path / ".hermes"
    profile = home / "profiles" / "coder" / "chrome-debug" / "Default"
    gpu_cache = profile / "GPUCache"
    gpu_cache.mkdir(parents=True)
    (home / "config.yaml").write_text("model: test\n", encoding="utf-8")
    (profile / "Cookies").write_bytes(b"persistent-session-state")

    cache_db = gpu_cache / "cache.db"
    holder = sqlite3.connect(cache_db, timeout=0)
    holder.execute("CREATE TABLE cache (key TEXT)")
    holder.commit()
    holder.execute("BEGIN EXCLUSIVE")

    archive = tmp_path / "pre-update.zip"
    started = time.monotonic()
    try:
        result = _write_full_zip_backup(archive, home)
    finally:
        holder.rollback()
        holder.close()

    assert result == archive
    assert time.monotonic() - started < 2.0
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
    assert "profiles/coder/chrome-debug/Default/Cookies" in names
    assert "profiles/coder/chrome-debug/Default/GPUCache/cache.db" not in names
