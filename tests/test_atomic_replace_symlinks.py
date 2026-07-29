"""Regression tests for GitHub #16743 — atomic writes must preserve symlinks.

``os.replace(tmp, target)`` replaces whatever exists at ``target`` — including
symlinks, which it swaps for a regular file.  Managed deployments that
symlink ``~/.hermes/config.yaml`` (and other state files) to a git-tracked
profile package were silently detached on every config write.

The fix: a shared ``atomic_replace`` helper in ``utils.py`` that resolves the
target through ``os.path.realpath`` when it is a symlink, so the real file is
overwritten in-place while the symlink survives.  All atomic-write sites in
the codebase were migrated to the helper; these tests pin that invariant.
"""
from __future__ import annotations

import errno
import json
import multiprocessing
import os
import sys
from pathlib import Path

import pytest
import yaml

# Ensure the repo root is importable when running via `pytest tests/...`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from utils import (
    atomic_json_write,
    atomic_replace,
    atomic_roundtrip_yaml_update,
    atomic_yaml_write,
)


# ─── Direct helper ────────────────────────────────────────────────────────────


def _write_tmp(dir_: Path, content: str) -> Path:
    tmp = dir_ / ".src.tmp"
    tmp.write_text(content, encoding="utf-8")
    return tmp


def _atomic_write_worker(path: str, worker_id: int, iterations: int) -> None:
    """Exercise the real atomic writer from a spawned Windows process."""
    for sequence in range(iterations):
        atomic_json_write(path, {"worker": worker_id, "sequence": sequence})


def test_atomic_replace_preserves_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real.yaml"
    link = tmp_path / "link.yaml"
    real.write_text("original\n", encoding="utf-8")
    link.symlink_to(real)

    tmp = _write_tmp(tmp_path, "updated\n")
    returned = atomic_replace(tmp, link)

    assert link.is_symlink(), "symlink must not be replaced with a regular file"
    assert real.read_text(encoding="utf-8") == "updated\n"
    assert Path(returned) == real
    # Follow the symlink — same content.
    assert link.read_text(encoding="utf-8") == "updated\n"


def test_atomic_replace_regular_file(tmp_path: Path) -> None:
    target = tmp_path / "plain.yaml"
    target.write_text("old\n", encoding="utf-8")

    tmp = _write_tmp(tmp_path, "fresh\n")
    returned = atomic_replace(tmp, target)

    assert Path(returned) == target
    assert target.read_text(encoding="utf-8") == "fresh\n"
    assert not target.is_symlink()


def test_atomic_replace_first_time_create(tmp_path: Path) -> None:
    target = tmp_path / "new.yaml"
    assert not target.exists()

    tmp = _write_tmp(tmp_path, "brand new\n")
    returned = atomic_replace(tmp, target)

    assert Path(returned) == target
    assert target.read_text(encoding="utf-8") == "brand new\n"


def test_atomic_replace_accepts_pathlike_and_str(tmp_path: Path) -> None:
    target = tmp_path / "dual.json"
    target.write_text("{}", encoding="utf-8")

    # str inputs
    tmp1 = _write_tmp(tmp_path, "1")
    atomic_replace(str(tmp1), str(target))
    assert target.read_text(encoding="utf-8") == "1"

    # Path inputs
    tmp2 = _write_tmp(tmp_path, "2")
    atomic_replace(tmp2, target)
    assert target.read_text(encoding="utf-8") == "2"


# ─── atomic_json_write / atomic_yaml_write wiring ──────────────────────────


def test_atomic_json_write_preserves_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    link = tmp_path / "link.json"
    real.write_text("{}", encoding="utf-8")
    link.symlink_to(real)

    atomic_json_write(link, {"hello": "world"})

    assert link.is_symlink()
    loaded = json.loads(real.read_text(encoding="utf-8"))
    assert loaded == {"hello": "world"}


def test_atomic_yaml_write_preserves_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real.yaml"
    link = tmp_path / "link.yaml"
    real.write_text("placeholder: true\n", encoding="utf-8")
    link.symlink_to(real)

    atomic_yaml_write(link, {"model": {"provider": "openrouter"}})

    assert link.is_symlink()
    data = yaml.safe_load(real.read_text(encoding="utf-8"))
    assert data == {"model": {"provider": "openrouter"}}


def test_atomic_json_write_preserves_symlink_permissions(tmp_path: Path) -> None:
    """Symlinked targets keep the real file's permission bits."""
    if os.name != "posix":
        pytest.skip("POSIX-only")

    real = tmp_path / "real.json"
    link = tmp_path / "link.json"
    real.write_text("{}", encoding="utf-8")
    os.chmod(real, 0o644)
    link.symlink_to(real)

    atomic_json_write(link, {"x": 1})

    import stat as _stat
    mode = _stat.S_IMODE(real.stat().st_mode)
    assert mode == 0o644, f"permissions drifted after symlinked write: {oct(mode)}"


def test_atomic_yaml_write_restores_owner_on_real_symlink_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Config writes through symlinks must restore the real file's owner.

    Docker support hit this when a root-run setup wizard rewrote a
    hermes-owned /opt/data/config.yaml via atomic replace, leaving the new file
    root-owned. The test forces a preserved uid/gid so it does not need root.
    """
    if os.name != "posix":
        pytest.skip("POSIX-only")

    real = tmp_path / "config.yaml"
    link = tmp_path / "link.yaml"
    real.write_text("old: true\n", encoding="utf-8")
    link.symlink_to(real)

    chown_calls: list[tuple[Path, int, int]] = []
    monkeypatch.setattr("utils._preserve_file_owner", lambda _path: (123, 456))
    monkeypatch.setattr(
        "utils.os.chown",
        lambda path, uid, gid: chown_calls.append((Path(path), uid, gid)),
    )

    atomic_yaml_write(link, {"new": True})

    assert chown_calls == [(real, 123, 456)]


def test_atomic_json_write_restores_owner_with_explicit_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name != "posix":
        pytest.skip("POSIX-only")

    target = tmp_path / "state.json"
    target.write_text("{}", encoding="utf-8")

    chown_calls: list[tuple[Path, int, int]] = []
    monkeypatch.setattr("utils._preserve_file_owner", lambda _path: (234, 567))
    monkeypatch.setattr(
        "utils.os.chown",
        lambda path, uid, gid: chown_calls.append((Path(path), uid, gid)),
    )

    atomic_json_write(target, {"api_key": "secret"}, mode=0o600)

    assert chown_calls == [(target, 234, 567)]
    assert target.stat().st_mode & 0o777 == 0o600


def test_atomic_roundtrip_yaml_update_restores_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name != "posix":
        pytest.skip("POSIX-only")

    target = tmp_path / "config.yaml"
    target.write_text("model:\n  provider: openrouter\n", encoding="utf-8")

    chown_calls: list[tuple[Path, int, int]] = []
    monkeypatch.setattr("utils._preserve_file_owner", lambda _path: (345, 678))
    monkeypatch.setattr(
        "utils.os.chown",
        lambda path, uid, gid: chown_calls.append((Path(path), uid, gid)),
    )

    atomic_roundtrip_yaml_update(target, "model.provider", "nvidia")

    assert chown_calls == [(target, 345, 678)]
    assert yaml.safe_load(target.read_text(encoding="utf-8"))["model"]["provider"] == "nvidia"


# ─── Broken-symlink edge case ─────────────────────────────────────────────


def test_atomic_replace_broken_symlink_creates_target(tmp_path: Path) -> None:
    """A symlink pointing at a missing file: the write should create the
    real target (resolving via realpath) rather than leaving the dangling
    link in place as a regular file.
    """
    missing = tmp_path / "does_not_exist_yet.yaml"
    link = tmp_path / "link.yaml"
    link.symlink_to(missing)
    assert link.is_symlink()
    assert not missing.exists()

    tmp = _write_tmp(tmp_path, "created-through-link\n")
    atomic_replace(tmp, link)

    assert link.is_symlink(), "symlink must be preserved"
    assert missing.exists(), "real target should now exist"
    assert missing.read_text(encoding="utf-8") == "created-through-link\n"


# ─── EXDEV / EBUSY copy fallback ───────────────────────────────────────────


@pytest.mark.parametrize("fail_errno", [errno.EXDEV, errno.EBUSY])
def test_atomic_replace_copy_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_errno: int
) -> None:
    target = tmp_path / "config.yaml"
    target.write_text("old\n", encoding="utf-8")
    tmp = _write_tmp(tmp_path, "new\n")

    def fail_replace(src: str, dst: str) -> None:
        raise OSError(fail_errno, os.strerror(fail_errno), src, None, dst)

    monkeypatch.setattr("utils.os.replace", fail_replace)

    assert Path(atomic_replace(tmp, target)) == target
    assert target.read_text(encoding="utf-8") == "new\n"
    assert not tmp.exists()


def test_atomic_replace_copy_fallback_preserves_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "real.yaml"
    link = tmp_path / "link.yaml"
    real.write_text("old\n", encoding="utf-8")
    link.symlink_to(real)
    tmp = _write_tmp(tmp_path, "new\n")

    def fail_replace(src: str, dst: str) -> None:
        raise OSError(errno.EXDEV, os.strerror(errno.EXDEV), src, None, dst)

    monkeypatch.setattr("utils.os.replace", fail_replace)

    assert Path(atomic_replace(tmp, link)) == real
    assert link.is_symlink()
    assert real.read_text(encoding="utf-8") == "new\n"
    assert not tmp.exists()


def test_atomic_replace_copy_fallback_preserves_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name != "posix":
        pytest.skip("POSIX-only")

    target = tmp_path / "config.yaml"
    target.write_text("old\n", encoding="utf-8")
    os.chmod(target, 0o600)
    tmp = _write_tmp(tmp_path, "new\n")
    os.chmod(tmp, 0o644)

    def fail_replace(src: str, dst: str) -> None:
        raise OSError(errno.EBUSY, os.strerror(errno.EBUSY), src, None, dst)

    monkeypatch.setattr("utils.os.replace", fail_replace)

    atomic_replace(tmp, target)
    assert target.read_text(encoding="utf-8") == "new\n"
    assert target.stat().st_mode & 0o777 == 0o644


def test_atomic_replace_other_oserror_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "config.yaml"
    target.write_text("old\n", encoding="utf-8")
    tmp = _write_tmp(tmp_path, "new\n")

    replace_calls = 0

    def fail_replace(src: str, dst: str) -> None:
        nonlocal replace_calls
        replace_calls += 1
        raise OSError(errno.EACCES, os.strerror(errno.EACCES), src, None, dst)

    monkeypatch.setattr("utils.os.replace", fail_replace)
    monkeypatch.setattr("utils._is_windows", lambda: False)

    with pytest.raises(OSError) as excinfo:
        atomic_replace(tmp, target)
    assert excinfo.value.errno == errno.EACCES
    assert replace_calls == 1
    assert target.read_text(encoding="utf-8") == "old\n"
    assert tmp.exists()


def test_atomic_replace_retries_two_windows_sharing_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "auth.json"
    target.write_text("old", encoding="utf-8")
    tmp = _write_tmp(tmp_path, "new")
    real_replace = os.replace
    attempts = 0
    sleeps: list[int] = []

    def sharing_then_success(src: str, dst: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            exc = OSError(errno.EACCES, "sharing violation", src, None, dst)
            exc.winerror = 32
            raise exc
        real_replace(src, dst)

    monkeypatch.setattr("utils._is_windows", lambda: True)
    monkeypatch.setattr("utils.os.replace", sharing_then_success)
    monkeypatch.setattr(
        "utils._sleep_before_windows_file_retry", sleeps.append
    )

    assert Path(atomic_replace(tmp, target)) == target
    assert attempts == 3
    assert sleeps == [1, 2]
    assert target.read_text(encoding="utf-8") == "new"
    assert not tmp.exists()


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_atomic_replace_exhausts_windows_sharing_retries_without_touching_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, winerror: int
) -> None:
    target = tmp_path / "auth.json"
    target.write_text("preserve-me", encoding="utf-8")
    tmp = _write_tmp(tmp_path, "replacement")
    attempts = 0

    def always_locked(src: str, dst: str) -> None:
        nonlocal attempts
        attempts += 1
        exc = OSError(errno.EACCES, "locked", src, None, dst)
        exc.winerror = winerror
        raise exc

    monkeypatch.setattr("utils._is_windows", lambda: True)
    monkeypatch.setattr("utils.os.replace", always_locked)
    monkeypatch.setattr("utils._sleep_before_windows_file_retry", lambda _attempt: None)

    with pytest.raises(OSError) as excinfo:
        atomic_replace(tmp, target)

    assert excinfo.value.winerror == winerror
    assert attempts == 6
    assert target.read_text(encoding="utf-8") == "preserve-me"
    assert tmp.read_text(encoding="utf-8") == "replacement"


@pytest.mark.parametrize("fail_errno", [errno.EACCES, errno.EPERM])
def test_atomic_replace_retries_windows_errno_fallbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_errno: int
) -> None:
    target = tmp_path / "state.json"
    target.write_text("old", encoding="utf-8")
    tmp = _write_tmp(tmp_path, "new")
    attempts = 0

    def always_denied(src: str, dst: str) -> None:
        nonlocal attempts
        attempts += 1
        raise OSError(fail_errno, os.strerror(fail_errno), src, None, dst)

    monkeypatch.setattr("utils._is_windows", lambda: True)
    monkeypatch.setattr("utils.os.replace", always_denied)
    monkeypatch.setattr("utils._sleep_before_windows_file_retry", lambda _attempt: None)

    with pytest.raises(OSError):
        atomic_replace(tmp, target)
    assert attempts == 6
    assert target.read_text(encoding="utf-8") == "old"


def test_atomic_replace_real_windows_handle_without_delete_share(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("Windows-only sharing semantics")

    import ctypes
    from ctypes import wintypes
    from unittest.mock import patch

    target = tmp_path / "auth.json"
    target.write_text("old", encoding="utf-8")
    tmp = _write_tmp(tmp_path, "new")

    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = ctypes.windll.kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = create_file(
        str(target),
        0x80000000,  # GENERIC_READ
        0x00000001 | 0x00000002,  # FILE_SHARE_READ | FILE_SHARE_WRITE
        None,
        3,  # OPEN_EXISTING
        0,
        None,
    )
    assert handle != wintypes.HANDLE(-1).value

    released = False

    def release_lock(_attempt: int) -> None:
        nonlocal released
        if released:
            return
        assert close_handle(handle)
        released = True

    try:
        with patch("utils._sleep_before_windows_file_retry", release_lock):
            atomic_replace(tmp, target)
    finally:
        if not released:
            close_handle(handle)

    assert released, "the first real replace must observe the exclusive handle"
    assert target.read_text(encoding="utf-8") == "new"


def test_atomic_json_write_windows_multiprocess_stress(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("Windows-only sharing stress")

    target = tmp_path / "shared.json"
    target.write_text("{}", encoding="utf-8")
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_atomic_write_worker, args=(str(target), worker_id, 25))
        for worker_id in range(4)
    ]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=20)

        assert all(not process.is_alive() for process in processes)
        assert [process.exitcode for process in processes] == [0, 0, 0, 0]
        final = json.loads(target.read_text(encoding="utf-8"))
        assert set(final) == {"worker", "sequence"}
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
        for process in processes:
            process.join(timeout=5)


def test_atomic_replace_real_cross_device(tmp_path: Path) -> None:
    shm = Path("/dev/shm")
    if os.name != "posix" or not os.access(shm, os.W_OK):
        pytest.skip("requires writable /dev/shm")

    import shutil as _shutil
    import uuid as _uuid

    other_fs_dir = shm / f"hermes-exdev-test-{_uuid.uuid4().hex[:8]}"
    other_fs_dir.mkdir()
    try:
        real = other_fs_dir / "config.yaml"
        real.write_text("old\n", encoding="utf-8")
        if os.stat(real).st_dev == os.stat(tmp_path).st_dev:
            pytest.skip("/dev/shm is not a separate filesystem here")

        link = tmp_path / "config.yaml"
        link.symlink_to(real)
        tmp = _write_tmp(tmp_path, "new\n")

        assert Path(atomic_replace(tmp, link)) == real
        assert link.is_symlink()
        assert real.read_text(encoding="utf-8") == "new\n"
        assert not tmp.exists()
    finally:
        _shutil.rmtree(other_fs_dir, ignore_errors=True)
