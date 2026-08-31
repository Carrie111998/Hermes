"""Stop/restart must speak each supervisor's own language, by exact identity.

Pure input→output: the supervisor kind is DATA on the runtime record, not
the host we happen to be running on, so these run everywhere. What they
pin is that a recorded unit/label is handed back verbatim, and that the
stop we issue is one that actually prevents an immediate respawn (a
KeepAlive launchd job restarted by `launchctl stop` would come straight
back on pre-update code — the very skew this work removes).
"""

from __future__ import annotations

import pytest

from hermes_cli import update_cmd


class TestSystemdCommands:
    def test_user_scope_stop(self):
        assert update_cmd._supervised_stop_command(
            "acme-dash.service", "user", uid=501
        ) == ["systemctl", "--user", "--no-ask-password", "stop", "acme-dash.service"]

    def test_system_scope_stop(self):
        argv = update_cmd._supervised_stop_command(
            "acme-dash.service", "system", uid=501, is_root=True
        )
        assert argv == [
            "systemctl",
            "--no-ask-password",
            "stop",
            "acme-dash.service",
        ]

    def test_system_scope_without_root_uses_non_interactive_sudo(self):
        argv = update_cmd._supervised_stop_command(
            "acme-dash.service", "system", uid=501, is_root=False
        )
        assert argv[:2] == ["sudo", "-n"]
        assert argv[-1] == "acme-dash.service"

    def test_restart_mirrors_stop(self):
        assert update_cmd._supervised_restart_command(
            "acme-dash.service", "user", uid=501
        )[-2:] == ["restart", "acme-dash.service"]


class TestLaunchdCommands:
    LABEL = "ai.hermes.gateway-zeus"

    def test_stop_unloads_so_keepalive_cannot_respawn(self):
        argv = update_cmd._supervised_stop_command(self.LABEL, "launchd", uid=501)
        assert argv[:2] == ["launchctl", "bootout"]
        assert argv[-1] == f"gui/501/{self.LABEL}"

    def test_restart_bootstraps_the_recorded_plist(self):
        argv = update_cmd._supervised_restart_command(
            self.LABEL, "launchd", uid=501, plist="/Users/x/Library/LaunchAgents/a.plist"
        )
        assert argv[:3] == ["launchctl", "bootstrap", "gui/501"]
        assert argv[-1] == "/Users/x/Library/LaunchAgents/a.plist"

    def test_restart_without_a_plist_falls_back_to_kickstart(self):
        argv = update_cmd._supervised_restart_command(self.LABEL, "launchd", uid=501)
        assert argv[:2] == ["launchctl", "kickstart"]
        assert argv[-1] == f"gui/501/{self.LABEL}"


class TestUnknownSupervisor:
    @pytest.mark.parametrize("scope", ["", "scm", "nonsense"])
    def test_no_argv_is_invented_for_a_supervisor_we_do_not_drive(self, scope):
        assert update_cmd._supervised_stop_command("x", scope, uid=0) is None
        assert update_cmd._supervised_restart_command("x", scope, uid=0) is None

    def test_empty_unit_never_produces_a_command(self):
        assert update_cmd._supervised_stop_command("", "user", uid=0) is None
        assert update_cmd._supervised_restart_command("", "user", uid=0) is None


class TestSupervisedStopIsFailClosed:
    """A supervised runtime is never stopped by PID.

    ``Restart=always`` means a PID kill is answered by a fresh process on
    pre-update code, inside the mutation window — and the old-PID exit
    check cannot see it, because the replacement has a different PID.
    """

    def _runtime(self, unit="acme-dash.service", scope="user"):
        from hermes_cli.update_inventory import RuntimeRecord

        return RuntimeRecord(
            kind="dashboard",
            profile="default",
            pid=4242,
            supervisor="systemd",
            unit=unit,
            unit_scope=scope,
        )

    def test_a_supervisor_stop_that_fails_is_a_failed_stop(self, monkeypatch):
        terminated: list = []
        monkeypatch.setattr(update_cmd, "_run_supervisor_command", lambda argv: False)
        monkeypatch.setattr(
            "gateway.status.terminate_pid",
            lambda pid, force=False: terminated.append(pid),
        )

        assert update_cmd._stop_runtime_for_quiesce(self._runtime()) is False
        assert terminated == [], "a supervised runtime must not be killed by PID"

    def test_a_supervisor_stop_that_succeeds_is_a_stop(self, monkeypatch):
        issued: list = []
        monkeypatch.setattr(
            update_cmd,
            "_run_supervisor_command",
            lambda argv: (issued.append(list(argv)) or True),
        )

        assert update_cmd._stop_runtime_for_quiesce(self._runtime()) is True
        assert issued == [
            ["systemctl", "--user", "--no-ask-password", "stop", "acme-dash.service"]
        ]

    def test_an_unsupervised_runtime_is_stopped_by_pid(self, monkeypatch):
        terminated: list = []
        monkeypatch.setattr(
            "gateway.status.terminate_pid",
            lambda pid, force=False: terminated.append(pid),
        )

        assert (
            update_cmd._stop_runtime_for_quiesce(self._runtime(unit="", scope=""))
            is True
        )
        assert terminated == [4242]
