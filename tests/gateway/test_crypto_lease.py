"""Tests for the inter-process Matrix crypto-store lease primitive.

``acquire_crypto_lease`` / ``release_crypto_lease`` exist in ``gateway.status``
to keep two OlmMachine instances from racing on the same crypto DB.  Unlike the
credential-scoped locks, the crypto lease is keyed to the resolved store path +
account/device identity (not process-level HERMES_HOME) and is fail-closed: on
success it returns the winning record; on any unconfirmable acquisition it
returns ``(False, existing)``.
"""

import json
import os

import pytest

from gateway import status


STORE_PATH = "/tmp/hermes-crypto/account.db"
IDENTITY = f"{STORE_PATH}::@alice:example.org::ABCDEFGHIJ"


def _lock_path():
    return status._get_scope_lock_path(status._CRYPTO_LEASE_SCOPE, IDENTITY)


class TestCryptoLease:
    def test_first_acquire_returns_own_record(self, tmp_path, monkeypatch):
        """A fresh acquire wins the lease and returns this process's record."""
        monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "locks"))

        acquired, record = status.acquire_crypto_lease(STORE_PATH, IDENTITY)

        assert acquired is True
        assert record is not None
        assert record["pid"] == os.getpid()
        assert record["store_path"] == STORE_PATH
        assert record["scope"] == status._CRYPTO_LEASE_SCOPE
        # The lease record must actually be on disk at the scope-specific path.
        lock_path = _lock_path()
        assert lock_path.exists()
        assert json.loads(lock_path.read_text())["pid"] == os.getpid()

    def test_uses_distinct_scope_namespace(self, tmp_path, monkeypatch):
        """The crypto lease file must not collide with credential scoped locks."""
        monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "locks"))

        status.acquire_crypto_lease(STORE_PATH, IDENTITY)

        lock_path = _lock_path()
        assert lock_path.name.startswith(status._CRYPTO_LEASE_SCOPE + "-")
        # A scoped lock over the same identity lives under its own namespace.
        scoped_path = status._get_scope_lock_path("telegram-bot-token", IDENTITY)
        assert scoped_path.name != lock_path.name

    def test_reclaims_stale_dead_pid(self, tmp_path, monkeypatch):
        """A lease left by a dead PID is reclaimed and returns our record."""
        monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "locks"))
        lock_path = _lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(json.dumps({
            "pid": 99999,
            "start_time": 123,
            "kind": "hermes-gateway",
        }))

        monkeypatch.setattr(status, "_pid_exists", lambda pid: False)

        acquired, record = status.acquire_crypto_lease(STORE_PATH, IDENTITY)

        assert acquired is True
        assert record is not None
        assert record["pid"] == os.getpid()
        assert json.loads(lock_path.read_text())["pid"] == os.getpid()

    def test_reclaims_corrupt_file(self, tmp_path, monkeypatch):
        """A corrupt / empty lease file is treated as stale and reclaimed."""
        monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "locks"))
        lock_path = _lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("{ not valid json")

        acquired, record = status.acquire_crypto_lease(STORE_PATH, IDENTITY)

        assert acquired is True
        assert record is not None
        assert record["pid"] == os.getpid()

    def test_reclaims_empty_file(self, tmp_path, monkeypatch):
        """A zero-byte lease file (writer killed between O_EXCL and json.dump)
        is reclaimed atomically and this process wins the lease."""
        monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "locks"))
        lock_path = _lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("")

        acquired, record = status.acquire_crypto_lease(STORE_PATH, IDENTITY)

        assert acquired is True
        assert record is not None
        assert record["pid"] == os.getpid()
        assert json.loads(lock_path.read_text())["pid"] == os.getpid()

    def test_empty_file_race_second_acquirer_loses(self, tmp_path, monkeypatch):
        """A concurrent writer caught between O_EXCL and json.dump() must not
        have its 0-byte file unlinked by this reader.  The reader's tombstone
        os.replace() is atomic; when it hits FileNotFoundError it falls through
        to O_EXCL and loses to the winner's fresh lock (the old unlink() would
        have deleted the winner's file and let both processes win)."""
        monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "locks"))
        lock_path = _lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("")

        winner_record = {
            "pid": 424242,
            "start_time": 456,
            "kind": "hermes-gateway",
            "scope": status._CRYPTO_LEASE_SCOPE,
        }
        real_replace = os.replace

        def racing_replace(src, dst, *args, **kwargs):
            if str(src) == str(lock_path):
                # Simulate the winner completing removal + O_EXCL create
                # between our empty-file check and our reclaim attempt.
                lock_path.write_text(json.dumps(winner_record))
                raise FileNotFoundError(2, "No such file or directory", str(src))
            return real_replace(src, dst, *args, **kwargs)

        monkeypatch.setattr(status.os, "replace", racing_replace)

        acquired, existing = status.acquire_crypto_lease(STORE_PATH, IDENTITY)

        assert acquired is False
        assert existing is not None
        assert existing["pid"] == 424242
        # The winner's fresh lock must remain untouched on disk.
        assert json.loads(lock_path.read_text())["pid"] == 424242

    def test_rejects_live_foreign_process(self, tmp_path, monkeypatch):
        """A lease held by another live PID is rejected fail-closed (False)."""
        monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "locks"))
        lock_path = _lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(json.dumps({
            "pid": 99999,
            "start_time": 123,
            "kind": "hermes-gateway",
        }))

        monkeypatch.setattr(status, "_pid_exists", lambda pid: True)
        monkeypatch.setattr(status, "_get_process_start_time", lambda pid: 123)
        monkeypatch.setattr(status, "_looks_like_gateway_process", lambda pid: True)

        acquired, existing = status.acquire_crypto_lease(STORE_PATH, IDENTITY)

        assert acquired is False
        assert existing is not None
        assert existing["pid"] == 99999
        # The foreign holder's lease must remain untouched on disk.
        assert json.loads(lock_path.read_text())["pid"] == 99999

    def test_self_reacquires_same_pid(self, tmp_path, monkeypatch):
        """Same PID always self-reacquires, returning the existing record."""
        monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "locks"))
        lock_path = _lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(json.dumps({
            "pid": os.getpid(),
            "start_time": None,
            "kind": "hermes-gateway",
        }))

        acquired, existing = status.acquire_crypto_lease(STORE_PATH, IDENTITY)

        assert acquired is True
        assert existing is not None
        assert existing["pid"] == os.getpid()
        assert json.loads(lock_path.read_text())["pid"] == os.getpid()

    def test_release_by_owner_removes_file(self, tmp_path, monkeypatch):
        """The owning process releasing its lease removes the lock file."""
        monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "locks"))

        acquired, _ = status.acquire_crypto_lease(STORE_PATH, IDENTITY)
        assert acquired is True

        status.release_crypto_lease(STORE_PATH, IDENTITY)

        assert not _lock_path().exists()

    def test_release_by_non_owner_is_noop(self, tmp_path, monkeypatch):
        """A non-owning process releasing the lease is a no-op."""
        monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "locks"))
        lock_path = _lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(json.dumps({
            "pid": 99999,
            "start_time": 123,
            "kind": "hermes-gateway",
        }))

        status.release_crypto_lease(STORE_PATH, IDENTITY)

        assert lock_path.exists()
        assert json.loads(lock_path.read_text())["pid"] == 99999