from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping

import pytest

from scripts.canary import production_release_rotation_stager_installer as installer


def _run(*argv: str, cwd: Path) -> str:
    completed = subprocess.run(
        argv,
        cwd=cwd,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        },
    )
    return completed.stdout.strip()


def _source_repository(tmp_path: Path) -> tuple[Path, tuple[tuple[str, str], ...]]:
    source = tmp_path / "upstream-source"
    source.mkdir()
    _run("git", "init", "-q", cwd=source)
    _run("git", "config", "user.name", "test", cwd=source)
    _run("git", "config", "user.email", "test@example.invalid", cwd=source)
    revisions: list[tuple[str, str]] = []
    for index in range(2):
        (source / "payload.txt").write_text(
            f"source snapshot {index}\n",
            encoding="utf-8",
        )
        _run("git", "add", "payload.txt", cwd=source)
        _run("git", "commit", "-qm", f"fixture {index}", cwd=source)
        revision = _run("git", "rev-parse", "HEAD", cwd=source)
        tree = _run("git", "rev-parse", "HEAD^{tree}", cwd=source)
        revisions.append((revision, tree))
    return source, tuple(revisions)


def _install(
    *,
    source: Path,
    destination: Path,
    revision: str,
    tree: str,
) -> bool:
    return installer._install_exact_source_snapshot(  # noqa: SLF001
        source_root=source,
        destination=destination,
        release_revision=revision,
        source_tree_oid=tree,
        expected_uid=os.geteuid(),
        expected_gid=os.getegid(),
    )


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _wait_for_path(path: Path, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {path}")


def _wait_for_child(pid: int, *, timeout: float = 20.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        selected, status = os.waitpid(pid, os.WNOHANG)
        if selected == pid:
            return status
        time.sleep(0.02)
    os.kill(pid, signal.SIGKILL)
    _selected, status = os.waitpid(pid, 0)
    raise AssertionError(f"child {pid} timed out with status {status}")


def _fork_install(
    *,
    source: Path,
    destination: Path,
    revision: str,
    tree: str,
    start_fd: int,
    result_path: Path,
) -> int:
    pid = os.fork()
    if pid:
        return pid
    try:
        os.read(start_fd, 1)
        try:
            created = _install(
                source=source,
                destination=destination,
                revision=revision,
                tree=tree,
            )
            result = f"ok:{int(created)}"
        except installer.RotationStagerInstallerError as exc:
            result = f"error:{exc}"
        result_path.write_text(result, encoding="ascii")
        os._exit(0)
    except BaseException:
        os._exit(91)


def _leave_complete_incomplete(
    *,
    source: Path,
    destination: Path,
    revision: str,
    tree: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    real_rename = installer._rename_directory_noreplace  # noqa: SLF001

    def stop_before_publication(source_path: Path, destination_path: Path) -> None:
        assert source_path.name.endswith(".incomplete")
        assert destination_path == destination
        raise KeyboardInterrupt

    monkeypatch.setattr(
        installer,
        "_rename_directory_noreplace",
        stop_before_publication,
    )
    with pytest.raises(KeyboardInterrupt):
        _install(
            source=source,
            destination=destination,
            revision=revision,
            tree=tree,
        )
    monkeypatch.setattr(installer, "_rename_directory_noreplace", real_rename)
    incomplete_entries = list(destination.parent.glob(".source.*.incomplete"))
    assert len(incomplete_entries) == 1
    return incomplete_entries[0]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork/flock")
def test_clone_intent_is_exact_and_snapshot_has_no_local_hardlinks(
    tmp_path: Path,
) -> None:
    source, revisions = _source_repository(tmp_path)
    revision, tree = revisions[-1]
    destination = tmp_path / "release" / "source"
    destination.parent.mkdir()

    assert (
        _install(
            source=source,
            destination=destination,
            revision=revision,
            tree=tree,
        )
        is True
    )
    assert (
        _install(
            source=source,
            destination=destination,
            revision=revision,
            tree=tree,
        )
        is False
    )

    intent_path = destination.with_name(".source.clone-intent.json")
    intent = json.loads(intent_path.read_text(encoding="ascii"))
    intent_sha256 = intent.pop("intent_sha256")
    assert hashlib.sha256(_canonical(intent)).hexdigest() == intent_sha256
    assert intent["release_revision"] == revision
    assert intent["source_tree_oid"] == tree
    assert intent["clone_transport"] == "local-path-only"
    assert intent["clone_no_hardlinks"] is True
    assert stat.S_IMODE(intent_path.stat().st_mode) == 0o444
    assert not (destination / ".git/objects/info/alternates").exists()

    object_relative = Path(".git/objects") / revision[:2] / revision[2:]
    source_object = source / object_relative
    cloned_object = destination / object_relative
    assert source_object.is_file()
    assert cloned_object.is_file()
    source_state = source_object.stat()
    cloned_state = cloned_object.stat()
    assert (source_state.st_dev, source_state.st_ino) != (
        cloned_state.st_dev,
        cloned_state.st_ino,
    )
    assert cloned_state.st_nlink == 1


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork/flock")
@pytest.mark.live_system_guard_bypass
def test_sigkill_during_clone_keeps_lock_until_git_exits_and_retry_recovers(
    tmp_path: Path,
) -> None:
    source, revisions = _source_repository(tmp_path)
    revision, tree = revisions[-1]
    destination = tmp_path / "release" / "source"
    destination.parent.mkdir()
    git_pid_path = tmp_path / "git.pid"
    retry_result = tmp_path / "retry.result"

    installer_pid = os.fork()
    if installer_pid == 0:
        original_run = installer.subprocess.run

        def paused_clone(argv: object, *args: object, **kwargs: object) -> object:
            if isinstance(argv, tuple) and "clone" in argv:
                popen_kwargs = {
                    name: kwargs[name]
                    for name in (
                        "stdin",
                        "stdout",
                        "stderr",
                        "env",
                        "cwd",
                        "pass_fds",
                    )
                    if name in kwargs
                }
                process = subprocess.Popen(argv, **popen_kwargs)
                os.kill(process.pid, signal.SIGSTOP)
                descriptor = os.open(
                    git_pid_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                try:
                    os.write(descriptor, f"{process.pid}\n".encode("ascii"))
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                stdout, stderr = process.communicate()
                return subprocess.CompletedProcess(
                    argv,
                    process.returncode,
                    stdout,
                    stderr,
                )
            return original_run(argv, *args, **kwargs)

        installer.subprocess.run = paused_clone  # type: ignore[assignment]
        try:
            _install(
                source=source,
                destination=destination,
                revision=revision,
                tree=tree,
            )
        finally:
            os._exit(92)

    _wait_for_path(git_pid_path)
    git_pid = int(git_pid_path.read_text(encoding="ascii").strip())
    os.kill(installer_pid, signal.SIGKILL)
    killed_status = _wait_for_child(installer_pid)
    assert os.WIFSIGNALED(killed_status)
    assert os.WTERMSIG(killed_status) == signal.SIGKILL

    retry_pid = os.fork()
    if retry_pid == 0:
        try:
            created = _install(
                source=source,
                destination=destination,
                revision=revision,
                tree=tree,
            )
            retry_result.write_text(f"ok:{int(created)}", encoding="ascii")
            os._exit(0)
        except BaseException as exc:
            retry_result.write_text(
                f"error:{type(exc).__name__}:{exc}", encoding="ascii"
            )
            os._exit(93)

    time.sleep(0.2)
    selected, _status = os.waitpid(retry_pid, os.WNOHANG)
    assert selected == 0
    assert not retry_result.exists()
    os.kill(git_pid, signal.SIGCONT)
    retry_status = _wait_for_child(retry_pid)
    assert os.WIFEXITED(retry_status)
    assert os.WEXITSTATUS(retry_status) == 0
    assert retry_result.read_text(encoding="ascii") == "ok:1"
    assert _run("git", "rev-parse", "HEAD", cwd=destination) == revision
    assert not list(destination.parent.glob(".source.*.incomplete"))
    assert not list(destination.parent.glob(".source.*.quarantine"))


@pytest.mark.parametrize(
    "crash_point",
    ("intent-link", "checkout", "fsync", "rename-before", "rename-after"),
)
@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork/flock")
@pytest.mark.live_system_guard_bypass
def test_sigkill_at_atomic_clone_crash_points_recovers_exactly(
    tmp_path: Path,
    crash_point: str,
) -> None:
    source, revisions = _source_repository(tmp_path)
    revision, tree = revisions[-1]
    destination = tmp_path / "release" / "source"
    destination.parent.mkdir()

    child = os.fork()
    if child == 0:
        real_link = installer.os.link
        real_git = installer._git  # noqa: SLF001
        real_fsync_tree = installer._fsync_snapshot_tree  # noqa: SLF001
        real_rename = installer._rename_directory_noreplace  # noqa: SLF001

        def crash_after_intent_link(
            source_path: object,
            destination_path: object,
            *args: object,
            **kwargs: object,
        ) -> object:
            result = real_link(source_path, destination_path, *args, **kwargs)
            if Path(os.fsdecode(destination_path)).name == ".source.clone-intent.json":
                os.kill(os.getpid(), signal.SIGKILL)
            return result

        def crash_after_checkout(
            selected: Path,
            *arguments: str,
            **kwargs: object,
        ) -> bytes:
            result = real_git(selected, *arguments, **kwargs)
            if arguments and arguments[0] == "checkout":
                os.kill(os.getpid(), signal.SIGKILL)
            return result

        def crash_after_fsync(root: Path) -> None:
            real_fsync_tree(root)
            os.kill(os.getpid(), signal.SIGKILL)

        def crash_at_rename(source_path: Path, destination_path: Path) -> None:
            if crash_point == "rename-before":
                os.kill(os.getpid(), signal.SIGKILL)
            real_rename(source_path, destination_path)
            os.kill(os.getpid(), signal.SIGKILL)

        if crash_point == "intent-link":
            installer.os.link = crash_after_intent_link  # type: ignore[assignment]
        elif crash_point == "checkout":
            installer._git = crash_after_checkout  # type: ignore[assignment]  # noqa: SLF001
        elif crash_point == "fsync":
            installer._fsync_snapshot_tree = crash_after_fsync  # type: ignore[assignment]  # noqa: SLF001
        else:
            installer._rename_directory_noreplace = crash_at_rename  # type: ignore[assignment]  # noqa: SLF001
        try:
            _install(
                source=source,
                destination=destination,
                revision=revision,
                tree=tree,
            )
        finally:
            os._exit(97)

    status = _wait_for_child(child)
    assert os.WIFSIGNALED(status)
    assert os.WTERMSIG(status) == signal.SIGKILL

    incomplete_entries = list(destination.parent.glob(".source.*.incomplete"))
    incomplete_inode: tuple[int, int] | None = None
    if crash_point in {"checkout", "fsync", "rename-before"}:
        assert len(incomplete_entries) == 1
        state = incomplete_entries[0].stat()
        incomplete_inode = (state.st_dev, state.st_ino)
    elif crash_point == "intent-link":
        final = destination.with_name(".source.clone-intent.json")
        pending = destination.with_name(".source.clone-intent.pending")
        assert final.stat().st_nlink == 2
        assert (final.stat().st_dev, final.stat().st_ino) == (
            pending.stat().st_dev,
            pending.stat().st_ino,
        )
    else:
        assert destination.is_dir()

    created = _install(
        source=source,
        destination=destination,
        revision=revision,
        tree=tree,
    )
    assert created is (crash_point != "rename-after")
    assert _run("git", "rev-parse", "HEAD", cwd=destination) == revision
    if incomplete_inode is not None:
        destination_state = destination.stat()
        assert (destination_state.st_dev, destination_state.st_ino) == incomplete_inode
    assert not list(destination.parent.glob(".source.*.incomplete"))
    assert not list(destination.parent.glob(".source.*.quarantine"))


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork/flock")
@pytest.mark.live_system_guard_bypass
def test_killed_git_leaves_actual_partial_git_tree_that_retry_quarantines(
    tmp_path: Path,
) -> None:
    source, _revisions = _source_repository(tmp_path)
    (source / "large-random.bin").write_bytes(os.urandom(32 * 1024 * 1024))
    _run("git", "add", "large-random.bin", cwd=source)
    _run("git", "commit", "-qm", "large clone fixture", cwd=source)
    revision = _run("git", "rev-parse", "HEAD", cwd=source)
    tree = _run("git", "rev-parse", "HEAD^{tree}", cwd=source)
    destination = tmp_path / "release" / "source"
    destination.parent.mkdir()

    child = os.fork()
    if child == 0:
        original_run = installer.subprocess.run

        def kill_partial_clone(argv: object, *args: object, **kwargs: object) -> object:
            if isinstance(argv, tuple) and "clone" in argv:
                popen_kwargs = {
                    name: kwargs[name]
                    for name in (
                        "stdin",
                        "stdout",
                        "stderr",
                        "env",
                        "cwd",
                        "pass_fds",
                    )
                    if name in kwargs
                }
                process = subprocess.Popen(argv, **popen_kwargs)
                incomplete = Path(str(argv[-1]))
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline:
                    if (incomplete / ".git").is_dir() and process.poll() is None:
                        os.kill(process.pid, signal.SIGKILL)
                        process.wait(timeout=10)
                        os.kill(os.getpid(), signal.SIGKILL)
                    if process.poll() is not None:
                        break
                    time.sleep(0.001)
                stdout, stderr = process.communicate()
                return subprocess.CompletedProcess(
                    argv,
                    process.returncode,
                    stdout,
                    stderr,
                )
            return original_run(argv, *args, **kwargs)

        installer.subprocess.run = kill_partial_clone  # type: ignore[assignment]
        try:
            _install(
                source=source,
                destination=destination,
                revision=revision,
                tree=tree,
            )
        finally:
            os._exit(98)

    status = _wait_for_child(child, timeout=30.0)
    assert os.WIFSIGNALED(status)
    assert os.WTERMSIG(status) == signal.SIGKILL
    incomplete_entries = list(destination.parent.glob(".source.*.incomplete"))
    assert len(incomplete_entries) == 1
    assert (incomplete_entries[0] / ".git").is_dir()
    assert not (incomplete_entries[0] / "large-random.bin").exists()

    assert (
        _install(
            source=source,
            destination=destination,
            revision=revision,
            tree=tree,
        )
        is True
    )
    assert _run("git", "rev-parse", "HEAD", cwd=destination) == revision
    assert not list(destination.parent.glob(".source.*.incomplete"))
    assert not list(destination.parent.glob(".source.*.quarantine"))


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork/flock")
@pytest.mark.live_system_guard_bypass
def test_sigkill_after_quarantine_rename_resumes_cleanup(tmp_path: Path) -> None:
    source, revisions = _source_repository(tmp_path)
    revision, tree = revisions[-1]
    destination = tmp_path / "release" / "source"
    destination.parent.mkdir()
    monkeypatch = pytest.MonkeyPatch()
    try:
        incomplete = _leave_complete_incomplete(
            source=source,
            destination=destination,
            revision=revision,
            tree=tree,
            monkeypatch=monkeypatch,
        )
    finally:
        monkeypatch.undo()
    (incomplete / "payload.txt").unlink()

    child = os.fork()
    if child == 0:

        def crash_cleanup(*_args: object, **_kwargs: object) -> None:
            os.kill(os.getpid(), signal.SIGKILL)

        installer._remove_quarantined_source_snapshot = crash_cleanup  # type: ignore[assignment]  # noqa: SLF001
        try:
            _install(
                source=source,
                destination=destination,
                revision=revision,
                tree=tree,
            )
        finally:
            os._exit(99)

    status = _wait_for_child(child)
    assert os.WIFSIGNALED(status)
    assert os.WTERMSIG(status) == signal.SIGKILL
    assert not list(destination.parent.glob(".source.*.incomplete"))
    assert len(list(destination.parent.glob(".source.*.quarantine"))) == 1

    assert (
        _install(
            source=source,
            destination=destination,
            revision=revision,
            tree=tree,
        )
        is True
    )
    assert _run("git", "rev-parse", "HEAD", cwd=destination) == revision
    assert not list(destination.parent.glob(".source.*.quarantine"))


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork/flock")
def test_same_revision_concurrency_serializes_to_one_create(tmp_path: Path) -> None:
    source, revisions = _source_repository(tmp_path)
    revision, tree = revisions[-1]
    destination = tmp_path / "release" / "source"
    destination.parent.mkdir()
    read_fd, write_fd = os.pipe()
    results = (tmp_path / "result-1", tmp_path / "result-2")
    pids = tuple(
        _fork_install(
            source=source,
            destination=destination,
            revision=revision,
            tree=tree,
            start_fd=read_fd,
            result_path=result,
        )
        for result in results
    )
    os.close(read_fd)
    os.write(write_fd, b"12")
    os.close(write_fd)

    statuses = tuple(_wait_for_child(pid) for pid in pids)
    assert all(
        os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0 for status in statuses
    )
    assert {path.read_text(encoding="ascii") for path in results} == {"ok:1", "ok:0"}
    assert _run("git", "rev-parse", "HEAD", cwd=destination) == revision


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork/flock")
def test_different_revision_concurrency_preserves_exact_winner(tmp_path: Path) -> None:
    source, revisions = _source_repository(tmp_path)
    destination = tmp_path / "release" / "source"
    destination.parent.mkdir()
    read_fd, write_fd = os.pipe()
    results = (tmp_path / "result-1", tmp_path / "result-2")
    pids = tuple(
        _fork_install(
            source=source,
            destination=destination,
            revision=revision,
            tree=tree,
            start_fd=read_fd,
            result_path=result,
        )
        for (revision, tree), result in zip(revisions, results, strict=True)
    )
    os.close(read_fd)
    os.write(write_fd, b"12")
    os.close(write_fd)

    statuses = tuple(_wait_for_child(pid) for pid in pids)
    assert all(
        os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0 for status in statuses
    )
    values = tuple(path.read_text(encoding="ascii") for path in results)
    assert sum(value == "ok:1" for value in values) == 1
    assert sum("source_snapshot_intent_conflict" in value for value in values) == 1
    installed_revision = _run("git", "rev-parse", "HEAD", cwd=destination)
    assert installed_revision in {revision for revision, _tree in revisions}
    intent = json.loads(
        destination.with_name(".source.clone-intent.json").read_text(encoding="ascii")
    )
    assert intent["release_revision"] == installed_revision


def test_foreign_incomplete_without_durable_intent_is_preserved(tmp_path: Path) -> None:
    source, revisions = _source_repository(tmp_path)
    revision, tree = revisions[-1]
    destination = tmp_path / "release" / "source"
    destination.parent.mkdir()
    intent = installer._source_snapshot_clone_intent(  # noqa: SLF001
        source_root=source,
        destination=destination,
        release_revision=revision,
        source_tree_oid=tree,
        expected_uid=os.geteuid(),
        expected_gid=os.getegid(),
    )
    foreign = destination.with_name(f".source.{intent['intent_sha256']}.incomplete")
    foreign.mkdir()
    marker = foreign / "owner-evidence"
    marker.write_text("preserve me", encoding="utf-8")

    with pytest.raises(
        installer.RotationStagerInstallerError,
        match="source_snapshot_conflict",
    ):
        _install(
            source=source,
            destination=destination,
            revision=revision,
            tree=tree,
        )

    assert marker.read_text(encoding="utf-8") == "preserve me"
    assert not destination.with_name(".source.clone-intent.json").exists()


def test_partial_pending_intent_is_rewritten_before_clone(tmp_path: Path) -> None:
    source, revisions = _source_repository(tmp_path)
    revision, tree = revisions[-1]
    destination = tmp_path / "release" / "source"
    destination.parent.mkdir()
    intent = installer._source_snapshot_clone_intent(  # noqa: SLF001
        source_root=source,
        destination=destination,
        release_revision=revision,
        source_tree_oid=tree,
        expected_uid=os.geteuid(),
        expected_gid=os.getegid(),
    )
    intent_raw = _canonical(intent) + b"\n"
    pending = destination.with_name(".source.clone-intent.pending")
    pending.write_bytes(intent_raw[:17])
    pending.chmod(0o600)

    assert (
        _install(
            source=source,
            destination=destination,
            revision=revision,
            tree=tree,
        )
        is True
    )
    assert not pending.exists()
    assert destination.with_name(".source.clone-intent.json").read_bytes() == intent_raw


def test_different_pending_intent_is_preserved_and_refused(tmp_path: Path) -> None:
    source, revisions = _source_repository(tmp_path)
    revision, tree = revisions[-1]
    destination = tmp_path / "release" / "source"
    destination.parent.mkdir()
    pending = destination.with_name(".source.clone-intent.pending")
    pending.write_bytes(b"different owner intent\n")
    pending.chmod(0o600)

    with pytest.raises(
        installer.RotationStagerInstallerError,
        match="source_snapshot_intent_conflict",
    ):
        _install(
            source=source,
            destination=destination,
            revision=revision,
            tree=tree,
        )

    assert pending.read_bytes() == b"different owner intent\n"
    assert not destination.with_name(".source.clone-intent.json").exists()


def test_distinct_exact_final_and_pending_intents_are_preserved_and_refused(
    tmp_path: Path,
) -> None:
    source, revisions = _source_repository(tmp_path)
    revision, tree = revisions[-1]
    destination = tmp_path / "release" / "source"
    destination.parent.mkdir()
    intent = installer._source_snapshot_clone_intent(  # noqa: SLF001
        source_root=source,
        destination=destination,
        release_revision=revision,
        source_tree_oid=tree,
        expected_uid=os.geteuid(),
        expected_gid=os.getegid(),
    )
    raw = _canonical(intent) + b"\n"
    final = destination.with_name(".source.clone-intent.json")
    pending = destination.with_name(".source.clone-intent.pending")
    final.write_bytes(raw)
    final.chmod(0o444)
    pending.write_bytes(raw)
    pending.chmod(0o444)
    final_state = final.stat()
    pending_state = pending.stat()
    assert (final_state.st_dev, final_state.st_ino) != (
        pending_state.st_dev,
        pending_state.st_ino,
    )

    with pytest.raises(
        installer.RotationStagerInstallerError,
        match="source_snapshot_intent_conflict",
    ):
        _install(
            source=source,
            destination=destination,
            revision=revision,
            tree=tree,
        )

    assert final.read_bytes() == raw
    assert pending.read_bytes() == raw
    assert final.stat().st_nlink == 1
    assert pending.stat().st_nlink == 1


def test_same_inode_nlink2_intent_link_crash_is_recovered(tmp_path: Path) -> None:
    source, revisions = _source_repository(tmp_path)
    revision, tree = revisions[-1]
    destination = tmp_path / "release" / "source"
    destination.parent.mkdir()
    intent = installer._source_snapshot_clone_intent(  # noqa: SLF001
        source_root=source,
        destination=destination,
        release_revision=revision,
        source_tree_oid=tree,
        expected_uid=os.geteuid(),
        expected_gid=os.getegid(),
    )
    raw = _canonical(intent) + b"\n"
    final = destination.with_name(".source.clone-intent.json")
    pending = destination.with_name(".source.clone-intent.pending")
    pending.write_bytes(raw)
    pending.chmod(0o444)
    os.link(pending, final)
    assert final.stat().st_nlink == 2

    assert (
        _install(
            source=source,
            destination=destination,
            revision=revision,
            tree=tree,
        )
        is True
    )
    assert not pending.exists()
    assert final.stat().st_nlink == 1


@pytest.mark.parametrize("unsafe_kind", ("hardlink", "fifo"))
def test_unsafe_quarantine_entry_is_preserved_and_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_kind: str,
) -> None:
    source, revisions = _source_repository(tmp_path)
    revision, tree = revisions[-1]
    destination = tmp_path / "release" / "source"
    destination.parent.mkdir()
    incomplete = _leave_complete_incomplete(
        source=source,
        destination=destination,
        revision=revision,
        tree=tree,
        monkeypatch=monkeypatch,
    )
    unsafe = (
        incomplete
        / ".git"
        / ("index.lock" if unsafe_kind == "hardlink" else "config.lock")
    )
    outside = tmp_path / "outside-owner-evidence"
    outside.write_bytes(b"must survive\n")
    if unsafe_kind == "hardlink":
        os.link(outside, unsafe)
        assert unsafe.stat().st_nlink == 2
    else:
        os.mkfifo(unsafe, 0o600)

    with pytest.raises(
        installer.RotationStagerInstallerError,
        match="source_snapshot_conflict",
    ):
        _install(
            source=source,
            destination=destination,
            revision=revision,
            tree=tree,
        )

    assert incomplete.is_dir()
    assert os.path.lexists(unsafe)
    assert outside.read_bytes() == b"must survive\n"
    assert not destination.exists()


def test_quarantine_cleanup_unlinks_symlink_without_following_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, revisions = _source_repository(tmp_path)
    destination = tmp_path / "release" / "source"
    destination.parent.mkdir()
    (source / "cleanup-link").symlink_to("../outside-owner-evidence")
    _run("git", "add", "cleanup-link", cwd=source)
    _run("git", "commit", "-qm", "tracked cleanup symlink", cwd=source)
    revision = _run("git", "rev-parse", "HEAD", cwd=source)
    tree = _run("git", "rev-parse", "HEAD^{tree}", cwd=source)
    incomplete = _leave_complete_incomplete(
        source=source,
        destination=destination,
        revision=revision,
        tree=tree,
        monkeypatch=monkeypatch,
    )
    outside = destination.parent / "outside-owner-evidence"
    outside.write_text("must survive", encoding="utf-8")
    assert (incomplete / "cleanup-link").is_symlink()
    (incomplete / "payload.txt").unlink()

    assert (
        _install(
            source=source,
            destination=destination,
            revision=revision,
            tree=tree,
        )
        is True
    )
    assert outside.read_text(encoding="utf-8") == "must survive"
    assert not list(destination.parent.glob(".source.*.quarantine"))
