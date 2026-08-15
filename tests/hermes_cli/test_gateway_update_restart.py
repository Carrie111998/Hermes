"""Regression coverage for gateway restart handoff during Hermes update."""

import os
import sys
from unittest.mock import MagicMock

import pytest

import hermes_cli.gateway as gateway_cli


def test_update_restart_defers_when_gateway_is_process_ancestor(monkeypatch):
    """#82161: a gateway-owned updater must not wait for its parent to exit."""
    graceful = MagicMock()
    monkeypatch.setattr(gateway_cli, "_request_gateway_self_restart", lambda pid: True)
    monkeypatch.setattr(gateway_cli, "_graceful_restart_via_sigusr1", graceful)

    outcome = gateway_cli._restart_gateway_for_update(654, 27.0)

    assert outcome == "deferred"
    graceful.assert_not_called()


def test_update_restart_detects_real_process_ancestor(monkeypatch):
    """Exercise real parent traversal; mock only signal delivery."""
    parent_pid = os.getppid()
    sent = []
    graceful = MagicMock()
    monkeypatch.setattr(gateway_cli.signal, "SIGUSR1", 10, raising=False)
    monkeypatch.setattr(
        gateway_cli.os,
        "kill",
        lambda pid, sig: sent.append((pid, sig)),
    )
    monkeypatch.setattr(gateway_cli, "_graceful_restart_via_sigusr1", graceful)

    outcome = gateway_cli._restart_gateway_for_update(parent_pid, 27.0)

    assert outcome == "deferred"
    assert sent == [(parent_pid, 10)]
    graceful.assert_not_called()


def test_update_restart_waits_for_unrelated_gateway(monkeypatch):
    monkeypatch.setattr(gateway_cli, "_request_gateway_self_restart", lambda pid: False)
    monkeypatch.setattr(
        gateway_cli,
        "_graceful_restart_via_sigusr1",
        lambda pid, timeout: (pid, timeout) == (654, 27.0),
    )

    assert gateway_cli._restart_gateway_for_update(654, 27.0) == "exited"


def test_update_restart_reports_failed_graceful_handoff(monkeypatch):
    monkeypatch.setattr(gateway_cli, "_request_gateway_self_restart", lambda pid: False)
    monkeypatch.setattr(
        gateway_cli, "_graceful_restart_via_sigusr1", lambda pid, timeout: False
    )

    assert gateway_cli._restart_gateway_for_update(654, 27.0) == "failed"


def test_detached_watcher_never_respawns_while_old_gateway_is_alive(monkeypatch):
    """A long after-turn drain must not create a second gateway after 120s."""
    outer_calls = []
    monkeypatch.setattr(
        gateway_cli.subprocess,
        "Popen",
        lambda argv, **kwargs: outer_calls.append((argv, kwargs)) or MagicMock(),
    )

    assert gateway_cli._spawn_gateway_restart_watcher(
        654,
        [sys.executable, "-m", "hermes_cli.main", "gateway", "run"],
        wait_timeout=0.0,
    )

    watcher_argv = outer_calls[0][0]
    watcher_source = watcher_argv[2]
    respawn = MagicMock()
    monkeypatch.setattr(gateway_cli.subprocess, "Popen", respawn)
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: pid == 654)
    monkeypatch.setattr(sys, "argv", ["-c", *watcher_argv[3:]])

    with pytest.raises(SystemExit) as exc:
        exec(compile(watcher_source, "<gateway-restart-watcher>", "exec"), {})

    assert exc.value.code == 1
    respawn.assert_not_called()


def test_profile_restart_watcher_uses_after_turn_wait_budget(monkeypatch):
    monkeypatch.setattr(gateway_cli, "_capture_gateway_argv", lambda pid: [])
    launch = MagicMock(return_value=True)
    monkeypatch.setattr(gateway_cli, "launch_detached_profile_gateway_restart", launch)

    assert (
        gateway_cli._prepare_profile_gateway_update_restart("work", 654, 27.0)
        == "detached"
    )
    launch.assert_called_once_with("work", 654, wait_timeout=27.0)


def test_unmapped_gateway_restart_replays_captured_command(monkeypatch):
    argv = [sys.executable, "-m", "hermes_cli.main", "gateway", "run"]
    monkeypatch.setattr(gateway_cli, "_capture_gateway_argv", lambda pid: argv)
    launch = MagicMock(return_value=True)
    monkeypatch.setattr(gateway_cli, "launch_detached_gateway_restart_by_cmdline", launch)

    assert (
        gateway_cli._prepare_unmapped_gateway_update_restart(654, 27.0)
        == "detached"
    )
    launch.assert_called_once_with(654, argv, wait_timeout=27.0)


def test_unmapped_external_supervisor_does_not_spawn_hermes_watcher(monkeypatch):
    argv = [
        sys.executable,
        "-m",
        "hermes_cli.main",
        "gateway",
        "run",
        "--external-supervisor",
    ]
    monkeypatch.setattr(gateway_cli, "_capture_gateway_argv", lambda pid: argv)
    launch = MagicMock()
    monkeypatch.setattr(
        gateway_cli,
        "launch_detached_gateway_restart_by_cmdline",
        launch,
    )

    assert (
        gateway_cli._prepare_unmapped_gateway_update_restart(654, 27.0)
        == "external-supervisor"
    )
    launch.assert_not_called()


def test_systemd_deferred_restart_schedules_post_exit_recovery(monkeypatch):
    launch = MagicMock(return_value=True)
    monkeypatch.setattr(gateway_cli, "_spawn_gateway_restart_watcher", launch)

    assert gateway_cli.launch_detached_systemd_restart_after_exit(
        654,
        ["systemctl", "--user"],
        "hermes-gateway.service",
        wait_timeout=27.0,
    )

    old_pid, command = launch.call_args.args
    assert old_pid == 654
    assert command[:2] == [sys.executable, "-c"]
    assert command[-2:] == [
        '["systemctl", "--user"]',
        "hermes-gateway.service",
    ]
    assert launch.call_args.kwargs == {"wait_timeout": 27.0}


def test_watcher_timeout_is_unbounded_for_deferred_ancestor(monkeypatch):
    """#82195 review: a guessed short deadline abandons the respawn once a
    deferred restart's real exit (gateway after-turn wait + drain, which this
    CLI cannot reliably bound) outlives it."""
    monkeypatch.setattr(gateway_cli, "_is_pid_ancestor_of_current_process", lambda pid: True)

    assert gateway_cli._restart_watcher_wait_timeout(654, 45.0) == float("inf")


def test_watcher_timeout_stays_bounded_for_unrelated_gateway(monkeypatch):
    """A non-ancestor gateway already went through a bounded synchronous
    SIGUSR1 wait before the watcher is spawned, so the drain-budget deadline
    remains a safe, tight bound."""
    monkeypatch.setattr(gateway_cli, "_is_pid_ancestor_of_current_process", lambda pid: False)

    assert gateway_cli._restart_watcher_wait_timeout(654, 45.0) == 75.0
