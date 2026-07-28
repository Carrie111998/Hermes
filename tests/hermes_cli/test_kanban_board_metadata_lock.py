"""Concurrency regressions for board.json policy updates."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    for key in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_BOARD",
        "HERMES_DELEGATED_CHILD_CONTEXT",
    ):
        monkeypatch.delenv(key, raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb._BOARD_METADATA_THREAD_LOCKS.clear()
    kb.create_board("guarded")
    return home


def _wait_for(path: Path, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {path}")


def test_concurrent_metadata_edits_preserve_tightened_allowlist(
    kanban_home, monkeypatch
):
    """An unrelated stale rename must not reopen a concurrently frozen board."""
    original_read = kb.read_board_metadata
    rename_has_read = threading.Event()
    release_rename = threading.Event()
    policy_started = threading.Event()
    policy_done = threading.Event()
    errors: list[BaseException] = []

    def gated_read(board=None):
        meta = original_read(board)
        if threading.current_thread().name == "metadata-rename":
            rename_has_read.set()
            if not release_rename.wait(timeout=5):
                raise TimeoutError("test did not release rename writer")
        return meta

    monkeypatch.setattr(kb, "read_board_metadata", gated_read)

    def rename_writer():
        try:
            kb.write_board_metadata("guarded", name="Renamed")
        except BaseException as exc:  # surfaced below on the main test thread
            errors.append(exc)

    def policy_writer():
        policy_started.set()
        try:
            kb.write_board_metadata("guarded", allowed_profiles=[])
        except BaseException as exc:  # surfaced below on the main test thread
            errors.append(exc)
        finally:
            policy_done.set()

    rename = threading.Thread(target=rename_writer, name="metadata-rename")
    policy = threading.Thread(target=policy_writer, name="metadata-policy")
    rename.start()
    _wait_for_event = rename_has_read.wait(timeout=5)
    assert _wait_for_event, "rename writer never reached the controlled stale read"
    policy.start()
    assert policy_started.wait(timeout=5)
    try:
        time.sleep(0.1)
        assert not policy_done.is_set(), (
            "policy writer entered while the stale rename held the metadata lock"
        )
    finally:
        release_rename.set()
        rename.join(timeout=10)
        policy.join(timeout=10)

    assert not rename.is_alive()
    assert not policy.is_alive()
    assert errors == []
    meta = original_read("guarded")
    assert meta["name"] == "Renamed"
    assert meta["allowed_profiles"] == []


def test_metadata_lock_timeout_fails_closed(kanban_home):
    entered = threading.Event()
    release = threading.Event()

    def holder():
        with kb._board_metadata_lock("guarded"):
            entered.set()
            release.wait(timeout=5)

    thread = threading.Thread(target=holder)
    thread.start()
    assert entered.wait(timeout=5)
    try:
        with pytest.raises(TimeoutError, match="board metadata lock"):
            with kb._board_metadata_lock("guarded", timeout_seconds=0.1):
                raise AssertionError("contended lock unexpectedly acquired")
    finally:
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_forked_child_does_not_inherit_reentrant_lock_exemption(kanban_home):
    """A fork child must contend on the kernel lock, not skip it as reentrant."""
    with kb._board_metadata_lock("guarded"):
        child_pid = os.fork()
        if child_pid == 0:  # pragma: no cover - assertions run in parent
            try:
                with kb._board_metadata_lock("guarded", timeout_seconds=0.2):
                    os._exit(2)
            except TimeoutError:
                os._exit(0)
            except BaseException:
                os._exit(3)
        waited_pid, status = os.waitpid(child_pid, 0)

    assert waited_pid == child_pid
    assert os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == 0


def test_remove_blocks_stale_writer_and_prevents_resurrection(
    kanban_home, monkeypatch
):
    """A metadata edit queued behind deletion must fail, not recreate the board."""
    remove_entered = threading.Event()
    release_remove = threading.Event()
    writer_started = threading.Event()
    writer_done = threading.Event()
    errors: list[BaseException] = []
    original_rmtree = kb.shutil.rmtree

    def gated_rmtree(path):
        remove_entered.set()
        if not release_remove.wait(timeout=5):
            raise TimeoutError("test did not release board removal")
        return original_rmtree(path)

    monkeypatch.setattr(kb.shutil, "rmtree", gated_rmtree)

    def remover():
        try:
            kb.remove_board("guarded", archive=False)
        except BaseException as exc:
            errors.append(exc)

    def stale_writer():
        writer_started.set()
        try:
            kb.write_board_metadata("guarded", name="Resurrected")
        except BaseException as exc:
            errors.append(exc)
        finally:
            writer_done.set()

    remove_thread = threading.Thread(target=remover)
    writer_thread = threading.Thread(target=stale_writer)
    remove_thread.start()
    assert remove_entered.wait(timeout=5)
    writer_thread.start()
    assert writer_started.wait(timeout=5)
    try:
        time.sleep(0.1)
        assert not writer_done.is_set(), "stale writer bypassed the removal lock"
    finally:
        release_remove.set()
        remove_thread.join(timeout=10)
        writer_thread.join(timeout=10)

    assert not remove_thread.is_alive()
    assert not writer_thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], ValueError)
    assert "does not exist" in str(errors[0])
    assert not kb.board_dir("guarded").exists()
    assert not kb.board_exists("guarded")


def test_write_metadata_preserves_implicit_named_board_creation(kanban_home):
    meta = kb.write_board_metadata("implicit-alias", name="Implicit Alias")

    assert meta["name"] == "Implicit Alias"
    assert kb.board_exists("implicit-alias")
    assert kb.read_board_metadata("implicit-alias")["name"] == "Implicit Alias"


@pytest.mark.skipif(
    os.name not in {"posix", "nt"},
    reason="board metadata lock supports POSIX flock and Windows msvcrt",
)
def test_metadata_lock_excludes_another_process(kanban_home, tmp_path):
    ready = tmp_path / "holder-ready"
    release = tmp_path / "holder-release"
    writer_started = tmp_path / "writer-started"
    writer_done = tmp_path / "writer-done"
    holder_script = tmp_path / "holder.py"
    writer_script = tmp_path / "writer.py"

    holder_script.write_text(
        textwrap.dedent(
            f"""
            import time
            from pathlib import Path
            from hermes_cli import kanban_db as kb

            ready = Path({str(ready)!r})
            release = Path({str(release)!r})
            with kb._board_metadata_lock("guarded"):
                ready.write_text("1", encoding="utf-8")
                deadline = time.monotonic() + 15
                while not release.exists():
                    if time.monotonic() >= deadline:
                        raise TimeoutError("parent did not release metadata lock")
                    time.sleep(0.01)
            """
        ),
        encoding="utf-8",
    )
    writer_script.write_text(
        textwrap.dedent(
            f"""
            from pathlib import Path
            from hermes_cli import kanban_db as kb

            Path({str(writer_started)!r}).write_text("1", encoding="utf-8")
            kb.write_board_metadata("guarded", allowed_profiles=[])
            Path({str(writer_done)!r}).write_text("1", encoding="utf-8")
            """
        ),
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["HERMES_HOME"] = str(kanban_home)
    env["HERMES_KANBAN_HOME"] = str(kanban_home)
    env["PYTHONPATH"] = str(_REPO_ROOT)
    env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)

    holder = subprocess.Popen(
        [sys.executable, str(holder_script)],
        cwd=_REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    writer = None
    try:
        _wait_for(ready)
        writer = subprocess.Popen(
            [sys.executable, str(writer_script)],
            cwd=_REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        _wait_for(writer_started)
        time.sleep(0.1)
        assert not writer_done.exists(), (
            "separate process entered the board metadata critical section"
        )
    finally:
        release.write_text("1", encoding="utf-8")

    holder_out, holder_err = holder.communicate(timeout=15)
    assert holder.returncode == 0, holder_out + holder_err
    assert writer is not None
    writer_out, writer_err = writer.communicate(timeout=15)
    assert writer.returncode == 0, writer_out + writer_err
    assert writer_done.exists()
    assert kb.read_board_metadata("guarded")["allowed_profiles"] == []
