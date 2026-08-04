from __future__ import annotations

import os
import threading

import pytest

from gateway import canonical_writer_activation as activation
from gateway import canonical_writer_lifecycle_lock as lifecycle
from scripts.canary import receipt_driven_release_gc as gc
from scripts.canary import writer_release


def test_shared_lock_surface_is_used_by_activation_release_and_gc():
    assert activation.ACTIVATION_LOCK_PATH == lifecycle.HOST_LIFECYCLE_LOCK_PATH
    assert (
        writer_release.host_release_lifecycle_lock
        is lifecycle.host_release_lifecycle_lock
    )
    assert gc.host_release_lifecycle_lock is lifecycle.host_release_lifecycle_lock


def test_lifecycle_lock_is_reentrant_but_excludes_concurrent_thread(tmp_path):
    path = tmp_path / "lifecycle.lock"
    entered = threading.Event()
    release = threading.Event()
    failures: list[BaseException] = []

    def hold() -> None:
        try:
            with lifecycle._lifecycle_file_lock(
                path,
                owner_uid=os.geteuid(),
                owner_gid=os.getegid(),
                validate_parent=False,
            ) as outer:
                with lifecycle._lifecycle_file_lock(
                    path,
                    owner_uid=os.geteuid(),
                    owner_gid=os.getegid(),
                    validate_parent=False,
                ) as inner:
                    assert inner == outer
                    entered.set()
                    release.wait(timeout=5)
        except BaseException as exc:  # pragma: no cover - surfaced below
            failures.append(exc)
            entered.set()

    thread = threading.Thread(target=hold)
    thread.start()
    assert entered.wait(timeout=5)
    assert not failures
    with pytest.raises(RuntimeError, match="another writer release lifecycle"):
        with lifecycle._lifecycle_file_lock(
            path,
            owner_uid=os.geteuid(),
            owner_gid=os.getegid(),
            validate_parent=False,
        ):
            pass
    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert not failures
