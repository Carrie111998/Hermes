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


def test_quarantine_cleanup_unlinks_symlink_without_following_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, revisions = _source_repository(tmp_path)
    revision, tree = revisions[-1]
    destination = tmp_path / "release" / "source"
    destination.parent.mkdir()
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
    outside = tmp_path / "outside-owner-evidence"
    outside.write_text("must survive", encoding="utf-8")
    (incomplete_entries[0] / ".git/cleanup-symlink").symlink_to(outside)

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
