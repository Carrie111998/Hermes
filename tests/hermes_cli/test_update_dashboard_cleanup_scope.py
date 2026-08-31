"""The late dashboard cleanup must not undo the update's own relaunch.

With the fleet quiesced before mutation and relaunched by its recorded
identity, the end-of-update dashboard sweep would otherwise find those
brand-new processes (it matches on cmdline, not age), kill them, and
restart them a second time. It must skip both the PIDs we just started
and the units we already restarted — matching units by exact identity,
with or without the ``.service`` suffix, since systemd accepts both
spellings and the two call sites historically disagreed.
"""

from __future__ import annotations

import pytest

from hermes_cli import dashboard_procs

# The exclusion logic itself is host-independent, but the sweep it runs
# through reaches the POSIX signal path. Pin it on the Linux lane rather
# than a bare ``skipif``, which would leave it running on no OS lane at
# all (AGENTS.md, "Use the marker, never a bare skipif").
pytestmark = pytest.mark.linux_only


@pytest.fixture
def fake_scan(monkeypatch):
    """A fleet of dashboard-ish processes with a controllable unit map."""
    seen = {"killed": []}

    def _scan(exclude_pids=None):
        exclude_pids = exclude_pids or set()
        return [
            (pid, "hermes dashboard --port 8765")
            for pid in (100, 200, 300)
            if pid not in exclude_pids
        ]

    from hermes_cli import main as hermes_main

    monkeypatch.setattr(hermes_main, "_scan_dashboard_processes", _scan)
    monkeypatch.setattr(
        hermes_main,
        "_find_stale_dashboard_pids",
        lambda *, exclude_pids=None: [p for p, _ in _scan(exclude_pids)],
    )
    monkeypatch.setattr(
        hermes_main, "_get_pid_cgroup_path", lambda pid: "/system.slice/acme.service"
    )
    monkeypatch.setattr(
        hermes_main,
        "_get_systemd_service_for_pid",
        lambda pid: "acme-dash.service" if pid == 300 else None,
    )
    monkeypatch.setattr(
        hermes_main, "_dashboard_cmdline_for_pid", lambda pid: ["hermes", "dashboard"]
    )
    monkeypatch.setattr(
        hermes_main, "_try_restart_systemd_service", lambda *a, **k: True
    )
    monkeypatch.setattr(dashboard_procs, "_lock_owned_serve_pids", lambda: set())

    def _kill(pid, sig):
        seen["killed"].append(pid)
        raise ProcessLookupError

    monkeypatch.setattr(dashboard_procs.os, "kill", _kill)
    monkeypatch.setattr(
        dashboard_procs, "_filter_dashboard_respawn_candidates", lambda *a, **k: []
    )
    return seen


def test_freshly_relaunched_pids_are_not_killed_again(fake_scan):
    dashboard_procs._kill_stale_dashboard_processes(
        restart_managed=True, exclude_pids={100, 200}
    )
    assert 100 not in fake_scan["killed"]
    assert 200 not in fake_scan["killed"]
    # The sweep still does its job on everything it was not told to skip.
    assert 300 in fake_scan["killed"]


def test_already_restarted_units_match_with_or_without_the_service_suffix(
    fake_scan,
):
    """PID 300's unit was already restarted by the update; the suffix
    spelling used by the caller must not decide whether it is skipped."""
    for spelling in ("acme-dash", "acme-dash.service"):
        fake_scan["killed"].clear()
        dashboard_procs._kill_stale_dashboard_processes(
            restart_managed=True,
            already_restarted_units={spelling},
            exclude_pids={100, 200},
        )
        assert 300 not in fake_scan["killed"], spelling
