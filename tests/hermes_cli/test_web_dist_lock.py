"""Dashboard artifact mutations share one no-follow OS lock."""

from __future__ import annotations

import os
import stat
import subprocess
import threading
from pathlib import Path

import pytest

from hermes_cli.web_dist_lock import WebDistLockError, web_dist_lock


def test_lock_is_private_and_reusable(tmp_path: Path):
    with web_dist_lock(tmp_path, timeout_seconds=0.0):
        lock_path = tmp_path / ".web_ui_build.lock"
        assert lock_path.is_file()
        if os.name != "nt":
            assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600

    with web_dist_lock(tmp_path, timeout_seconds=0.0):
        pass


@pytest.mark.linux_only
def test_lock_refuses_link_file_without_touching_target(tmp_path: Path):
    target = tmp_path / "target"
    target.write_bytes(b"sentinel")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".web_ui_build.lock").symlink_to(target)

    with pytest.raises(WebDistLockError, match="dashboard mutation lock"):
        with web_dist_lock(project, timeout_seconds=0.0):
            pass

    assert target.read_bytes() == b"sentinel"


@pytest.mark.linux_only
def test_lock_refuses_linked_project_parent(tmp_path: Path):
    real_project = tmp_path / "real-project"
    real_project.mkdir()
    linked_project = tmp_path / "linked-project"
    linked_project.symlink_to(real_project, target_is_directory=True)

    with pytest.raises(WebDistLockError, match="link or reparse point"):
        with web_dist_lock(linked_project, timeout_seconds=0.0):
            pass

    assert not (real_project / ".web_ui_build.lock").exists()


@pytest.mark.linux_only
def test_waiter_refuses_lock_path_replaced_during_contention(
    tmp_path: Path, monkeypatch
):
    import hermes_cli.web_dist_lock as lock_module

    opened = threading.Event()
    result: list[BaseException | str] = []
    original_open = lock_module._open_no_follow

    with web_dist_lock(tmp_path, timeout_seconds=0.0):

        def observed_open(path: Path) -> int:
            fd = original_open(path)
            opened.set()
            return fd

        monkeypatch.setattr(lock_module, "_open_no_follow", observed_open)

        def wait_for_lock() -> None:
            try:
                with web_dist_lock(tmp_path, timeout_seconds=2.0):
                    result.append("acquired")
            except BaseException as exc:
                result.append(exc)

        waiter = threading.Thread(target=wait_for_lock)
        waiter.start()
        assert opened.wait(timeout=1.0)
        lock_path = tmp_path / ".web_ui_build.lock"
        lock_path.unlink()
        lock_path.write_bytes(b"replacement")

    waiter.join(timeout=3.0)

    assert len(result) == 1
    assert isinstance(result[0], WebDistLockError)
    assert "changed while waiting" in str(result[0])


@pytest.mark.linux_only
def test_interrupted_wait_closes_handle_deterministically(tmp_path: Path, monkeypatch):
    import hermes_cli.web_dist_lock as lock_module

    class AbortWait(BaseException):
        pass

    contender = lock_module.WebDistLock(tmp_path, timeout_seconds=1.0)
    with web_dist_lock(tmp_path, timeout_seconds=0.0):
        monkeypatch.setattr(
            lock_module.time,
            "sleep",
            lambda seconds: (_ for _ in ()).throw(AbortWait()),
        )
        with pytest.raises(AbortWait):
            contender.__enter__()

    assert contender._handle is None
    with web_dist_lock(tmp_path, timeout_seconds=0.0):
        pass


@pytest.mark.linux_only
def test_interrupted_fdopen_closes_raw_descriptor(tmp_path: Path, monkeypatch):
    import hermes_cli.web_dist_lock as lock_module

    class AbortOpen(BaseException):
        pass

    descriptors: list[int] = []
    original_open = lock_module._open_no_follow

    def record_open(path: Path) -> int:
        fd = original_open(path)
        descriptors.append(fd)
        return fd

    monkeypatch.setattr(lock_module, "_open_no_follow", record_open)
    monkeypatch.setattr(
        lock_module.os,
        "fdopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AbortOpen()),
    )

    with pytest.raises(AbortOpen):
        lock_module.WebDistLock(tmp_path, timeout_seconds=0.0).__enter__()

    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


@pytest.mark.linux_only
def test_interrupted_post_open_validation_closes_handle(tmp_path: Path, monkeypatch):
    import hermes_cli.web_dist_lock as lock_module

    class AbortValidation(BaseException):
        pass

    calls = 0
    original_validate = lock_module._validate_no_reparse_topology

    def interrupt_second_validation(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise AbortValidation()
        original_validate(path)

    monkeypatch.setattr(
        lock_module,
        "_validate_no_reparse_topology",
        interrupt_second_validation,
    )
    contender = lock_module.WebDistLock(tmp_path, timeout_seconds=0.0)

    with pytest.raises(AbortValidation):
        contender.__enter__()

    assert contender._handle is None
    with web_dist_lock(tmp_path, timeout_seconds=0.0):
        pass


@pytest.mark.windows_only
def test_lock_refuses_windows_junction_parent(tmp_path: Path):
    real_project = tmp_path / "real-project"
    real_project.mkdir()
    linked_project = tmp_path / "linked-project"
    created = subprocess.run(
        [
            "cmd.exe",
            "/d",
            "/c",
            "mklink",
            "/J",
            str(linked_project),
            str(real_project),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if created.returncode != 0:
        pytest.skip(f"cannot create junction: {created.stdout} {created.stderr}")

    with pytest.raises(WebDistLockError, match="link or reparse point"):
        with web_dist_lock(linked_project, timeout_seconds=0.0):
            pass

    assert not (real_project / ".web_ui_build.lock").exists()
