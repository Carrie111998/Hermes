"""Two supervisor-authority leaks found alongside #99450 round 2.

1. **Windows SCM.** ``_stop_runtime_for_quiesce`` stopped an SCM-supervised
   runtime with ``sc.exe stop``, and on an exception logged it and fell
   through — past ``_supervised_stop_command`` (which has no SCM branch, so
   it returns ``None``) and straight into the PID kill. The SCM restarts a
   killed service, on pre-update code, inside the mutation window. A stop we
   could not perform must be a FAILED stop, exactly as it already is for
   systemd and launchd.

2. **launchd domain.** The stop/restart pair addressed every label as
   ``gui/<uid>/<label>``. A LaunchDaemon lives in the ``system`` domain, so
   the bootout targeted a job that does not exist there — reported success
   for a job still running, and then re-bootstrapped it into the wrong
   domain. The domain is now recorded pre-mutation, from the directory the
   job's plist actually lives in, and both commands use it verbatim.
"""

from __future__ import annotations

import pytest

import hermes_cli.update_cmd as update_cmd
import hermes_cli.update_inventory as ui
from hermes_cli import update_quiesce
from hermes_cli.update_inventory import RuntimeRecord

LABEL = "ai.hermes.gateway-zeus"


def _scm_runtime(pid=4242):
    return RuntimeRecord(
        kind="gateway",
        profile="default",
        pid=pid,
        supervisor="windows-service",
        restart_via="windows-service",
        unit="HermesGateway",
        unit_scope="scm",
        detail={"start_time": 1.0},
    )


class TestWindowsServiceStopNeverFallsThroughToAKill:
    def test_a_failing_sc_stop_is_a_failed_stop(self, monkeypatch):
        killed: list = []
        monkeypatch.setattr("hermes_cli.main._is_windows", lambda: True)
        monkeypatch.setattr(
            "hermes_cli.main._stop_windows_gateway_service",
            lambda unit: (_ for _ in ()).throw(OSError("access denied")),
        )
        monkeypatch.setattr(
            "gateway.status.terminate_pid",
            lambda pid, force=False: killed.append((pid, force)),
        )

        assert update_cmd._stop_runtime_for_quiesce(_scm_runtime()) is False
        assert killed == [], "an SCM-supervised runtime must never be PID-killed"

    def test_a_successful_sc_stop_still_reports_success(self, monkeypatch):
        stopped: list = []
        monkeypatch.setattr("hermes_cli.main._is_windows", lambda: True)
        monkeypatch.setattr(
            "hermes_cli.main._stop_windows_gateway_service", stopped.append
        )

        assert update_cmd._stop_runtime_for_quiesce(_scm_runtime()) is True
        assert stopped == ["HermesGateway"]

    def test_an_scm_record_off_windows_is_a_failed_stop(self, monkeypatch):
        """No sc.exe to drive: the service still owns the process."""
        killed: list = []
        monkeypatch.setattr("hermes_cli.main._is_windows", lambda: False)
        monkeypatch.setattr(
            "gateway.status.terminate_pid",
            lambda pid, force=False: killed.append((pid, force)),
        )

        assert update_cmd._stop_runtime_for_quiesce(_scm_runtime()) is False
        assert killed == []

    def test_the_quiesce_aborts_rather_than_mutating(self, monkeypatch):
        monkeypatch.setattr("hermes_cli.main._is_windows", lambda: True)
        monkeypatch.setattr(
            "hermes_cli.main._stop_windows_gateway_service",
            lambda unit: (_ for _ in ()).throw(OSError("access denied")),
        )
        plan = ui.UpdatePlan()
        plan.runtimes = [_scm_runtime()]

        update_quiesce.reset_mutation_authorization()
        with pytest.raises(update_quiesce.QuiesceAbort):
            update_quiesce.run_pre_mutation_quiesce(
                plan,
                stop_runtime=update_cmd._stop_runtime_for_quiesce,
                pid_alive=lambda pid: True,
                assess_isolation=lambda p: update_quiesce.IsolationResult(
                    isolated=True, reason="t"
                ),
                exit_timeout=0.1,
                poll_interval=0.01,
                persist_state=False,
            )
        with pytest.raises(update_quiesce.QuiesceAbort):
            update_quiesce.assert_mutation_authorized("git")


class TestLaunchdKeepsItsRecordedDomain:
    def test_a_daemon_is_booted_out_of_the_domain_it_lives_in(self):
        argv = update_cmd._supervised_stop_command(
            LABEL, "launchd", uid=501, domain="system"
        )
        assert argv == ["launchctl", "bootout", f"system/{LABEL}"]

    def test_a_daemon_is_bootstrapped_back_into_that_same_domain(self):
        argv = update_cmd._supervised_restart_command(
            LABEL,
            "launchd",
            uid=501,
            domain="system",
            plist="/Library/LaunchDaemons/a.plist",
        )
        assert argv == [
            "launchctl",
            "bootstrap",
            "system",
            "/Library/LaunchDaemons/a.plist",
        ]

    def test_a_daemon_kickstart_uses_that_domain_too(self):
        argv = update_cmd._supervised_restart_command(
            LABEL, "launchd", uid=501, domain="system"
        )
        assert argv == ["launchctl", "kickstart", "-k", f"system/{LABEL}"]

    def test_an_unrecorded_domain_still_defaults_to_the_gui_session(self):
        """Backward compatibility with records written before the domain was
        captured — the overwhelmingly common LaunchAgent case."""
        assert update_cmd._supervised_stop_command(LABEL, "launchd", uid=501) == [
            "launchctl",
            "bootout",
            f"gui/501/{LABEL}",
        ]


class TestTheInventoryRecordsTheDomain:
    def _capture(self, monkeypatch, plist_dir, tmp_path):
        plist = plist_dir / f"{LABEL}.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text("<plist/>", encoding="utf-8")
        monkeypatch.setattr(
            ui, "_live_launchd_labels", lambda: {4242: LABEL}
        )
        monkeypatch.setattr(ui, "_windows_service_names_by_pid", lambda: {})
        monkeypatch.setattr(ui, "_default_pid_cgroup", lambda pid: None)
        monkeypatch.setattr(ui, "_process_start_time", lambda pid: 1.0)
        monkeypatch.setattr(
            ui, "_LAUNCHD_PLIST_DIRS", (str(plist_dir),), raising=False
        )
        plan = ui.UpdatePlan()
        plan.runtimes = [
            RuntimeRecord(kind="gateway", profile="default", pid=4242)
        ]
        ui._attach_supervisor_identities(plan)
        return plan.runtimes[0]

    def test_a_launch_daemon_records_the_system_domain(self, monkeypatch, tmp_path):
        row = self._capture(monkeypatch, tmp_path / "LaunchDaemons", tmp_path)
        assert row.unit == LABEL
        assert row.unit_scope == "launchd"
        assert row.detail["launchd_domain"] == "system"

    def test_a_launch_agent_records_the_gui_domain(self, monkeypatch, tmp_path):
        import os

        row = self._capture(monkeypatch, tmp_path / "LaunchAgents", tmp_path)
        uid = os.getuid() if hasattr(os, "getuid") else 0
        assert row.detail["launchd_domain"] == f"gui/{uid}"

    def test_the_stop_uses_the_recorded_domain(self, monkeypatch, tmp_path):
        row = self._capture(monkeypatch, tmp_path / "LaunchDaemons", tmp_path)
        commands: list = []
        monkeypatch.setattr(
            update_cmd,
            "_run_supervisor_command",
            lambda argv: (commands.append(list(argv)) or True),
        )

        assert update_cmd._stop_runtime_for_quiesce(row) is True
        assert commands == [["launchctl", "bootout", f"system/{LABEL}"]]

    def test_the_restart_uses_the_recorded_domain(self, monkeypatch, tmp_path):
        row = self._capture(monkeypatch, tmp_path / "LaunchDaemons", tmp_path)
        commands: list = []
        monkeypatch.setattr(
            update_cmd,
            "_run_supervisor_command",
            lambda argv: (commands.append(list(argv)) or True),
        )
        state = {"runtimes": [row.to_dict()]}

        assert update_cmd._restart_supervised_unit(LABEL, "launchd", state) is True
        assert commands == [
            ["launchctl", "bootstrap", "system", row.detail["plist"]]
        ]
