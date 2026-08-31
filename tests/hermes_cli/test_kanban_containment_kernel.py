from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_containment as kc


def _worker(root: Path, *, populated: str = "1") -> Path:
    worker = root / "hermes-kanban-r7-0123456789abcdef01234567"
    worker.mkdir()
    (worker / "cgroup.events").write_text(
        f"populated {populated}\nfrozen 0\n", encoding="ascii"
    )
    return worker


def _authorize(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr(kc, "_current_cgroup_dir", lambda: root)


def test_cgroup_populated_reads_exact_inode_bound_directory(tmp_path, monkeypatch):
    root = tmp_path / "delegated"
    root.mkdir()
    worker = _worker(root)
    _authorize(monkeypatch, root)
    inode = worker.stat(follow_symlinks=False).st_ino

    assert kc.cgroup_populated(str(worker), inode) is True
    (worker / "cgroup.events").write_text("populated 0\n", encoding="ascii")
    assert kc.cgroup_populated(str(worker), inode) is False

    with pytest.raises(kc.ContainmentError, match="inode mismatch"):
        kc.cgroup_populated(str(worker), inode + 1)


def test_kill_cgroup_writes_kernel_kill_and_waits_for_extinction(
    tmp_path, monkeypatch
):
    root = tmp_path / "delegated"
    root.mkdir()
    worker = _worker(root)
    (worker / "cgroup.kill").write_bytes(b"")
    _authorize(monkeypatch, root)
    inode = worker.stat(follow_symlinks=False).st_ino
    observations = iter((True, True, False))
    monotonic = iter((10.0, 10.1, 10.2))
    monkeypatch.setattr(kc, "_read_populated_fd", lambda _fd: next(observations))
    monkeypatch.setattr(kc.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(kc.time, "sleep", lambda _seconds: None)

    outcome = kc.kill_cgroup(str(worker), inode, wait_seconds=1.0)

    assert outcome == {
        "backend": "cgroup_v2",
        "containment_certified": True,
        "termination_attempted": True,
        "terminated": True,
        "sigkill": True,
    }
    assert (worker / "cgroup.kill").read_bytes() == b"1\n"


def test_cleanup_cgroup_removes_only_exact_empty_directory(tmp_path, monkeypatch):
    root = tmp_path / "delegated"
    root.mkdir()
    worker = _worker(root, populated="0")
    _authorize(monkeypatch, root)
    inode = worker.stat(follow_symlinks=False).st_ino
    real_rmdir = os.rmdir

    def emulate_cgroupfs_rmdir(name, *, dir_fd=None):
        assert name == worker.name
        assert dir_fd is not None
        target_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=dir_fd)
        try:
            os.unlink("cgroup.events", dir_fd=target_fd)
        finally:
            os.close(target_fd)
        real_rmdir(name, dir_fd=dir_fd)

    monkeypatch.setattr(kc.os, "rmdir", emulate_cgroupfs_rmdir)

    assert kc.cleanup_cgroup(str(worker), inode) is True
    assert not worker.exists()


def test_cgroup_absent_requires_authorized_parent_and_exact_missing_name(
    tmp_path, monkeypatch
):
    root = tmp_path / "delegated"
    root.mkdir()
    worker = root / "hermes-kanban-r7-0123456789abcdef01234567"
    _authorize(monkeypatch, root)

    assert kc.cgroup_absent(str(worker)) is True
    worker.mkdir()
    assert kc.cgroup_absent(str(worker)) is False
    worker.rmdir()
    root.rmdir()
    assert kc.cgroup_absent(str(worker)) is False


def test_backend_rejects_non_linux_before_inspecting_cgroups(monkeypatch):
    monkeypatch.setattr(kc.sys, "platform", "darwin")

    with pytest.raises(kc.ContainmentError, match="Linux"):
        kc._current_cgroup_dir()


def test_explicit_delegated_root_is_inode_and_owner_bound(tmp_path, monkeypatch):
    root = tmp_path / "docker-scope"
    root.mkdir()
    inode = root.stat(follow_symlinks=False).st_ino
    monkeypatch.setenv("HERMES_KANBAN_CGROUP_ROOT", str(root))
    monkeypatch.setenv("HERMES_KANBAN_CGROUP_ROOT_INODE", str(inode))
    monkeypatch.setattr(
        kc.Path,
        "read_text",
        lambda *_args, **_kwargs: pytest.fail("self cgroup must not be consulted"),
    )

    assert kc._current_cgroup_dir() == root


@pytest.mark.parametrize("inode_value", [None, "not-an-inode", "999999999"])
def test_explicit_delegated_root_rejects_missing_or_wrong_inode(
    tmp_path, monkeypatch, inode_value
):
    root = tmp_path / "docker-scope"
    root.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_CGROUP_ROOT", str(root))
    if inode_value is None:
        monkeypatch.delenv("HERMES_KANBAN_CGROUP_ROOT_INODE", raising=False)
    else:
        monkeypatch.setenv("HERMES_KANBAN_CGROUP_ROOT_INODE", inode_value)

    with pytest.raises(kc.ContainmentError):
        kc._current_cgroup_dir()


def test_explicit_delegated_root_rejects_symlink(tmp_path, monkeypatch):
    real_root = tmp_path / "real-scope"
    real_root.mkdir()
    link = tmp_path / "linked-scope"
    link.symlink_to(real_root, target_is_directory=True)
    monkeypatch.setenv("HERMES_KANBAN_CGROUP_ROOT", str(link))
    monkeypatch.setenv(
        "HERMES_KANBAN_CGROUP_ROOT_INODE",
        str(real_root.stat(follow_symlinks=False).st_ino),
    )

    with pytest.raises(kc.ContainmentError):
        kc._current_cgroup_dir()


def test_paths_outside_namespace_and_symlinks_fail_closed(tmp_path, monkeypatch):
    root = tmp_path / "delegated"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "cgroup.events").write_text("populated 1\n", encoding="ascii")
    link = root / "hermes-kanban-r7-0123456789abcdef01234567"
    link.symlink_to(outside, target_is_directory=True)
    _authorize(monkeypatch, root)

    with pytest.raises(kc.ContainmentError):
        kc.cgroup_populated(str(link), outside.stat().st_ino)
    assert kc.cgroup_absent(str(link)) is False
    traversal = root / "ignored" / ".." / link.name
    with pytest.raises(kc.ContainmentError, match="outside"):
        kc.cgroup_populated(str(traversal), outside.stat().st_ino)


def test_kill_cgroup_wrong_inode_never_touches_kernel_kill(tmp_path, monkeypatch):
    root = tmp_path / "delegated"
    root.mkdir()
    worker = _worker(root)
    kill_file = worker / "cgroup.kill"
    kill_file.write_bytes(b"")
    _authorize(monkeypatch, root)

    outcome = kc.kill_cgroup(str(worker), worker.stat().st_ino + 1)

    assert outcome["containment_certified"] is False
    assert outcome["termination_attempted"] is False
    assert "inode mismatch" in outcome["uncertainty"]
    assert kill_file.read_bytes() == b""


def test_kill_cgroup_deadline_preserves_uncertainty(tmp_path, monkeypatch):
    root = tmp_path / "delegated"
    root.mkdir()
    worker = _worker(root)
    (worker / "cgroup.kill").write_bytes(b"")
    _authorize(monkeypatch, root)
    monkeypatch.setattr(kc, "_read_populated_fd", lambda _fd: True)
    moments = iter((20.0, 20.6))
    monkeypatch.setattr(kc.time, "monotonic", lambda: next(moments))

    outcome = kc.kill_cgroup(str(worker), worker.stat().st_ino, wait_seconds=0.5)

    assert outcome["containment_certified"] is False
    assert outcome["termination_attempted"] is True
    assert outcome["uncertainty"] == "cgroup_remained_populated"


def test_cleanup_preserves_populated_or_wrong_inode_cgroup(tmp_path, monkeypatch):
    root = tmp_path / "delegated"
    root.mkdir()
    worker = _worker(root, populated="1")
    _authorize(monkeypatch, root)
    inode = worker.stat().st_ino

    assert kc.cleanup_cgroup(str(worker), inode) is False
    assert worker.exists()
    (worker / "cgroup.events").write_text("populated 0\n", encoding="ascii")
    assert kc.cleanup_cgroup(str(worker), inode + 1) is False
    assert worker.exists()


def test_cleanup_revalidates_name_after_retained_descriptor_read(
    tmp_path, monkeypatch
):
    root = tmp_path / "delegated"
    root.mkdir()
    worker = _worker(root, populated="0")
    displaced = root / "displaced"
    _authorize(monkeypatch, root)
    inode = worker.stat().st_ino
    real_read = kc._read_populated_fd

    def swap_then_read(fd):
        worker.rename(displaced)
        replacement = _worker(root, populated="0")
        assert replacement.stat().st_ino != inode
        return real_read(fd)

    monkeypatch.setattr(kc, "_read_populated_fd", swap_then_read)

    assert kc.cleanup_cgroup(str(worker), inode) is False
    assert worker.exists()
    assert displaced.exists()


def test_spawn_gated_moves_blocked_helper_before_release(tmp_path, monkeypatch):
    root = tmp_path / "delegated"
    root.mkdir()
    worker = root / "hermes-kanban-r73-0123456789abcdef01234567"
    worker.mkdir()
    read_fd, write_fd = os.pipe()
    observations = []
    child_reads = []

    class FakeProc:
        pid = 818181

    def fake_popen(argv, **kwargs):
        observations.append(("spawn", tuple(argv), kwargs["pass_fds"]))
        assert kwargs["pass_fds"][0] == read_fd
        assert len(kwargs["pass_fds"]) == 2
        assert argv[1:3] == ["-I", "-S"]
        assert argv[3].startswith("/proc/self/fd/")
        child_reads.append(os.dup(read_fd))
        return FakeProc()

    monkeypatch.setattr(kc, "_create_worker_cgroup", lambda run_id: (worker, 7373))
    monkeypatch.setattr(kc.os, "pipe", lambda: (read_fd, write_fd))
    monkeypatch.setattr(kc.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        kc,
        "_move_pid_to_cgroup",
        lambda path, inode, pid: observations.append(
            ("moved", path, inode, pid)
        ),
    )

    handle = kc.spawn_gated(
        ["/opt/hermes/.venv/bin/hermes", "chat", "-q", "task"],
        task_id="t_exact",
        run_id=73,
        claim_lock="host:exact-claim",
        popen_kwargs={"env": {"SAFE": "1"}},
    )

    assert observations[0][0] == "spawn"
    assert observations[1] == ("moved", str(worker), 7373, FakeProc.pid)
    assert handle.pid == FakeProc.pid
    assert handle.task_id == "t_exact"
    assert handle.run_id == 73
    assert handle.claim_lock == "host:exact-claim"
    assert handle.cgroup_path == str(worker)
    assert handle.cgroup_inode == 7373
    assert handle.released is False
    handle.release()
    assert os.read(child_reads[0], 1) == b"1"
    assert handle.released is True
    os.close(child_reads[0])


def test_gate_helper_executes_only_after_release_byte(tmp_path):
    marker = tmp_path / "executed"
    read_fd, write_fd = os.pipe()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hermes_cli.kanban_worker_gate",
            str(read_fd),
            "--",
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).write_text('ok')",
        ],
        pass_fds=(read_fd,),
    )
    os.close(read_fd)
    try:
        assert not marker.exists()
        os.write(write_fd, b"1")
        os.close(write_fd)
        write_fd = -1
        assert process.wait(timeout=5) == 0
        assert marker.read_text(encoding="utf-8") == "ok"
    finally:
        if write_fd >= 0:
            os.close(write_fd)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_gate_helper_eof_aborts_without_exec(tmp_path):
    marker = tmp_path / "must-not-exist"
    read_fd, write_fd = os.pipe()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hermes_cli.kanban_worker_gate",
            str(read_fd),
            "--",
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).touch()",
        ],
        pass_fds=(read_fd,),
    )
    os.close(read_fd)
    os.close(write_fd)
    assert process.wait(timeout=5) == 125
    assert not marker.exists()


def test_spawn_gate_bootstrap_ignores_workspace_python_shadow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    worker = tmp_path / "delegated" / "worker"
    worker.mkdir(parents=True)
    monkeypatch.setattr(kc, "_create_worker_cgroup", lambda _run_id: (worker, 9191))
    monkeypatch.setattr(kc, "_move_pid_to_cgroup", lambda *_args: None)

    workspace = tmp_path / "workspace"
    shadow_package = workspace / "hermes_cli"
    shadow_package.mkdir(parents=True)
    startup_marker = tmp_path / "startup-ran"
    shadow_marker = tmp_path / "shadow-helper-ran"
    payload_marker = tmp_path / "payload-ran"
    (workspace / "sitecustomize.py").write_text(
        f"from pathlib import Path\nPath({str(startup_marker)!r}).write_text('bad')\n",
        encoding="utf-8",
    )
    (shadow_package / "__init__.py").write_text("", encoding="utf-8")
    (shadow_package / "kanban_worker_gate.py").write_text(
        f"from pathlib import Path\nPath({str(shadow_marker)!r}).write_text('bad')\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(workspace)

    handle = kc.spawn_gated(
        [
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(payload_marker)!r}).write_text('ok')",
        ],
        task_id="t_shadow",
        run_id=91,
        claim_lock="host:shadow",
        popen_kwargs={"cwd": str(workspace), "env": env},
    )
    try:
        time.sleep(0.2)
        assert not startup_marker.exists()
        assert not shadow_marker.exists()
        assert not payload_marker.exists()
        handle.release()
        assert handle._process.wait(timeout=5) == 0
        assert payload_marker.read_text(encoding="utf-8") == "ok"
        assert not shadow_marker.exists()
    finally:
        if handle._process.poll() is None:
            handle._process.kill()
            handle._process.wait(timeout=5)
