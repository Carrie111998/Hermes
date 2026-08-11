"""Subprocess boundary tests for dev-pipeline group kill."""

from __future__ import annotations

import os
import multiprocessing as mp
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import dev_executor as ex


def _run_exited_group_leader(pidfile: str) -> None:
    leader = (
        "import os,pathlib,subprocess,sys; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        f"pathlib.Path({pidfile!r}).write_text(str(child.pid)); "
        "os._exit(0)"
    )
    try:
        ex.run_subprocess([sys.executable, "-c", leader], timeout=0.3)
    except subprocess.TimeoutExpired:
        return
    raise AssertionError("exited group leader did not time out")


def test_run_subprocess_success_captures_output(tmp_path):
    proc = ex.run_subprocess(
        ["python3", "-c", "print('hello-subprocess')"],
        cwd=tmp_path,
        timeout=30,
    )
    assert proc.returncode == 0
    assert "hello-subprocess" in (proc.stdout or "")


def test_run_subprocess_timeout_carries_partial_output(tmp_path):
    script = (
        "import sys, time\n"
        "print('partial-line', flush=True)\n"
        "time.sleep(30)\n"
    )
    with pytest.raises(subprocess.TimeoutExpired) as excinfo:
        ex.run_subprocess(
            ["python3", "-c", script],
            cwd=tmp_path,
            timeout=1,
        )
    exc = excinfo.value
    assert exc.timeout == 1
    assert exc.cmd is not None
    combined = (exc.stdout or exc.output or "") + (exc.stderr or "")
    assert "partial-line" in combined
    assert combined.count("partial-line") == 1


@pytest.mark.skipif(
    os.name == "nt" or not hasattr(os, "killpg"),
    reason="process groups require POSIX killpg",
)
def test_run_subprocess_timeout_kills_descendants(tmp_path):
    pidfile = tmp_path / "descendant.pid"
    shell_cmd = (
        f"sleep 300 & echo $! > {pidfile}; "
        "echo started-descendant; "
        "wait"
    )
    descendant_pid: int | None = None
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            ex.run_subprocess(
                ["bash", "-c", shell_cmd],
                cwd=tmp_path,
                timeout=1,
            )
        assert pidfile.is_file(), "descendant pidfile was not written"
        descendant_pid = int(pidfile.read_text(encoding="utf-8").strip())
        with pytest.raises(ProcessLookupError):
            os.kill(descendant_pid, 0)
    finally:
        if descendant_pid is not None:
            try:
                os.kill(descendant_pid, 9)
            except (ProcessLookupError, PermissionError):
                pass


@pytest.mark.skipif(
    os.name == "nt" or not hasattr(os, "killpg"),
    reason="process groups require POSIX killpg",
)
def test_timeout_kills_group_after_leader_exits(tmp_path):
    pidfile = tmp_path / "orphan.pid"
    ctx = mp.get_context("fork")
    worker = ctx.Process(target=_run_exited_group_leader, args=(str(pidfile),))
    child_pid: int | None = None
    worker.start()
    try:
        for _ in range(40):
            if pidfile.is_file():
                child_pid = int(pidfile.read_text(encoding="utf-8").strip())
                break
            time.sleep(0.05)
        worker.join(timeout=3)
        assert not worker.is_alive(), "timeout helper hung after group leader exited"
        assert worker.exitcode == 0
        assert child_pid is not None
        for _ in range(20):
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("descendant survived after group leader exited")
    finally:
        if child_pid is not None:
            try:
                os.killpg(os.getpgid(child_pid), 9)
            except (ProcessLookupError, PermissionError):
                pass
        if worker.is_alive():
            worker.terminate()
        worker.join(timeout=2)
