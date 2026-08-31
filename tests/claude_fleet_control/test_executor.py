"""Executor boundary: final revalidation, failure recording, survivor proof.

All fakes, all negative pids. The terminate function is injected; nothing in
this file can reach a real process.
"""

from claude_fleet_control.executor import WindowsTreeExecutor
from claude_fleet_control.models import TargetSummary
from tests.claude_fleet_control.conftest import NOW, cli_rec, rec


def _target(root, members):
    return TargetSummary(
        root_identity=root.identity, root_pid=root.pid,
        root_create_time=root.create_time,
        member_identities=tuple(sorted(m.identity for m in members)),
        member_count=len(members), total_rss=sum(m.rss for m in members),
        transcript_path="t.jsonl", transcript_mtime=NOW - 3600.0,
        idle_minutes=60.0, strike_key="k", strikes=2,
    )


def _executor(live_ref, kills, fail=None):
    def terminate(pid, *, force, reason):
        assert force is True and reason.startswith("claude_fleet:")
        if fail is not None:
            raise fail
        kills.append(pid)
        live_ref["records"] = [r for r in live_ref["records"] if r.pid > 0]  # tree gone

    return WindowsTreeExecutor(
        terminate_fn=terminate,
        snapshot_fn=lambda: list(live_ref["records"]),
        sleep_fn=lambda _s: None,
    )


def test_root_identity_mismatch_cancels_without_killing():
    root = cli_rec(-200)
    child = rec(-201, ppid=-200)
    recycled_root = cli_rec(-200, create_time=NOW - 5.0)  # same pid, new life
    kills = []
    executor = _executor({"records": [recycled_root, child]}, kills)
    report = executor.hard_terminate_tree(_target(root, (root, child)), plan_id="p")
    assert report.cancelled and not report.ok and kills == []


def test_recycled_member_pid_cancels_without_killing():
    root = cli_rec(-210)
    child = rec(-211, ppid=-210)
    recycled_child = rec(-211, ppid=-210, create_time=NOW - 3.0)
    kills = []
    executor = _executor({"records": [root, recycled_child]}, kills)
    report = executor.hard_terminate_tree(_target(root, (root, child)), plan_id="p")
    assert report.cancelled and kills == []


def test_terminate_failure_is_recorded_with_survivors():
    root = cli_rec(-220)
    child = rec(-221, ppid=-220)
    executor = _executor(
        {"records": [root, child]}, [], fail=OSError("taskkill said no")
    )
    report = executor.hard_terminate_tree(_target(root, (root, child)), plan_id="p")
    assert not report.ok and not report.cancelled
    assert "terminate failed" in report.detail
    assert set(report.surviving_identities) == {root.identity, child.identity}


def test_successful_kill_proves_exit_of_every_member():
    root = cli_rec(-230)
    child = rec(-231, ppid=-230)
    kills = []
    executor = _executor({"records": [root, child]}, kills)
    report = executor.hard_terminate_tree(_target(root, (root, child)), plan_id="p")
    assert report.ok and kills == [-230]  # one taskkill /T call, root only
    assert set(report.exited_identities) == {root.identity, child.identity}
    assert report.surviving_identities == ()


def test_survivor_is_reported_not_retried():
    root = cli_rec(-240)
    stubborn = rec(-241, ppid=-240)
    live = {"records": [root, stubborn]}

    def terminate(pid, *, force, reason):
        live["records"] = [stubborn]  # root died, child survived

    executor = WindowsTreeExecutor(
        terminate_fn=terminate,
        snapshot_fn=lambda: list(live["records"]),
        sleep_fn=lambda _s: None,
    )
    report = executor.hard_terminate_tree(_target(root, (root, stubborn)), plan_id="p")
    assert not report.ok and not report.cancelled
    assert report.surviving_identities == (stubborn.identity,)
    assert report.exited_identities == (root.identity,)
