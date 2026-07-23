"""Regression tests for Hermes MCP OAuth token snapshot/restore safety."""

from __future__ import annotations

import stat
import sys

import pytest

from tools.mcp_oauth import HermesTokenStorage


def _storage(tmp_path, monkeypatch) -> HermesTokenStorage:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return HermesTokenStorage("snapshot-server")


def _write(path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class TestHermesTokenStorageSnapshotRestore:
    def test_snapshot_empty_when_no_files(self, tmp_path, monkeypatch):
        storage = _storage(tmp_path, monkeypatch)

        assert storage.snapshot() == {}

    def test_snapshot_captures_existing_files(self, tmp_path, monkeypatch):
        storage = _storage(tmp_path, monkeypatch)
        _write(storage._tokens_path(), b"token-bytes")
        _write(storage._client_info_path(), b"client-bytes")

        assert storage.snapshot() == {
            storage._tokens_path().name: b"token-bytes",
            storage._client_info_path().name: b"client-bytes",
        }

    def test_snapshot_skips_missing_files(self, tmp_path, monkeypatch):
        storage = _storage(tmp_path, monkeypatch)
        _write(storage._tokens_path(), b"token-bytes")

        snapshot = storage.snapshot()

        assert snapshot == {storage._tokens_path().name: b"token-bytes"}
        assert storage._client_info_path().name not in snapshot
        assert storage._meta_path().name not in snapshot

    def test_restore_empty_snapshot_is_noop(self, tmp_path, monkeypatch):
        storage = _storage(tmp_path, monkeypatch)

        storage.restore({})

        assert not storage._tokens_path().exists()
        assert not storage._client_info_path().exists()
        assert not storage._meta_path().exists()

    def test_restore_writes_files_back(self, tmp_path, monkeypatch):
        storage = _storage(tmp_path, monkeypatch)
        original = {
            storage._tokens_path(): b"token-bytes",
            storage._client_info_path(): b"client-bytes",
            storage._meta_path(): b"meta-bytes",
        }
        for path, data in original.items():
            _write(path, data)
        snapshot = storage.snapshot()

        storage.remove()
        storage.restore(snapshot)

        for path, data in original.items():
            assert path.read_bytes() == data

    def test_restore_clears_stale_before_writing(self, tmp_path, monkeypatch):
        storage = _storage(tmp_path, monkeypatch)
        _write(storage._tokens_path(), b"original-token")
        snapshot = storage.snapshot()
        _write(storage._tokens_path(), b"partial-new-token")
        _write(storage._client_info_path(), b"stale-client")
        _write(storage._meta_path(), b"stale-meta")

        storage.restore(snapshot)

        assert storage._tokens_path().read_bytes() == b"original-token"
        assert not storage._client_info_path().exists()
        assert not storage._meta_path().exists()

    @pytest.mark.skipif(
        sys.platform.startswith("win"),
        reason="POSIX mode bits not enforced on Windows",
    )
    def test_restore_sets_user_only_permissions(self, tmp_path, monkeypatch):
        storage = _storage(tmp_path, monkeypatch)
        _write(storage._tokens_path(), b"token-bytes")
        snapshot = storage.snapshot()
        storage.remove()

        storage.restore(snapshot)

        mode = stat.S_IMODE(storage._tokens_path().stat().st_mode)
        assert mode == stat.S_IRUSR | stat.S_IWUSR

    def test_snapshot_restore_roundtrip_idempotent(self, tmp_path, monkeypatch):
        storage = _storage(tmp_path, monkeypatch)
        _write(storage._tokens_path(), b"token-bytes")
        _write(storage._client_info_path(), b"client-bytes")
        _write(storage._meta_path(), b"meta-bytes")

        snapshot = storage.snapshot()
        storage.restore(snapshot)

        assert storage.snapshot() == snapshot
