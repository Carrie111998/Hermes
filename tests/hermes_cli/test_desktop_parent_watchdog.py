from __future__ import annotations

from hermes_cli.desktop_parent_watchdog import (
    _parse_parent_pid,
    _should_exit_for_parent,
    start_desktop_parent_watchdog,
)


def test_parse_parent_pid_accepts_positive_ints_only():
    assert _parse_parent_pid("123") == 123
    assert _parse_parent_pid(456) == 456
    assert _parse_parent_pid("0") is None
    assert _parse_parent_pid("-1") is None
    assert _parse_parent_pid("not-a-pid") is None
    assert _parse_parent_pid(None) is None


def test_should_exit_only_after_desktop_parent_is_gone():
    assert _should_exit_for_parent(4242, getppid=lambda: 4242, pid_exists=lambda _pid: True) is False
    assert _should_exit_for_parent(4242, getppid=lambda: 1, pid_exists=lambda _pid: True) is True
    assert _should_exit_for_parent(4242, getppid=lambda: 99, pid_exists=lambda _pid: False) is True
    # Wrapper/shell launch case: immediate parent differs, but the desktop PID is
    # still alive, so do not self-kill immediately.
    assert _should_exit_for_parent(4242, getppid=lambda: 99, pid_exists=lambda _pid: True) is False


def test_watchdog_disabled_without_desktop_parent_env():
    assert start_desktop_parent_watchdog({"HERMES_DESKTOP": "1"}) is None
    assert start_desktop_parent_watchdog({"HERMES_DESKTOP_PARENT_PID": "123"}) is None
    assert start_desktop_parent_watchdog({"HERMES_DESKTOP": "1", "HERMES_DESKTOP_PARENT_PID": "bad"}) is None
