"""Launchd restart handoff regressions."""

from types import SimpleNamespace

import hermes_cli.gateway as gateway_cli


def test_launchd_restart_accepts_replacement_while_old_child_lingers(monkeypatch):
    """A fresh launchd service PID completes the graceful handoff promptly.

    launchd supervises the stderr wrapper while ``get_running_pid()`` reports
    its gateway child. The old child can remain reportable after KeepAlive has
    already replaced the wrapper, so waiting only for that child burns the full
    drain budget despite a successful restart.
    """
    now = [0.0]
    service_pids = [700, 700, 701]
    calls = []

    monkeypatch.setattr(gateway_cli.signal, "SIGUSR1", 10, raising=False)
    monkeypatch.setattr(gateway_cli, "get_launchd_label", lambda: "ai.hermes.gateway")
    monkeypatch.setattr(gateway_cli, "_launchd_domain", lambda: "gui/501")
    monkeypatch.setattr("gateway.status.get_running_pid", lambda *a, **k: 654)
    monkeypatch.setattr("gateway.status._pid_exists", lambda _pid: True)
    monkeypatch.setattr(gateway_cli, "_request_gateway_self_restart", lambda pid: False)
    monkeypatch.setattr(
        gateway_cli,
        "probe_gateway_loop_liveness",
        lambda pid, **kw: gateway_cli.GATEWAY_LOOP_ALIVE,
    )
    monkeypatch.setattr(gateway_cli, "_get_restart_exit_wait_budget", lambda: 27.0)
    monkeypatch.setattr(
        gateway_cli,
        "_launchd_print_service_pid",
        lambda domain, label: (
            True,
            service_pids.pop(0) if len(service_pids) > 1 else service_pids[0],
        ),
    )
    monkeypatch.setattr(gateway_cli.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        gateway_cli.time,
        "sleep",
        lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    monkeypatch.setattr(
        gateway_cli.os,
        "kill",
        lambda pid, sig: calls.append(("signal", pid, sig)),
    )
    monkeypatch.setattr(
        gateway_cli.subprocess,
        "run",
        lambda *a, **k: calls.append(("kickstart", a[0]))
        or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(gateway_cli, "_clear_launchd_unsupported_marker", lambda: None)

    gateway_cli.launchd_restart()

    assert ("signal", 654, 10) in calls
    assert not any(call[0] == "kickstart" for call in calls)
    assert now[0] < 27.0
