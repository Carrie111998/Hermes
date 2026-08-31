"""Real processes, a real git repo: the generation-skew window must not exist.

Integration, not mocks: actual Python processes continuously read a
payload out of a real git checkout, are quiesced through the production
stop path, HEAD moves for real, and replacements are launched from the
recorded launch authority.

The invariants are exactly the ones the bug violated:

* every old PID is gone BEFORE HEAD moves;
* no runtime reads the checkout during the skew window;
* each replacement is a fresh interpreter that sees the NEW source and
  reports the new SHA;
* across a whole fleet, every old PID is replaced exactly once.
"""

from __future__ import annotations

import sys
import time

import pytest

from hermes_cli import update_cmd, update_quiesce
from tests.hermes_cli import quiesce_fleet_support as support

pytestmark = pytest.mark.linux_only


@pytest.fixture
def repo(tmp_path):
    return support.make_repo(tmp_path)


@pytest.fixture
def runtime_script(tmp_path):
    return support.write_runtime_script(tmp_path)


@pytest.fixture(autouse=True)
def _clean_state():
    update_quiesce.reset_mutation_authorization()
    update_quiesce.clear_restart_pending_state()
    yield
    update_quiesce.reset_mutation_authorization()
    update_quiesce.clear_restart_pending_state()


def _quiesce(plan, expected_sha):
    return update_quiesce.run_pre_mutation_quiesce(
        plan,
        stop_runtime=update_cmd._stop_runtime_for_quiesce,
        pid_alive=update_cmd._runtime_pid_alive,
        assess_isolation=update_quiesce.assess_updater_isolation,
        exit_timeout=15.0,
        poll_interval=0.02,
        expected_sha=expected_sha,
    )


# The ordering/replacement contract itself is shared with the macOS and
# Windows lanes so each host proves it on its own process semantics.
def test_old_pids_exit_before_head_moves_and_no_skew_read_happens(tmp_path):
    support.assert_runtimes_stop_before_head_moves(tmp_path)


def test_failed_stop_never_authorizes_mutation(tmp_path):
    support.assert_failed_stop_never_authorizes_mutation(tmp_path)


def test_replacement_runs_the_new_source_and_reports_the_new_sha(tmp_path):
    support.assert_replacement_reads_the_new_source(tmp_path)


def test_whole_fleet_is_replaced_exactly_once_on_the_same_sha(
    repo, runtime_script, tmp_path, monkeypatch
):
    fleet = [
        support.Runtime(runtime_script, repo, tmp_path / "gw-default.log", "gw-default"),
        support.Runtime(runtime_script, repo, tmp_path / "gw-zeus.log", "gw-zeus"),
        support.Runtime(runtime_script, repo, tmp_path / "dash.log", "dash"),
        support.Runtime(runtime_script, repo, tmp_path / "serve.log", "serve"),
    ]
    records = [
        support.record(fleet[0], kind="gateway", profile="default"),
        support.record(fleet[1], kind="gateway", profile="zeus"),
        support.record(fleet[2], kind="dashboard", profile="default"),
        support.record(fleet[3], kind="serve", profile="zeus"),
    ]
    spawned: list[int] = []
    try:
        for runtime in fleet:
            assert runtime.wait_for_output()
        old_pids = [r.pid for r in fleet]
        old_sha = support.git(repo, "rev-parse", "HEAD")

        report = _quiesce(support.plan(*records), old_sha)
        assert sorted(report.quiesced_pids) == sorted(old_pids)
        assert all(update_cmd._runtime_pid_alive(pid) is False for pid in old_pids)
        assert support.git(repo, "rev-parse", "HEAD") == old_sha

        new_sha = support.advance_head(repo)

        # Drive the PRODUCTION relaunch helper, so discharging the durable
        # obligation is part of what is being tested.
        from hermes_cli import main as hermes_main

        respawn_calls: list[str] = []
        real_respawn = update_cmd._respawn_recorded_runtime

        def _respawn(argv, record):
            respawn_calls.append(argv)
            pid = real_respawn(argv, record)
            if pid:
                spawned.append(pid)
            return pid

        monkeypatch.setattr(hermes_main, "_respawn_recorded_runtime", _respawn)
        monkeypatch.setattr(
            hermes_main,
            "_probe_relaunched_runtime_sha",
            lambda record, _new_pid=None: support.git(repo, "rev-parse", "HEAD"),
        )
        outcomes = update_cmd._relaunch_quiesced_runtimes(new_sha)

        assert len(respawn_calls) == len(fleet)
        assert len(set(respawn_calls)) == len(fleet), "one relaunch per runtime"
        assert len(spawned) == len(fleet)
        assert not set(spawned) & set(old_pids)
        assert update_quiesce.relaunch_is_complete(outcomes) is True
        assert {o.code_sha for o in outcomes} == {new_sha}
        # The obligation is discharged.
        assert update_quiesce.read_restart_pending_state() is None
    finally:
        for runtime in fleet:
            runtime.kill()
        support.reap(spawned)


def test_interrupted_after_quiesce_is_recoverable_from_disk(
    repo, runtime_script, tmp_path
):
    """An updater killed between quiesce and relaunch: only the on-disk
    record can restore the fleet, and it must be enough."""
    runtime = support.Runtime(runtime_script, repo, tmp_path / "old.log", "old")
    spawned: list[int] = []
    try:
        assert runtime.wait_for_output()
        old_sha = support.git(repo, "rev-parse", "HEAD")
        _quiesce(support.plan(support.record(runtime)), old_sha)
        new_sha = support.advance_head(repo)

        # The updater dies here — everything in memory is lost.
        update_quiesce.reset_mutation_authorization()

        state = update_quiesce.read_restart_pending_state()
        assert state is not None, "the retry has nothing else to work from"
        state["expected_sha"] = new_sha

        def _respawn(argv, record):
            pid = update_cmd._respawn_recorded_runtime(argv, record)
            if pid:
                spawned.append(pid)
            return pid

        outcomes = update_quiesce.relaunch_recorded_runtimes(
            state,
            restart_unit=lambda unit, scope: False,
            respawn_argv=_respawn,
            pid_alive=update_cmd._runtime_pid_alive,
            probe_sha=lambda record, _new_pid=None: support.git(repo, "rev-parse", "HEAD"),
        )
        assert update_quiesce.relaunch_is_complete(outcomes) is True
    finally:
        runtime.kill()
        support.reap(spawned)


SPAWNER_SRC = '''
import pathlib, sys, time

from hermes_cli.update_cmd import _respawn_recorded_runtime

argv, pidfile = sys.argv[1], sys.argv[2]
pid = _respawn_recorded_runtime(argv, {})
pathlib.Path(pidfile).write_text(str(pid or 0), encoding="utf-8")
while True:
    time.sleep(0.05)
'''


@pytest.mark.live_system_guard_bypass
def test_a_parent_group_kill_does_not_take_the_detached_runtime_with_it(
    repo, runtime_script, tmp_path
):
    """A tool timeout or cancelled turn kills the caller's process group.

    Anything the update spawns must outlive that: the whole point of
    detached ownership is that the updater (and the runtimes it relaunches)
    are not collateral damage when the caller goes away.
    """
    import os
    import signal
    import subprocess

    spawner_script = tmp_path / "spawner.py"
    spawner_script.write_text(SPAWNER_SRC, encoding="utf-8")
    pidfile = tmp_path / "child.pid"
    child_argv = support.join_argv(
        [
            sys.executable,
            str(runtime_script),
            str(repo),
            str(tmp_path / "detached.log"),
            "detached",
        ]
    )

    spawner = subprocess.Popen(
        [sys.executable, str(spawner_script), child_argv, str(pidfile)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    child_pid = 0
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if pidfile.is_file() and pidfile.read_text(encoding="utf-8").strip():
                child_pid = int(pidfile.read_text(encoding="utf-8").strip())
                break
            time.sleep(0.05)
        assert child_pid, "the production respawn must report a PID"
        assert update_cmd._runtime_pid_alive(child_pid) is True

        # The caller's whole process group dies — exactly what a terminal
        # tool timeout / cancellation does.
        os.killpg(os.getpgid(spawner.pid), signal.SIGKILL)
        spawner.wait(timeout=15)

        time.sleep(0.5)
        assert update_cmd._runtime_pid_alive(child_pid) is True, (
            "the detached runtime must survive its spawner's group kill"
        )
    finally:
        if spawner.poll() is None:
            spawner.kill()
        support.reap([child_pid] if child_pid else [])
