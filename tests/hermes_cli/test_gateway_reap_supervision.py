"""Regression tests for the gateway orphan-reap supervision guard (issue #83683).

``_reap_unsupervised_gateway_orphans`` must never kill a *supervised* gateway
(one that owns a live ``gateway.pid`` record, or one managed by an external
supervisor: systemd / launchd / the Windows ``Hermes_Gateway`` scheduled task).
Killing a supervised gateway on a desktop (re)start is exactly the regression
that silently took WeChat/QQ/Telegram offline.
"""

import os
import time
from unittest.mock import MagicMock

import pytest

from gateway import status as gateway_status
from hermes_cli import gateway


class _FakePath:
    def __init__(self, exists: bool):
        self._exists = exists

    def exists(self) -> bool:
        return self._exists


@pytest.fixture
def reap_env(monkeypatch):
    """Common mocks for ``_reap_unsupervised_gateway_orphans`` tests."""
    monkeypatch.setattr(gateway, "supports_systemd_services", lambda: False)
    monkeypatch.setattr(gateway, "_gateway_has_active_supervisor", lambda: False)
    monkeypatch.setattr(gateway_status, "write_planned_stop_marker", lambda *a, **k: True)

    killed = []

    def fake_kill(pid, sig):
        killed.append((pid, sig))

    monkeypatch.setattr("os.kill", fake_kill)
    # Make the survivor-wait loop exit immediately.
    monkeypatch.setattr(gateway_status, "_pid_exists", lambda pid: False)
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)

    clock = [0.0]

    def fake_monotonic():
        clock[0] += 100.0
        return clock[0]

    monkeypatch.setattr("time.monotonic", fake_monotonic)
    return killed


class TestReapSupervisedGateway:
    def test_supervisor_active_skips_reap(self, monkeypatch, reap_env):
        monkeypatch.setattr(gateway, "_gateway_has_active_supervisor", lambda: True)
        assert gateway._reap_unsupervised_gateway_orphans() is False
        assert reap_env == []  # nothing killed

    def test_systemd_host_skips_reap(self, monkeypatch, reap_env):
        monkeypatch.setattr(gateway, "supports_systemd_services", lambda: True)
        assert gateway._reap_unsupervised_gateway_orphans() is False
        assert reap_env == []

    def test_supervised_pid_excluded_orphan_still_reaped(self, monkeypatch, reap_env):
        # supervised pid 100 (from gateway.pid) must survive; orphan 200 killed.
        monkeypatch.setattr(gateway_status, "get_running_pid", lambda: 100)
        monkeypatch.setattr(gateway, "find_gateway_pids", lambda **k: [100, 200])
        assert gateway._reap_unsupervised_gateway_orphans() is True
        killed_pids = [pid for pid, _sig in reap_env]
        assert 100 not in killed_pids
        assert 200 in killed_pids

    def test_only_orphan_no_supervisor_killed(self, monkeypatch, reap_env):
        # No pidfile, but a manually-started gateway (orphan) is running.
        monkeypatch.setattr(gateway_status, "get_running_pid", lambda: None)
        monkeypatch.setattr(gateway, "find_gateway_pids", lambda **k: [555])
        assert gateway._reap_unsupervised_gateway_orphans() is True
        killed_pids = [pid for pid, _sig in reap_env]
        assert 555 in killed_pids


class TestSupervisorSurvivesPoolChurn:
    """Regression for the multi-profile Desktop pool-churn trigger (issue #83683).

    Reported by TheVisher: the Desktop app can stay open while normal per-profile
    backend-pool rotation repeatedly starts fresh ``HERMES_DESKTOP=1`` serve
    backends. Each start calls ``_reap_unsupervised_gateway_orphans()``, so a
    launchd-supervised gateway gets reaped on every churn unless the supervisor
    guard holds. Assert that repeated reap calls against a live launchd-managed
    gateway never signal it and never write a planned-stop marker, and that the
    gateway PID survives unchanged.
    """

    def test_launchd_gateway_survives_repeated_churn(self, monkeypatch):
        # Force the real launchd detection path (do NOT stub
        # _gateway_has_active_supervisor): macOS + a loaded+running plist.
        monkeypatch.setattr(gateway, "supports_systemd_services", lambda: False)
        monkeypatch.setattr(gateway, "is_macos", lambda: True)
        monkeypatch.setattr(gateway, "is_windows", lambda: False)
        monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: _FakePath(True))
        monkeypatch.setattr(gateway, "_probe_launchd_service_running", lambda: True)

        live_gw_pid = 4242
        monkeypatch.setattr(gateway, "find_gateway_pids", lambda **k: [live_gw_pid])
        monkeypatch.setattr(gateway_status, "get_running_pid", lambda: None)

        killed = []
        monkeypatch.setattr("os.kill", lambda pid, sig: killed.append((pid, sig)))
        markers = []
        monkeypatch.setattr(
            gateway_status, "write_planned_stop_marker", lambda *a, **k: markers.append(a) or True
        )
        monkeypatch.setattr(gateway_status, "_pid_exists", lambda pid: False)
        monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)

        clock = [0.0]
        def fake_monotonic():
            clock[0] += 100.0
            return clock[0]
        monkeypatch.setattr("time.monotonic", fake_monotonic)

        # Simulate backend-pool churn: the Desktop recreates the serve backend
        # for this profile several times (LRU eviction + reopen).
        for _ in range(5):
            assert gateway._reap_unsupervised_gateway_orphans() is False

        # The launchd-managed gateway must survive every churn untouched.
        assert killed == []
        assert markers == []
        # And the supervised PID is still reported live (unchanged).
        assert gateway.find_gateway_pids() == [live_gw_pid]

    def test_profile_selection_keeps_launchd_gateway_pids_unchanged(self, monkeypatch):
        # Regression for @luiborge23's reproduction (issue #83683): with multiple
        # named Desktop profiles, each owning a launchd-supervised gateway, merely
        # *selecting* a profile starts that profile's `hermes serve` backend, which
        # calls `_reap_unsupervised_gateway_orphans()`. Every launchd-managed
        # gateway (selected profile and others) must survive the reap untouched —
        # the reap must not SIGTERM a supervised gateway nor write a planned-stop
        # marker, and the gateway PID reported by `find_gateway_pids` must be
        # unchanged. A read-only `gateway status` does not trigger this; the
        # Desktop profile *selection* does.
        monkeypatch.setattr(gateway, "supports_systemd_services", lambda: False)
        monkeypatch.setattr(gateway, "is_macos", lambda: True)
        monkeypatch.setattr(gateway, "is_windows", lambda: False)
        monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: _FakePath(True))
        monkeypatch.setattr(gateway, "_probe_launchd_service_running", lambda: True)

        # Two profiles, each with its own supervised launchd gateway (mirrors
        # coder_1=95356, coder_3=95442 from the report).
        coder_1_pid, coder_3_pid = 95356, 95442
        monkeypatch.setattr(
            gateway, "find_gateway_pids", lambda **k: [coder_1_pid, coder_3_pid]
        )
        monkeypatch.setattr(gateway_status, "get_running_pid", lambda: None)

        killed = []
        monkeypatch.setattr("os.kill", lambda pid, sig: killed.append((pid, sig)))
        markers = []
        monkeypatch.setattr(
            gateway_status, "write_planned_stop_marker", lambda *a, **k: markers.append(a) or True
        )
        monkeypatch.setattr(gateway_status, "_pid_exists", lambda pid: False)
        monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)

        clock = [0.0]

        def fake_monotonic():
            clock[0] += 100.0
            return clock[0]

        monkeypatch.setattr("time.monotonic", fake_monotonic)

        # Simulate the desktop backend start triggered by selecting profile
        # "coder_1" (a single reap call, not churn).
        assert gateway._reap_unsupervised_gateway_orphans() is False

        # Neither the selected profile's gateway nor the other profile's gateway
        # was signaled, and no planned-stop marker was written.
        assert killed == []
        assert markers == []
        # Both PIDs are still reported live and unchanged.
        assert gateway.find_gateway_pids() == [coder_1_pid, coder_3_pid]


class TestGatewayHasActiveSupervisor:
    def test_systemd_running(self, monkeypatch):
        monkeypatch.setattr(gateway, "supports_systemd_services", lambda: True)
        monkeypatch.setattr(gateway, "_probe_systemd_service_running", lambda: (False, True))
        monkeypatch.setattr(gateway, "is_macos", lambda: False)
        monkeypatch.setattr(gateway, "is_windows", lambda: False)
        assert gateway._gateway_has_active_supervisor() is True

    def test_launchd_running(self, monkeypatch):
        monkeypatch.setattr(gateway, "supports_systemd_services", lambda: False)
        monkeypatch.setattr(gateway, "is_macos", lambda: True)
        monkeypatch.setattr(gateway, "is_windows", lambda: False)
        monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: _FakePath(True))
        monkeypatch.setattr(gateway, "_probe_launchd_service_running", lambda: True)
        assert gateway._gateway_has_active_supervisor() is True

    def test_launchd_loaded_but_dead_is_not_supervisor(self, monkeypatch):
        # @yun520-1 / @fluxkapacitor edge: the launchd job is *loaded* (plist
        # present) but the gateway is actually dead — KeepAlive didn't fire or
        # it crashed hard — so `launchctl list` returns no PID. The Desktop
        # reaper MUST distinguish "process gone, launchd will handle it" from
        # "process gone, I must handle it". Because the guard requires a live
        # PID (not just a loaded definition), a loaded-but-dead job is NOT
        # treated as an active supervisor: the reap is not short-circuited and
        # `_ensure_desktop_gateway_running()` relaunches the missing gateway on
        # the next desktop (re)start. This is the exact state-reversal hazard
        # the two-owner framing warns about — and it is already handled.
        monkeypatch.setattr(gateway, "supports_systemd_services", lambda: False)
        monkeypatch.setattr(gateway, "is_macos", lambda: True)
        monkeypatch.setattr(gateway, "is_windows", lambda: False)
        monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: _FakePath(True))
        monkeypatch.setattr(gateway, "_probe_launchd_service_running", lambda: False)
        assert gateway._gateway_has_active_supervisor() is False

    def test_windows_scheduled_task_running(self, monkeypatch):
        monkeypatch.setattr(gateway, "supports_systemd_services", lambda: False)
        monkeypatch.setattr(gateway, "is_macos", lambda: False)
        monkeypatch.setattr(gateway, "is_windows", lambda: True)
        import hermes_cli.gateway_windows as gw_win

        monkeypatch.setattr(gw_win, "is_task_registered", lambda: True)
        monkeypatch.setattr(gw_win, "query_task_status", lambda: {"status": "running"})
        monkeypatch.setattr(gw_win, "_gateway_pids", lambda: [])
        assert gateway._gateway_has_active_supervisor() is True

    def test_windows_scheduled_task_live_pid(self, monkeypatch):
        # Task not "Running" but a live gateway pid is present => supervised.
        monkeypatch.setattr(gateway, "supports_systemd_services", lambda: False)
        monkeypatch.setattr(gateway, "is_macos", lambda: False)
        monkeypatch.setattr(gateway, "is_windows", lambda: True)
        import hermes_cli.gateway_windows as gw_win

        monkeypatch.setattr(gw_win, "is_task_registered", lambda: True)
        monkeypatch.setattr(gw_win, "query_task_status", lambda: {"status": "ready"})
        monkeypatch.setattr(gw_win, "_gateway_pids", lambda: [999])
        assert gateway._gateway_has_active_supervisor() is True

    def test_no_supervisor(self, monkeypatch):
        monkeypatch.setattr(gateway, "supports_systemd_services", lambda: False)
        monkeypatch.setattr(gateway, "is_macos", lambda: False)
        monkeypatch.setattr(gateway, "is_windows", lambda: False)
        assert gateway._gateway_has_active_supervisor() is False


class TestPlannedStopMarkerDiscipline:
    """Regression for the planned-stop marker discipline (issue #83683).

    A planned-stop marker is only legitimate when *this* process is about to
    intentionally stop the gateway. The reap path must never write one for a
    *supervised* gateway: doing so records a "clean exit", which under a launchd
    job whose ``KeepAlive.SuccessfulExit`` is false defeats auto-recovery and
    leaves the gateway down indefinitely (see @openclaw-claw's reproduction).
    This class locks in that the reap is a pure no-op — no signal, no marker —
    whenever any platform supervisor owns the gateway.
    """

    @pytest.mark.parametrize(
        "supervisor_setup",
        ["systemd", "launchd", "windows_task"],
    )
    def test_reap_never_writes_marker_under_active_supervisor(
        self, monkeypatch, supervisor_setup
    ):
        if supervisor_setup == "systemd":
            monkeypatch.setattr(gateway, "supports_systemd_services", lambda: True)
            monkeypatch.setattr(gateway, "is_macos", lambda: False)
            monkeypatch.setattr(gateway, "is_windows", lambda: False)
        elif supervisor_setup == "launchd":
            monkeypatch.setattr(gateway, "supports_systemd_services", lambda: False)
            monkeypatch.setattr(gateway, "is_macos", lambda: True)
            monkeypatch.setattr(gateway, "is_windows", lambda: False)
            monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: _FakePath(True))
            monkeypatch.setattr(gateway, "_probe_launchd_service_running", lambda: True)
        else:  # windows_task
            monkeypatch.setattr(gateway, "supports_systemd_services", lambda: False)
            monkeypatch.setattr(gateway, "is_macos", lambda: False)
            monkeypatch.setattr(gateway, "is_windows", lambda: True)
            import hermes_cli.gateway_windows as gw_win

            monkeypatch.setattr(gw_win, "is_installed", lambda: True)
            monkeypatch.setattr(gw_win, "is_task_registered", lambda: True)
            monkeypatch.setattr(gw_win, "query_task_status", lambda: {"status": "running"})
            monkeypatch.setattr(gw_win, "_gateway_pids", lambda: [])

        # Active supervisor => _gateway_has_active_supervisor() is consulted
        # first and short-circuits the reap as a no-op.
        monkeypatch.setattr(gateway, "_gateway_has_active_supervisor", lambda: True)
        monkeypatch.setattr(gateway_status, "get_running_pid", lambda: None)
        monkeypatch.setattr(gateway, "find_gateway_pids", lambda **k: [1234])

        killed = []
        monkeypatch.setattr("os.kill", lambda pid, sig: killed.append((pid, sig)))
        markers = []
        monkeypatch.setattr(
            gateway_status,
            "write_planned_stop_marker",
            lambda *a, **k: markers.append(a) or True,
        )
        monkeypatch.setattr(gateway_status, "_pid_exists", lambda pid: False)
        monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)

        clock = [0.0]

        def fake_monotonic():
            clock[0] += 100.0
            return clock[0]

        monkeypatch.setattr("time.monotonic", fake_monotonic)

        assert gateway._reap_unsupervised_gateway_orphans() is False
        assert killed == []  # never signals a supervised gateway
        assert markers == []  # never records a "clean stop" for it either
