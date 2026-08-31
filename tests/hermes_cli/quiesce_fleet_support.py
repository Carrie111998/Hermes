"""Shared real-process fleet harness for the pre-mutation quiesce tests.

Not a test module: it holds the fixtures the per-OS quiesce integration
files (linux/macos/windows) all drive, so the same invariants are pinned
on every host without three copies of the plumbing.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from hermes_cli.update_inventory import RuntimeRecord, UpdatePlan

# A stand-in runtime: re-reads the checkout's payload every tick, exactly
# like a long-lived interpreter lazily importing from the shared tree.
RUNTIME_SRC = '''
import pathlib, sys, time

repo, log, tag = sys.argv[1], sys.argv[2], sys.argv[3]
while True:
    namespace = {}
    exec(pathlib.Path(repo, "payload.py").read_text(encoding="utf-8"), namespace)
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(f"{tag} {namespace['VERSION']}\\n")
        handle.flush()
    time.sleep(0.02)
'''


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def make_repo(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    (root / "payload.py").write_text('VERSION = "v1"\n', encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "v1")
    return root


def write_runtime_script(tmp_path: Path) -> Path:
    path = tmp_path / "runtime.py"
    path.write_text(RUNTIME_SRC, encoding="utf-8")
    return path


def advance_head(repo: Path) -> str:
    (repo / "payload.py").write_text('VERSION = "v2"\n', encoding="utf-8")
    git(repo, "commit", "-qam", "v2")
    return git(repo, "rev-parse", "HEAD")


class Runtime:
    """A live child process reading the checkout, with a recorded argv."""

    def __init__(self, script: Path, repo: Path, log: Path, tag: str):
        self.log = Path(log)
        self.argv = [sys.executable, str(script), str(repo), str(log), tag]
        kwargs: dict = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        else:
            kwargs["start_new_session"] = True
        self.proc = subprocess.Popen(self.argv, **kwargs)

    @property
    def pid(self) -> int:
        return self.proc.pid

    def wait_for_output(self, timeout: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.log.is_file() and self.log.read_text(encoding="utf-8").strip():
                return True
            time.sleep(0.02)
        return False

    def lines(self) -> list:
        if not self.log.is_file():
            return []
        return [
            line
            for line in self.log.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()


def join_argv(argv) -> str:
    """Render argv the way the platform's respawn path will re-parse it."""
    if os.name == "nt":
        return subprocess.list2cmdline(list(argv))
    import shlex

    return shlex.join(str(part) for part in argv)


def record(runtime: Runtime, *, kind: str = "gateway", profile: str = "default"):
    """A plan row whose ONLY relaunch authority is the recorded argv."""
    return RuntimeRecord(
        kind=kind,
        profile=profile,
        pid=runtime.pid,
        supervisor="manual",
        restart_via="manual",
        detail={"argv": join_argv(runtime.argv)},
    )


def plan(*records) -> UpdatePlan:
    result = UpdatePlan()
    result.runtimes = list(records)
    return result


def reap(pids) -> None:
    for pid in pids or ():
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F", "/T"],
                    capture_output=True,
                    timeout=15,
                )
            else:
                os.kill(int(pid), 9)
        except (OSError, subprocess.SubprocessError):
            pass


# ---------------------------------------------------------------------------
# Shared per-OS scenarios
# ---------------------------------------------------------------------------
#
# The ordering contract is identical on every platform, but it has to be
# proven on the host's REAL process/signal semantics rather than by
# monkeypatching ``sys.platform``. So the scenario bodies live here once
# and each OS file drives them behind its own ``*_only`` marker.


def quiesce(plan_obj, expected_sha, *, exit_timeout=20.0, poll_interval=0.05):
    """Run the production pre-mutation quiesce against a real fleet."""
    from hermes_cli import update_cmd, update_quiesce

    return update_quiesce.run_pre_mutation_quiesce(
        plan_obj,
        stop_runtime=update_cmd._stop_runtime_for_quiesce,
        pid_alive=update_cmd._runtime_pid_alive,
        assess_isolation=update_quiesce.assess_updater_isolation,
        exit_timeout=exit_timeout,
        poll_interval=poll_interval,
        expected_sha=expected_sha,
    )


def assert_runtimes_stop_before_head_moves(tmp_path: Path) -> None:
    """Every old PID is gone BEFORE HEAD moves, and none read the skew."""
    from hermes_cli import update_cmd

    repo = make_repo(tmp_path)
    script = write_runtime_script(tmp_path)
    fleet = [
        Runtime(script, repo, tmp_path / "a.log", "a"),
        Runtime(script, repo, tmp_path / "b.log", "b"),
    ]
    try:
        for runtime in fleet:
            assert runtime.wait_for_output()
        old_sha = git(repo, "rev-parse", "HEAD")

        report = quiesce(plan(*[record(r) for r in fleet]), old_sha)

        assert sorted(report.quiesced_pids) == sorted(r.pid for r in fleet)
        assert all(update_cmd._runtime_pid_alive(r.pid) is False for r in fleet)
        assert git(repo, "rev-parse", "HEAD") == old_sha

        advance_head(repo)
        time.sleep(0.5)  # a generous skew window
        for runtime in fleet:
            assert all(
                line.endswith("v1") for line in runtime.lines()
            ), runtime.lines()
    finally:
        for runtime in fleet:
            runtime.kill()


def assert_failed_stop_never_authorizes_mutation(tmp_path: Path) -> None:
    """A stop that does not take leaves mutation unauthorized."""
    import pytest

    from hermes_cli import update_cmd, update_quiesce

    repo = make_repo(tmp_path)
    script = write_runtime_script(tmp_path)
    runtime = Runtime(script, repo, tmp_path / "a.log", "a")
    try:
        assert runtime.wait_for_output()
        with pytest.raises(update_quiesce.QuiesceAbort):
            update_quiesce.run_pre_mutation_quiesce(
                plan(record(runtime)),
                stop_runtime=lambda _record: False,
                pid_alive=update_cmd._runtime_pid_alive,
                assess_isolation=update_quiesce.assess_updater_isolation,
                exit_timeout=1.0,
                poll_interval=0.05,
            )
        with pytest.raises(update_quiesce.QuiesceAbort):
            update_quiesce.assert_mutation_authorized("git")
    finally:
        runtime.kill()


def assert_replacement_reads_the_new_source(tmp_path: Path) -> None:
    """The replacement is a fresh interpreter that sees the NEW source."""
    from hermes_cli import update_cmd, update_quiesce

    repo = make_repo(tmp_path)
    script = write_runtime_script(tmp_path)
    runtime = Runtime(script, repo, tmp_path / "a.log", "a")
    spawned: list = []
    try:
        assert runtime.wait_for_output()
        old_sha = git(repo, "rev-parse", "HEAD")
        quiesce(plan(record(runtime)), old_sha)
        new_sha = advance_head(repo)

        state = update_quiesce.read_restart_pending_state()
        assert state is not None
        state["expected_sha"] = new_sha

        def _respawn(argv, rec):
            pid = update_cmd._respawn_recorded_runtime(argv, rec)
            if pid:
                spawned.append(pid)
            return pid

        outcomes = update_quiesce.relaunch_recorded_runtimes(
            state,
            restart_unit=lambda unit, scope: False,
            respawn_argv=_respawn,
            pid_alive=update_cmd._runtime_pid_alive,
            probe_sha=lambda rec: git(repo, "rev-parse", "HEAD"),
        )
        assert update_quiesce.relaunch_is_complete(outcomes) is True
        assert outcomes[0].new_pid not in (None, runtime.pid)
        assert outcomes[0].code_sha == new_sha

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if any(line.endswith("v2") for line in runtime.lines()):
                break
            time.sleep(0.05)
        assert any(line.endswith("v2") for line in runtime.lines()), runtime.lines()
    finally:
        runtime.kill()
        reap(spawned)
