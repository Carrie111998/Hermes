"""Tests for the stale-dashboard handling run at the end of ``hermes update``.

``hermes update`` detects ``hermes dashboard`` processes left over from the
previous version and kills them (SIGTERM + SIGKILL grace, or ``taskkill /F``
on Windows).  Without this, the running backend silently serves stale Python
against a freshly-updated JS bundle, producing 401s / empty data.

History:
- #16872 introduced the warn-only helper (``_warn_stale_dashboard_processes``).
- #17049 fixed a Windows wmic UnicodeDecodeError crash on non-UTF-8 locales.
- This file now also covers the kill semantics that replaced the warning.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch, MagicMock

import pytest

from hermes_cli.main import (
    _finish_dashboard_update_cleanup,
    _find_stale_dashboard_pids,
    _kill_stale_dashboard_processes,
    _restart_managed_dashboard_service,
    _warn_stale_dashboard_processes,  # back-compat alias
)


@pytest.fixture(autouse=True)
def _refresh_bindings_against_live_module():
    """Rebind module-level names to the *current* ``hermes_cli.main``.

    Other tests in the suite (notably ``test_env_loader.py`` and
    ``test_skills_subparser.py``) reload or delete ``hermes_cli.main`` from
    ``sys.modules``.  When that happens on the same xdist worker before we
    run, our top-of-file ``from hermes_cli.main import ...`` bindings end
    up pointing at the *old* module object.  ``patch(\"hermes_cli.main.X\")``
    then patches the *new* module, but the function we call still resolves
    ``_find_stale_dashboard_pids`` via its stale ``__globals__``, so every
    patch becomes a no-op and the kill path silently returns early.

    Refreshing the bindings (and the patch target) to the live module
    object — and keeping them consistent — makes the tests immune to
    ordering within the worker.  The fix lives in the test module because
    the two pollutants above are load-bearing for their own tests.
    """
    global _finish_dashboard_update_cleanup
    global _find_stale_dashboard_pids
    global _kill_stale_dashboard_processes
    global _restart_managed_dashboard_service
    global _warn_stale_dashboard_processes

    live = sys.modules.get("hermes_cli.main")
    if live is None:
        live = importlib.import_module("hermes_cli.main")

    _finish_dashboard_update_cleanup = live._finish_dashboard_update_cleanup
    _find_stale_dashboard_pids = live._find_stale_dashboard_pids
    _kill_stale_dashboard_processes = live._kill_stale_dashboard_processes
    _restart_managed_dashboard_service = live._restart_managed_dashboard_service
    _warn_stale_dashboard_processes = live._warn_stale_dashboard_processes
    yield


def _ps_line(pid: int, cmd: str) -> str:
    """Format a line as it would appear in ``ps -A -o pid=,command=`` output."""
    return f"{pid:>7} {cmd}"


def _ps_runner(stdout: str):
    """Build a subprocess.run side_effect that only stubs ps -A calls.

    Any other subprocess.run invocation (e.g. taskkill on Windows) is
    handed back as a successful no-op.  This lets tests exercise the real
    scan path without having to re-stub every unrelated subprocess call
    made later in ``_kill_stale_dashboard_processes``.
    """
    def _side_effect(args, *a, **kw):
        if isinstance(args, (list, tuple)) and args and args[0] == "ps":
            return MagicMock(returncode=0, stdout=stdout, stderr="")
        # Any other subprocess.run (e.g. taskkill) — benign success stub.
        return MagicMock(returncode=0, stdout="", stderr="")
    return _side_effect


class TestFindStaleDashboardPids:
    """Unit tests for the ps/wmic-based detection step."""



    @pytest.mark.skipif(sys.platform == "win32", reason="ps-based scan path")
    def test_self_pid_excluded(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="\n".join([
                    _ps_line(os.getpid(), "python3 -m hermes_cli.main dashboard"),
                    _ps_line(12345, "hermes dashboard --port 9119"),
                ]) + "\n",
                stderr="",
            )
            pids = _find_stale_dashboard_pids()
        assert os.getpid() not in pids
        assert 12345 in pids


    def test_ps_timeout_returns_empty(self):
        import subprocess as sp
        with patch("subprocess.run", side_effect=sp.TimeoutExpired("ps", 10)):
            assert _find_stale_dashboard_pids() == []




@pytest.mark.skipif(sys.platform == "win32", reason="POSIX kill semantics")
class TestKillStaleDashboardPosix:
    """Kill path on Linux / macOS: SIGTERM then SIGKILL any survivors."""


    def test_sigterm_graceful_exit(self, capsys):
        """Processes that exit on SIGTERM (the probe gets ProcessLookupError)
        are reported as stopped and SIGKILL is never sent."""
        import signal as _signal

        killed_signals: list[tuple[int, int]] = []

        def fake_kill(pid, sig):
            killed_signals.append((pid, sig))
            if sig == 0:
                # Probe after SIGTERM → "process gone".
                raise ProcessLookupError
            # SIGTERM itself: succeed silently.

        with patch("hermes_cli.main._find_stale_dashboard_pids",
                   return_value=[12345, 12346]), \
             patch("os.kill", side_effect=fake_kill), \
             patch("time.sleep"):
            result = _kill_stale_dashboard_processes()

        # Both got SIGTERM.
        sigterms = [pid for pid, sig in killed_signals if sig == _signal.SIGTERM]
        assert sorted(sigterms) == [12345, 12346]
        # No SIGKILL was needed.
        assert not any(sig == _signal.SIGKILL for _, sig in killed_signals)
        assert result["matched"] == [12345, 12346]
        assert result["killed"] == [12345, 12346]
        assert result["failed"] == []

        out = capsys.readouterr().out
        assert "Stopping 2 dashboard" in out
        assert "✓ stopped PID 12345" in out
        assert "✓ stopped PID 12346" in out
        assert "Restart the dashboard" in out





    @pytest.mark.linux_only
    def test_user_scope_restart_never_falls_back_to_system_or_sudo(self, capsys):
        """A user unit is discovered and restarted through ``systemctl --user``."""
        calls: list[list[str]] = []

        def fake_run(args, *a, **kw):
            calls.append(list(args))
            if args == ["systemctl", "--user", "list-unit-files", "hermes-dashboard.service", "--no-legend", "--no-pager"]:
                return MagicMock(returncode=0, stdout="hermes-dashboard.service enabled enabled\n", stderr="")
            if args == ["systemctl", "--user", "is-active", "hermes-dashboard.service"]:
                return MagicMock(returncode=0, stdout="active\n", stderr="")
            if args == ["systemctl", "--user", "is-enabled", "hermes-dashboard.service"]:
                return MagicMock(returncode=0, stdout="enabled\n", stderr="")
            if args == ["systemctl", "--user", "restart", "hermes-dashboard.service"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected subprocess.run call: {args}")

        with patch("subprocess.run", side_effect=fake_run), \
             patch("hermes_cli.main._find_stale_dashboard_pids", return_value=[12345]) as find_pids, \
             patch("os.kill") as kill:
            _kill_stale_dashboard_processes(restart_managed=True)

        assert calls == [
            ["systemctl", "--user", "list-unit-files", "hermes-dashboard.service", "--no-legend", "--no-pager"],
            ["systemctl", "--user", "is-active", "hermes-dashboard.service"],
            ["systemctl", "--user", "is-enabled", "hermes-dashboard.service"],
            ["systemctl", "--user", "restart", "hermes-dashboard.service"],
        ]
        assert all(call[:1] != ["sudo"] and call[:2] != ["systemctl"] for call in calls)
        find_pids.assert_not_called()
        kill.assert_not_called()
        assert "✓ restarted hermes-dashboard.service" in capsys.readouterr().out

    @pytest.mark.macos_only
    def test_launchctl_missing_response_requires_exact_label(self):
        """A named-profile error must not prove the default job is absent."""
        live = sys.modules["hermes_cli.main"]
        label = "ai.hermes.dashboard"
        other_profile = subprocess.CompletedProcess(
            ["launchctl", "print", f"gui/501/{label}"],
            113,
            "",
            (
                'Could not find service "ai.hermes.dashboard-work" '
                "in domain for user gui: 501"
            ),
        )

        assert not live._launchctl_dashboard_job_missing(other_profile, label)

    @pytest.mark.macos_only
    def test_launchd_managed_dashboard_restarts_without_detached_respawn(
        self, tmp_path, capsys
    ):
        """A loaded LaunchAgent is restarted through launchctl, never raw-killed."""
        live = sys.modules["hermes_cli.main"]
        launch_agents = tmp_path / "Library" / "LaunchAgents"
        calls: list[list[str]] = []

        def fake_run(args, *a, **kw):
            calls.append(list(args))
            if args == ["launchctl", "print", "gui/501/ai.hermes.dashboard"]:
                return MagicMock(returncode=0, stdout="state = running\n", stderr="")
            if args == [
                "launchctl", "kickstart", "-k", "gui/501/ai.hermes.dashboard"
            ]:
                return MagicMock(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected subprocess.run call: {args}")

        with patch.object(live.os, "getuid", return_value=501), \
             patch.object(
                 live,
                 "_dashboard_launchd_label",
                 return_value="ai.hermes.dashboard",
                 create=True,
             ), \
             patch.object(
                 live,
                 "_dashboard_launchd_plist_path",
                 side_effect=lambda label: launch_agents / f"{label}.plist",
             ), \
             patch.object(live.subprocess, "run", side_effect=fake_run), \
             patch.object(live, "_find_stale_dashboard_pids") as find_pids, \
             patch.object(live, "_respawn_dashboard_processes") as respawn, \
             patch.object(live.os, "kill") as kill:
            result = _kill_stale_dashboard_processes(restart_managed=True)

        assert result == {"matched": [], "killed": [], "failed": []}
        assert calls == [
            ["launchctl", "print", "gui/501/ai.hermes.dashboard"],
            ["launchctl", "kickstart", "-k", "gui/501/ai.hermes.dashboard"],
        ]
        find_pids.assert_not_called()
        respawn.assert_not_called()
        kill.assert_not_called()
        assert "✓ restarted ai.hermes.dashboard" in capsys.readouterr().out

    @pytest.mark.macos_only
    def test_launchd_update_finds_default_job_from_named_profile(
        self, tmp_path, capsys
    ):
        """A source update must not raw-kill a default job from another profile."""
        live = sys.modules["hermes_cli.main"]
        launch_agents = tmp_path / "Library" / "LaunchAgents"
        launch_agents.mkdir(parents=True)
        default_plist = launch_agents / "ai.hermes.dashboard.plist"
        default_plist.write_text("<plist/>", encoding="utf-8")
        calls: list[list[str]] = []

        def plist_path(label: str) -> Path:
            return launch_agents / f"{label}.plist"

        def fake_run(args, *a, **kw):
            calls.append(list(args))
            target = args[-1]
            if args[:2] == ["launchctl", "print"]:
                if target == "gui/501/ai.hermes.dashboard":
                    return MagicMock(returncode=0, stdout="state = running\n", stderr="")
                label = target.rsplit("/", 1)[-1]
                return MagicMock(
                    returncode=113,
                    stdout="",
                    stderr=(
                        f'Could not find service "{label}" '
                        "in domain for user gui: 501"
                    ),
                )
            if args == [
                "launchctl", "kickstart", "-k", "gui/501/ai.hermes.dashboard"
            ]:
                return MagicMock(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected subprocess.run call: {args}")

        with patch.object(live.os, "getuid", return_value=501), \
             patch.object(
                 live,
                 "_dashboard_launchd_label",
                 return_value="ai.hermes.dashboard-work",
             ), \
             patch.object(
                 live,
                 "_dashboard_launchd_plist_path",
                 side_effect=plist_path,
             ), \
             patch.object(live.subprocess, "run", side_effect=fake_run), \
             patch.object(live, "_find_stale_dashboard_pids") as find_pids, \
             patch.object(live, "_respawn_dashboard_processes") as respawn, \
             patch.object(live.os, "kill") as kill:
            result = _kill_stale_dashboard_processes(restart_managed=True)

        assert not result["matched"]
        assert [
            "launchctl", "kickstart", "-k", "gui/501/ai.hermes.dashboard"
        ] in calls
        find_pids.assert_not_called()
        respawn.assert_not_called()
        kill.assert_not_called()
        assert "✓ restarted ai.hermes.dashboard" in capsys.readouterr().out

    @pytest.mark.macos_only
    def test_launchd_update_restarts_all_loaded_profile_jobs(self, tmp_path, capsys):
        """Every conventional loaded Dashboard job sharing the source is restarted."""
        live = sys.modules["hermes_cli.main"]
        launch_agents = tmp_path / "Library" / "LaunchAgents"
        launch_agents.mkdir(parents=True)
        labels = ("ai.hermes.dashboard", "ai.hermes.dashboard-work")
        for label in labels:
            (launch_agents / f"{label}.plist").write_text(
                "<plist/>", encoding="utf-8"
            )
        calls: list[list[str]] = []

        def plist_path(label: str) -> Path:
            return launch_agents / f"{label}.plist"

        def fake_run(args, *a, **kw):
            calls.append(list(args))
            target = args[-1]
            if args[:2] == ["launchctl", "print"] and target in {
                f"gui/501/{label}" for label in labels
            }:
                return MagicMock(returncode=0, stdout="state = running\n", stderr="")
            if args[:3] == ["launchctl", "kickstart", "-k"] and target in {
                f"gui/501/{label}" for label in labels
            }:
                return MagicMock(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected subprocess.run call: {args}")

        with patch.object(live.os, "getuid", return_value=501), \
             patch.object(
                 live,
                 "_dashboard_launchd_label",
                 return_value="ai.hermes.dashboard-work",
             ), \
             patch.object(
                 live,
                 "_dashboard_launchd_plist_path",
                 side_effect=plist_path,
             ), \
             patch.object(live.subprocess, "run", side_effect=fake_run), \
             patch.object(live, "_find_stale_dashboard_pids") as find_pids, \
             patch.object(live, "_respawn_dashboard_processes") as respawn, \
             patch.object(live.os, "kill") as kill:
            result = _kill_stale_dashboard_processes(restart_managed=True)

        assert result == {"matched": [], "killed": [], "failed": []}
        assert {
            tuple(["launchctl", "kickstart", "-k", f"gui/501/{label}"])
            for label in labels
        } <= {tuple(call) for call in calls}
        find_pids.assert_not_called()
        respawn.assert_not_called()
        kill.assert_not_called()
        output = capsys.readouterr().out
        assert "✓ restarted ai.hermes.dashboard" in output
        assert "✓ restarted ai.hermes.dashboard-work" in output

    @pytest.mark.macos_only
    def test_launchd_profile_probe_is_all_or_nothing(self, tmp_path, capsys):
        """A later ambiguous profile probe prevents every supervisor mutation."""
        live = sys.modules["hermes_cli.main"]
        launch_agents = tmp_path / "Library" / "LaunchAgents"
        launch_agents.mkdir(parents=True)
        labels = ("ai.hermes.dashboard", "ai.hermes.dashboard-work")
        for label in labels:
            (launch_agents / f"{label}.plist").write_text(
                "<plist/>", encoding="utf-8"
            )
        calls: list[list[str]] = []

        def plist_path(label: str) -> Path:
            return launch_agents / f"{label}.plist"

        def fake_run(args, *a, **kw):
            calls.append(list(args))
            target = args[-1]
            if args == ["launchctl", "print", "gui/501/ai.hermes.dashboard"]:
                return MagicMock(returncode=0, stdout="state = running\n", stderr="")
            if args == ["launchctl", "print", "gui/501/ai.hermes.dashboard-work"]:
                raise subprocess.TimeoutExpired(args, 10)
            raise AssertionError(f"unexpected subprocess.run call: {args}")

        with patch.object(live.os, "getuid", return_value=501), \
             patch.object(
                 live,
                 "_dashboard_launchd_label",
                 return_value="ai.hermes.dashboard-work",
             ), \
             patch.object(
                 live,
                 "_dashboard_launchd_plist_path",
                 side_effect=plist_path,
             ), \
             patch.object(live.subprocess, "run", side_effect=fake_run), \
             patch.object(live, "_find_stale_dashboard_pids") as find_pids, \
             patch.object(live, "_respawn_dashboard_processes") as respawn, \
             patch.object(live.os, "kill") as kill:
            result = _kill_stale_dashboard_processes(restart_managed=True)

        assert result == {"matched": [], "killed": [], "failed": []}
        assert not any(call[:3] == ["launchctl", "kickstart", "-k"] for call in calls)
        find_pids.assert_not_called()
        respawn.assert_not_called()
        kill.assert_not_called()
        assert "ownership is unknown" in capsys.readouterr().out

    @pytest.mark.macos_only
    def test_launchd_profile_restart_failure_never_uses_detached_fallback(
        self, tmp_path, capsys
    ):
        """One failed profile restart is reported while other loaded jobs continue."""
        live = sys.modules["hermes_cli.main"]
        launch_agents = tmp_path / "Library" / "LaunchAgents"
        launch_agents.mkdir(parents=True)
        labels = ("ai.hermes.dashboard", "ai.hermes.dashboard-work")
        for label in labels:
            (launch_agents / f"{label}.plist").write_text(
                "<plist/>", encoding="utf-8"
            )
        calls: list[list[str]] = []

        def plist_path(label: str) -> Path:
            return launch_agents / f"{label}.plist"

        def fake_run(args, *a, **kw):
            calls.append(list(args))
            target = args[-1]
            if args[:2] == ["launchctl", "print"] and target in {
                f"gui/501/{label}" for label in labels
            }:
                return MagicMock(returncode=0, stdout="state = running\n", stderr="")
            if args == [
                "launchctl", "kickstart", "-k", "gui/501/ai.hermes.dashboard"
            ]:
                return MagicMock(returncode=5, stdout="", stderr="not permitted")
            if args == [
                "launchctl", "kickstart", "-k", "gui/501/ai.hermes.dashboard-work"
            ]:
                return MagicMock(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected subprocess.run call: {args}")

        with patch.object(live.os, "getuid", return_value=501), \
             patch.object(
                 live,
                 "_dashboard_launchd_label",
                 return_value="ai.hermes.dashboard-work",
             ), \
             patch.object(
                 live,
                 "_dashboard_launchd_plist_path",
                 side_effect=plist_path,
             ), \
             patch.object(live.subprocess, "run", side_effect=fake_run), \
             patch.object(live, "_find_stale_dashboard_pids") as find_pids, \
             patch.object(live, "_respawn_dashboard_processes") as respawn, \
             patch.object(live.os, "kill") as kill:
            result = _kill_stale_dashboard_processes(restart_managed=True)

        assert result == {"matched": [], "killed": [], "failed": []}
        for label in labels:
            assert [
                "launchctl", "kickstart", "-k", f"gui/501/{label}"
            ] in calls
        find_pids.assert_not_called()
        respawn.assert_not_called()
        kill.assert_not_called()
        output = capsys.readouterr().out
        assert "failed to restart ai.hermes.dashboard: not permitted" in output
        assert "✓ restarted ai.hermes.dashboard-work" in output
        assert "refusing detached fallback" in output

    @pytest.mark.macos_only
    def test_launchd_ambiguous_probe_failure_refuses_detached_fallback(
        self, tmp_path, capsys
    ):
        """Unknown launchd ownership must not degrade to PID kill and respawn."""
        live = sys.modules["hermes_cli.main"]
        launch_agents = tmp_path / "Library" / "LaunchAgents"

        with patch.object(live.os, "getuid", return_value=501), \
             patch.object(
                 live,
                 "_dashboard_launchd_label",
                 return_value="ai.hermes.dashboard",
                 create=True,
             ), \
             patch.object(
                 live,
                 "_dashboard_launchd_plist_path",
                 side_effect=lambda label: launch_agents / f"{label}.plist",
             ), \
             patch.object(
                 live.subprocess,
                 "run",
                 side_effect=subprocess.TimeoutExpired(["launchctl", "print"], 10),
             ), \
             patch.object(live, "_find_stale_dashboard_pids") as find_pids, \
             patch.object(live, "_respawn_dashboard_processes") as respawn, \
             patch.object(live.os, "kill") as kill:
            result = _kill_stale_dashboard_processes(restart_managed=True)

        assert result == {"matched": [], "killed": [], "failed": []}
        find_pids.assert_not_called()
        respawn.assert_not_called()
        kill.assert_not_called()
        assert "refusing detached fallback" in capsys.readouterr().out

    @pytest.mark.macos_only
    def test_launchd_missing_job_with_plist_refuses_detached_fallback(
        self, tmp_path, capsys
    ):
        """An installed but unloaded LaunchAgent stays supervisor-owned."""
        live = sys.modules["hermes_cli.main"]
        plist = tmp_path / "Library" / "LaunchAgents" / "ai.hermes.dashboard.plist"
        plist.parent.mkdir(parents=True)
        plist.write_text("<plist/>", encoding="utf-8")
        missing = MagicMock(
            returncode=113,
            stdout="",
            stderr=(
                'Could not find service "ai.hermes.dashboard" '
                "in domain for user gui: 501"
            ),
        )

        with patch.object(live.os, "getuid", return_value=501), \
             patch.object(
                 live,
                 "_dashboard_launchd_label",
                 return_value="ai.hermes.dashboard",
                 create=True,
             ), \
             patch.object(
                 live,
                 "_dashboard_launchd_plist_path",
                 return_value=plist,
                 create=True,
             ), \
             patch.object(live.subprocess, "run", return_value=missing), \
             patch.object(live, "_find_stale_dashboard_pids") as find_pids, \
             patch.object(live, "_respawn_dashboard_processes") as respawn, \
             patch.object(live.os, "kill") as kill:
            result = _kill_stale_dashboard_processes(restart_managed=True)

        assert result == {"matched": [], "killed": [], "failed": []}
        find_pids.assert_not_called()
        respawn.assert_not_called()
        kill.assert_not_called()
        assert "configured but not loaded" in capsys.readouterr().out

    @pytest.mark.macos_only
    def test_launchd_missing_job_without_plist_keeps_manual_respawn(
        self, tmp_path, capsys
    ):
        """Definitively unmanaged macOS dashboards retain the existing update path."""
        live = sys.modules["hermes_cli.main"]
        argv = ["hermes", "dashboard", "--port", "8300"]
        missing = MagicMock(
            returncode=113,
            stdout="",
            stderr=(
                'Could not find service "ai.hermes.dashboard" '
                "in domain for user gui: 501"
            ),
        )
        plist = tmp_path / "Library" / "LaunchAgents" / "ai.hermes.dashboard.plist"

        with patch.object(live.os, "getuid", return_value=501), \
             patch.object(
                 live,
                 "_dashboard_launchd_label",
                 return_value="ai.hermes.dashboard",
                 create=True,
             ), \
             patch.object(
                 live,
                 "_dashboard_launchd_plist_path",
                 return_value=plist,
                 create=True,
             ), \
             patch.object(live.subprocess, "run", return_value=missing), \
             patch.object(live, "_find_stale_dashboard_pids", return_value=[6001]), \
             patch.object(live, "_get_pid_cgroup_path", return_value=None), \
             patch.object(live, "_get_systemd_service_for_pid", return_value=None), \
             patch.object(live, "_dashboard_cmdline_for_pid", return_value=argv), \
             patch("hermes_cli.dashboard_procs._hermes_home_for_pid", return_value=None), \
             patch.object(live, "_respawn_dashboard_processes", return_value=[]) as respawn, \
             patch.object(live.os, "kill"), \
             patch("gateway.status._pid_exists", return_value=False), \
             patch("time.sleep"):
            result = _kill_stale_dashboard_processes(restart_managed=True)

        assert result["killed"] == [6001]
        respawn.assert_called_once_with([argv])
        assert "refusing detached fallback" not in capsys.readouterr().out




class TestKillStaleDashboardWindows:
    """Kill path on Windows: taskkill /F."""

    @pytest.mark.windows_only
    def test_taskkill_invoked_for_each_pid(self, capsys):
        """``windows_only``: ``taskkill.exe`` only exists on Windows, and the
        faked platform also silently skipped the POSIX-only cgroup/argv
        snapshot the real Windows path must not take.
        """

        def fake_run(args, *a, **kw):
            # taskkill returns 0 on success
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("hermes_cli.main._find_stale_dashboard_pids",
                   return_value=[12345, 12346]), \
             patch("subprocess.run", side_effect=fake_run) as mock_run:
            _kill_stale_dashboard_processes()

        # Each PID triggered a taskkill /PID <n> /F invocation.
        taskkill_calls = [
            c for c in mock_run.call_args_list
            if c.args and isinstance(c.args[0], list) and c.args[0][:1] == ["taskkill"]
        ]
        assert len(taskkill_calls) == 2
        assert ["taskkill", "/PID", "12345", "/F"] in [c.args[0] for c in taskkill_calls]
        assert ["taskkill", "/PID", "12346", "/F"] in [c.args[0] for c in taskkill_calls]

        out = capsys.readouterr().out
        assert "✓ stopped PID 12345" in out
        assert "✓ stopped PID 12346" in out


class TestBackCompatAlias:
    """``_warn_stale_dashboard_processes`` is kept as an alias for the
    new kill function so old imports don't break."""

    def test_alias_is_the_kill_function(self):
        assert _warn_stale_dashboard_processes is _kill_stale_dashboard_processes


class TestDashboardUpdateCleanup:
    """The git and Windows ZIP update paths share this final cleanup."""

    def test_all_failed_stops_do_not_claim_the_dashboard_was_stopped(self, capsys):
        with patch(
            "hermes_cli.main._kill_stale_dashboard_processes",
            return_value={"matched": [12345], "killed": [], "failed": [(12345, "denied")],
                          "unrecovered": []},
        ):
            _finish_dashboard_update_cleanup([])

        assert "stopped during update" not in capsys.readouterr().out


class TestWindowsWmicEncoding:
    """Regression tests for #17049 — the Windows wmic branch must not crash
    `hermes update` on non-UTF-8 system locales (e.g. cp936 on zh-CN).
    """

    @pytest.mark.windows_only
    def test_wmic_invoked_with_utf8_ignore_errors(self):
        """The wmic subprocess.run call must pass encoding='utf-8' and
        errors='ignore' so the subprocess reader thread cannot raise
        UnicodeDecodeError on non-UTF-8 wmic output.

        ``windows_only``: the branch also imports ``windows_hide_flags()`` and
        the crash it guards is a real cp936/wmic decode — neither reproducible
        with a patched ``sys.platform`` on Linux.
        """
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=(
                    "CommandLine=python -m hermes_cli.main dashboard\n"
                    "ProcessId=12345\n"
                ),
                stderr="",
            )
            _find_stale_dashboard_pids()

        # The wmic call is the first subprocess.run invocation.
        assert mock_run.called, "subprocess.run was not invoked"
        wmic_call = mock_run.call_args_list[0]
        kwargs = wmic_call.kwargs
        assert kwargs.get("encoding") == "utf-8", (
            "encoding kwarg must be 'utf-8' so wmic output is decoded "
            "deterministically rather than via the implicit reader-thread "
            "default that crashes on non-UTF-8 locales (#17049)."
        )
        assert kwargs.get("errors") == "ignore", (
            "errors kwarg must be 'ignore' so undecodable bytes don't take "
            "down the reader thread (#17049)."
        )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX kill + systemd restart")
class TestSupervisedBackendRestart:
    """After the kill, systemd-supervised PIDs get their owning unit
    restarted (#68934) — SIGTERM reads as a clean stop to systemd, so
    Restart=on-failure never fires on its own."""

    def _live(self):
        return sys.modules["hermes_cli.main"]

    def test_supervised_pid_restarts_owning_unit(self, capsys):
        """A killed PID whose cgroup names a custom unit → systemctl restart."""
        live = self._live()

        def fake_kill(pid, sig):
            if sig == 0:
                raise ProcessLookupError

        with patch.object(live, "_restart_managed_dashboard_service", return_value=False), \
             patch.object(live, "_find_stale_dashboard_pids", return_value=[4321]), \
             patch.object(live, "_get_pid_cgroup_path",
                          return_value="/system.slice/hermes-serve.service"), \
             patch.object(live, "_get_systemd_service_for_pid",
                          return_value="hermes-serve.service"), \
             patch.object(live, "_try_restart_systemd_service", return_value=True) as restart, \
             patch("os.kill", side_effect=fake_kill), \
             patch("time.sleep"):
            _kill_stale_dashboard_processes(restart_managed=True)

        restart.assert_called_once_with(
            "hermes-serve.service", "/system.slice/hermes-serve.service"
        )
        out = capsys.readouterr().out
        assert "✓ restarted systemd service hermes-serve.service" in out
        # Supervised restart succeeded — no manual hint.
        assert "when you're ready" not in out


class TestManualBackendRespawn:
    """Manually-started dashboards/serves have their argv captured before the
    kill and are respawned detached after the update (#40449)."""

    def _live(self):
        return sys.modules["hermes_cli.main"]


    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX cmdline capture + respawn")
    def test_argv_capture_failure_falls_back_to_hint(self, capsys):
        live = self._live()

        def fake_kill(pid, sig):
            if sig == 0:
                raise ProcessLookupError

        with patch.object(live, "_restart_managed_dashboard_service", return_value=False), \
             patch.object(live, "_find_stale_dashboard_pids", return_value=[5555]), \
             patch.object(live, "_get_pid_cgroup_path", return_value=None), \
             patch.object(live, "_get_systemd_service_for_pid", return_value=None), \
             patch.object(live, "_dashboard_cmdline_for_pid", return_value=None), \
             patch.object(live, "_respawn_dashboard_processes") as respawn, \
             patch("os.kill", side_effect=fake_kill), \
             patch("time.sleep"):
            _kill_stale_dashboard_processes(restart_managed=True)

        respawn.assert_not_called()
        out = capsys.readouterr().out
        assert "Restart anything not auto-restarted" in out

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX cmdline capture + respawn")
    def test_non_orphan_fixed_port_still_respawns(self, capsys):
        """A supervised-by-shell dashboard with a fixed port is still restarted."""
        live = self._live()
        argv = ["hermes", "dashboard", "--port", "8300"]

        def fake_kill(pid, sig):
            if sig == 0:
                raise ProcessLookupError

        with patch.object(live, "_restart_managed_dashboard_service", return_value=False), \
             patch.object(live, "_find_stale_dashboard_pids", return_value=[6001]), \
             patch.object(live, "_get_pid_cgroup_path", return_value=None), \
             patch.object(live, "_get_systemd_service_for_pid", return_value=None), \
             patch.object(live, "_dashboard_cmdline_for_pid", return_value=argv), \
             patch("hermes_cli.dashboard_procs._hermes_home_for_pid", return_value=None), \
             patch.object(live, "_respawn_dashboard_processes", return_value=[]) as respawn, \
             patch("os.kill", side_effect=fake_kill), \
             patch("time.sleep"):
            _kill_stale_dashboard_processes(restart_managed=True)

        respawn.assert_called_once_with([argv])
        assert "when you're ready" not in capsys.readouterr().out

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX cmdline capture + respawn")
    def test_port_zero_serves_killed_without_respawn(self, capsys):
        """``serve --port 0`` backends are stopped but not resurrected (#78821)."""
        live = self._live()
        argv = [
            "python", "-m", "hermes_cli.main",
            "serve", "--host", "127.0.0.1", "--port", "0",
        ]

        def fake_kill(pid, sig):
            if sig == 0:
                raise ProcessLookupError

        with patch.object(live, "_restart_managed_dashboard_service", return_value=False), \
             patch.object(live, "_find_stale_dashboard_pids",
                          return_value=[7001, 7002, 7003]), \
             patch.object(live, "_get_pid_cgroup_path", return_value=None), \
             patch.object(live, "_get_systemd_service_for_pid", return_value=None), \
             patch.object(live, "_dashboard_cmdline_for_pid", return_value=argv), \
             patch("hermes_cli.dashboard_procs._hermes_home_for_pid", return_value=None), \
             patch.object(live, "_respawn_dashboard_processes") as respawn, \
             patch("os.kill", side_effect=fake_kill), \
             patch("time.sleep"):
            result = _kill_stale_dashboard_processes(restart_managed=True)

        respawn.assert_not_called()
        assert sorted(result["killed"]) == [7001, 7002, 7003]
        # Intentional skips are not "unrecovered" — no noisy manual hint.
        assert result["unrecovered"] == []
        assert "when you're ready" not in capsys.readouterr().out

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX cmdline capture + respawn")
    def test_detached_fixed_port_still_respawns_after_prior_update(self, capsys):
        """PPID-1 fixed-port backends (prior start_new_session respawn) stay eligible."""
        live = self._live()
        argv = ["hermes", "dashboard", "--port", "8300"]

        def fake_kill(pid, sig):
            if sig == 0:
                raise ProcessLookupError

        with patch.object(live, "_restart_managed_dashboard_service", return_value=False), \
             patch.object(live, "_find_stale_dashboard_pids", return_value=[8001]), \
             patch.object(live, "_get_pid_cgroup_path", return_value=None), \
             patch.object(live, "_get_systemd_service_for_pid", return_value=None), \
             patch.object(live, "_dashboard_cmdline_for_pid", return_value=argv), \
             patch("hermes_cli.dashboard_procs._hermes_home_for_pid", return_value=None), \
             patch.object(live, "_respawn_dashboard_processes", return_value=[]) as respawn, \
             patch("os.kill", side_effect=fake_kill), \
             patch("time.sleep"):
            _kill_stale_dashboard_processes(restart_managed=True)

        respawn.assert_called_once_with([argv])
        assert "when you're ready" not in capsys.readouterr().out

    def test_respawn_adds_no_open_to_dashboard_commands(self, tmp_path, monkeypatch):
        """Respawned `dashboard` argv gains --no-open; `serve` argv untouched."""
        live = self._live()
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        spawned: list[list[str]] = []

        class _FakePopen:
            def __init__(self, cmd, **kwargs):
                spawned.append(list(cmd))

        with patch.object(live.subprocess, "Popen", _FakePopen):
            failed = live._respawn_dashboard_processes([
                ["hermes", "dashboard", "--port", "8300"],
                ["hermes", "serve", "--host", "0.0.0.0"],
            ])

        assert failed == []
        assert spawned[0] == ["hermes", "dashboard", "--port", "8300", "--no-open"]
        assert spawned[1] == ["hermes", "serve", "--host", "0.0.0.0"]

    def test_respawn_failure_returned(self, tmp_path, monkeypatch, capsys):
        live = self._live()
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

        with patch.object(live.subprocess, "Popen", side_effect=OSError("no such file")):
            failed = live._respawn_dashboard_processes([["hermes", "serve"]])

        assert failed == [["hermes", "serve"]]
        out = capsys.readouterr().out
        assert "✗ failed to restart" in out


class TestFilterDashboardRespawnCandidates:
    """Unit tests for respawn filtering / dedupe / orphan skip (#78821)."""

    def test_skips_serve_port_zero(self):
        from hermes_cli.dashboard_procs import _filter_dashboard_respawn_candidates

        argv = [
            "python", "-m", "hermes_cli.main",
            "--profile", "mini-cat",
            "serve", "--host", "127.0.0.1", "--port", "0",
        ]
        assert _filter_dashboard_respawn_candidates([
            (42, argv, "/home/u/.hermes/profiles/mini-cat"),
        ]) == []

    def test_skips_legacy_dashboard_port_zero(self):
        from hermes_cli.dashboard_procs import _filter_dashboard_respawn_candidates

        argv = [
            "hermes", "--profile", "coder",
            "dashboard", "--no-open", "--host", "127.0.0.1", "--port", "0",
        ]
        assert _filter_dashboard_respawn_candidates([(7, argv, None)]) == []

    def test_skips_serve_port_equals_zero(self):
        from hermes_cli.dashboard_procs import _filter_dashboard_respawn_candidates

        argv = ["hermes", "serve", "--port=0"]
        assert _filter_dashboard_respawn_candidates([(1, argv, None)]) == []

    def test_keeps_ppid1_fixed_port_for_repeat_update(self):
        """Detached prior-update respawns (PPID 1) must remain restartable (#40449)."""
        from hermes_cli.dashboard_procs import _filter_dashboard_respawn_candidates

        argv = ["hermes", "dashboard", "--port", "9119"]
        assert _filter_dashboard_respawn_candidates([(10, argv, None)]) == [argv]

    def test_dedupes_identical_normalized_cmdlines(self):
        from hermes_cli.dashboard_procs import _filter_dashboard_respawn_candidates

        a = ["/usr/bin/python3", "-m", "hermes_cli.main", "dashboard", "--port", "8300"]
        b = ["/other/python", "-m", "hermes_cli.main", "dashboard", "--port", "8300"]
        out = _filter_dashboard_respawn_candidates([
            (1, a, None),
            (2, b, None),
        ])
        assert out == [a]

    def test_caps_one_per_profile(self):
        from hermes_cli.dashboard_procs import _filter_dashboard_respawn_candidates

        a = ["hermes", "--profile", "coder", "dashboard", "--port", "8300"]
        b = ["hermes", "--profile", "coder", "dashboard", "--port", "8301"]
        c = ["hermes", "--profile", "writer", "dashboard", "--port", "8302"]
        out = _filter_dashboard_respawn_candidates([
            (1, a, None),
            (2, b, None),
            (3, c, None),
        ])
        assert out == [a, c]

    def test_caps_one_per_hermes_home(self):
        from hermes_cli.dashboard_procs import _filter_dashboard_respawn_candidates

        home = "/tmp/hermes-home-a"
        a = ["hermes", "dashboard", "--port", "8300"]
        b = ["hermes", "dashboard", "--port", "8301"]
        out = _filter_dashboard_respawn_candidates([
            (1, a, home),
            (2, b, home),
        ])
        assert out == [a]

    def test_profile_flag_and_profiles_home_share_cap(self):
        from hermes_cli.dashboard_procs import _filter_dashboard_respawn_candidates

        a = ["hermes", "--profile", "coder", "dashboard", "--port", "8300"]
        b = ["hermes", "dashboard", "--port", "8301"]
        out = _filter_dashboard_respawn_candidates([
            (1, a, None),
            (2, b, "/home/u/.hermes/profiles/coder"),
        ])
        assert out == [a]

    def test_default_profile_same_root_home_caps(self):
        from hermes_cli.dashboard_procs import _filter_dashboard_respawn_candidates

        a = ["hermes", "--profile", "default", "dashboard", "--port", "8300"]
        b = ["hermes", "dashboard", "--port", "8301"]
        home = "/home/u/.hermes"
        out = _filter_dashboard_respawn_candidates([
            (1, a, home),
            (2, b, home),
        ])
        assert out == [a]

    def test_distinct_dot_hermes_homes_do_not_share_cap(self):
        from hermes_cli.dashboard_procs import _filter_dashboard_respawn_candidates

        a = ["hermes", "dashboard", "--port", "8300"]
        b = ["hermes", "dashboard", "--port", "8301"]
        out = _filter_dashboard_respawn_candidates([
            (1, a, "/home/u/.hermes"),
            (2, b, "/work/project/.hermes"),
        ])
        assert out == [a, b]

    def test_keeps_fixed_port_serve(self):
        from hermes_cli.dashboard_procs import _filter_dashboard_respawn_candidates

        argv = ["hermes", "serve", "--host", "0.0.0.0", "--port", "9119"]
        assert _filter_dashboard_respawn_candidates([
            (9, argv, None),
        ]) == [argv]

    def test_seventeen_port_zero_orphans_collapse_to_zero(self):
        """The reported accumulation case: many identical serve --port 0 → none."""
        from hermes_cli.dashboard_procs import _filter_dashboard_respawn_candidates

        argv = [
            "python", "-m", "hermes_cli.main",
            "serve", "--host", "127.0.0.1", "--port", "0",
        ]
        candidates = [(i, argv, None) for i in range(17)]
        assert _filter_dashboard_respawn_candidates(candidates) == []


class TestCmdlineCapture:
    """_dashboard_cmdline_for_pid reads /proc on Linux, ps on macOS."""

    def _live(self):
        return sys.modules["hermes_cli.main"]

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX /proc cmdline path")
    def test_reads_proc_cmdline_when_available(self, tmp_path, monkeypatch):
        live = self._live()
        proc_file = tmp_path / "cmdline"
        proc_file.write_bytes(b"/usr/bin/python3\x00-m\x00hermes_cli.main\x00serve\x00")

        real_exists = os.path.exists

        def fake_exists(path):
            if path == "/proc/777/cmdline":
                return True
            return real_exists(path)

        real_open = open

        def fake_open(path, *a, **kw):
            if path == "/proc/777/cmdline":
                return real_open(proc_file, *a, **kw)
            return real_open(path, *a, **kw)

        with patch.object(live.os.path, "exists", fake_exists), \
             patch("builtins.open", fake_open):
            argv = live._dashboard_cmdline_for_pid(777)

        assert argv == ["/usr/bin/python3", "-m", "hermes_cli.main", "serve"]

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX ps cmdline fallback")
    def test_falls_back_to_ps_without_proc(self, monkeypatch):
        live = self._live()

        def fake_run(args, *a, **kw):
            assert args == ["ps", "-p", "888", "-o", "command="]
            return MagicMock(returncode=0, stdout="hermes serve --port 8300\n", stderr="")

        with patch.object(live.os.path, "exists", return_value=False), \
             patch("subprocess.run", side_effect=fake_run):
            argv = live._dashboard_cmdline_for_pid(888)

        assert argv == ["hermes", "serve", "--port", "8300"]

    @pytest.mark.windows_only
    def test_returns_none_on_windows(self):
        """``windows_only``: the contract is "no graceful-argv capture on a
        real Windows host" — asserting it against a faked platform only
        restated the branch condition.
        """
        live = self._live()
        assert live._dashboard_cmdline_for_pid(123) is None
