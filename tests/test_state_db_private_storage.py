import os
import stat
import sys
from pathlib import Path

import pytest

import hermes_state
from hermes_state import SessionDB


pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX owner/mode/link hardening contract",
)


def test_session_db_privatizes_parent_db_wal_and_shm(tmp_path):
    parent = tmp_path / "profile"
    parent.mkdir(mode=0o755)
    db_path = parent / "state.db"
    previous_umask = os.umask(0o022)
    db = None
    try:
        db = SessionDB(db_path)
        db.create_session("private-storage", source="test")
        db.append_message("private-storage", "user", "private transcript")

        assert stat.S_IMODE(parent.stat().st_mode) == 0o700
        for artifact in (
            db_path,
            db_path.with_name(db_path.name + "-wal"),
            db_path.with_name(db_path.name + "-shm"),
        ):
            assert artifact.exists(), artifact
            info = artifact.lstat()
            assert not artifact.is_symlink()
            assert stat.S_ISREG(info.st_mode)
            assert info.st_uid == os.geteuid()
            assert info.st_nlink == 1
            assert stat.S_IMODE(info.st_mode) == 0o600
    finally:
        if db is not None:
            db.close()
        os.umask(previous_umask)


def test_session_db_rejects_symlink_and_hardlink_before_sqlite_open(tmp_path):
    parent = tmp_path / "profile"
    parent.mkdir(mode=0o700)

    external = tmp_path / "external.db"
    external.write_bytes(b"sentinel")
    symlink_db = parent / "state.db"
    symlink_db.symlink_to(external)
    with pytest.raises(OSError, match="unsafe private SQLite"):
        SessionDB(symlink_db)
    assert external.read_bytes() == b"sentinel"

    symlink_db.unlink()
    hardlink_source = tmp_path / "hardlink-source.db"
    hardlink_source.write_bytes(b"sentinel-hardlink")
    os.link(hardlink_source, symlink_db)
    with pytest.raises(OSError, match="unsafe private SQLite"):
        SessionDB(symlink_db)
    assert hardlink_source.read_bytes() == b"sentinel-hardlink"


def test_malformed_backups_are_private_and_retention_bounded(tmp_path):
    db_path = tmp_path / "state.db"
    db_path.write_bytes(b"SQLite forensic bytes")
    os.chmod(db_path, 0o644)
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + suffix)
        sidecar.write_bytes(("sidecar" + suffix).encode())
        os.chmod(sidecar, 0o644)

    for index in range(5):
        old = tmp_path / f"state.db.malformed-backup-20000101_00000{index}"
        old.write_bytes(b"old forensic copy")
        os.chmod(old, 0o644)

    backup = hermes_state._backup_db_file(db_path)
    assert backup is not None
    assert backup.exists()
    for artifact in (
        backup,
        backup.with_name(backup.name + "-wal"),
        backup.with_name(backup.name + "-shm"),
    ):
        info = artifact.lstat()
        assert stat.S_ISREG(info.st_mode)
        assert info.st_nlink == 1
        assert stat.S_IMODE(info.st_mode) == 0o600

    bases = [
        path
        for path in tmp_path.glob("state.db.malformed-backup-*")
        if not path.name.endswith(("-wal", "-shm"))
    ]
    assert len(bases) <= 3


def test_read_only_session_db_does_not_mutate_private_storage(tmp_path, monkeypatch):
    parent = tmp_path / "profile"
    parent.mkdir(mode=0o700)
    db_path = parent / "state.db"

    writer = SessionDB(db_path)
    try:
        writer.create_session("read-only-observer", source="test")
        writer.append_message("read-only-observer", "user", "private transcript")
    finally:
        writer.close()

    os.chmod(parent, 0o700)
    for artifact in hermes_state._sqlite_artifact_paths(db_path):
        if artifact.exists():
            os.chmod(artifact, 0o600)

    def snapshot():
        result = {}
        for path in sorted(parent.iterdir(), key=lambda item: item.name):
            info = path.lstat()
            result[path.name] = (
                info.st_ino,
                stat.S_IMODE(info.st_mode),
                info.st_size,
                info.st_mtime_ns,
            )
        return result

    before = snapshot()

    def reject_chmod(*_args, **_kwargs):
        raise AssertionError("read-only SessionDB must not call chmod")

    def reject_mkdir(*_args, **_kwargs):
        raise AssertionError("read-only SessionDB must not call mkdir")

    monkeypatch.setattr(os, "chmod", reject_chmod)
    monkeypatch.setattr(Path, "mkdir", reject_mkdir)

    observer = SessionDB(db_path, read_only=True)
    try:
        assert observer.get_session("read-only-observer") is not None
    finally:
        observer.close()

    assert snapshot() == before


def test_read_only_session_db_fails_closed_when_wal_has_no_shm(tmp_path):
    parent = tmp_path / "profile"
    parent.mkdir(mode=0o700)
    db_path = parent / "state.db"
    wal_path = db_path.with_name(db_path.name + "-wal")
    shm_path = db_path.with_name(db_path.name + "-shm")

    writer = SessionDB(db_path)
    try:
        writer.create_session("wal-without-shm", source="test")
        writer.append_message("wal-without-shm", "user", "committed in WAL")
        db_bytes = db_path.read_bytes()
        wal_bytes = wal_path.read_bytes()
    finally:
        writer.close()

    db_path.write_bytes(db_bytes)
    wal_path.write_bytes(wal_bytes)
    os.chmod(db_path, 0o600)
    os.chmod(wal_path, 0o600)
    shm_path.unlink(missing_ok=True)

    def snapshot():
        parent_info = parent.lstat()
        return (
            stat.S_IMODE(parent_info.st_mode),
            parent_info.st_mtime_ns,
            {
                path.name: (
                    path.lstat().st_ino,
                    stat.S_IMODE(path.lstat().st_mode),
                    path.lstat().st_size,
                    path.lstat().st_mtime_ns,
                )
                for path in parent.iterdir()
            },
        )

    before = snapshot()
    observer = None
    error = None
    try:
        observer = SessionDB(db_path, read_only=True)
        observer.get_session("wal-without-shm")
    except OSError as exc:
        error = exc
    finally:
        if observer is not None:
            observer.close()

    assert snapshot() == before
    assert error is not None
    assert "read-only SQLite WAL requires an existing SHM sidecar" in str(error)
