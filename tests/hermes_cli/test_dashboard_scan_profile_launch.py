"""Detection of dashboards started from a named profile.

``hermes -p <profile> dashboard`` re-execs into
``python -m hermes_cli.main -p <profile> dashboard ...``.  The literal pattern
list only matches when the subcommand follows the entrypoint directly, so the
global ``-p`` flag in between made every profile-started server invisible to
``--status``, ``--stop`` and the post-update cleanup.

These tests pin both the positive shape and the false positive the
ps-over-pgrep choice exists to avoid: a chat cmdline that merely mentions the
word "dashboard".
"""

from __future__ import annotations

import argparse
import subprocess
from unittest.mock import patch

import pytest

from hermes_cli.dashboard_procs import (
    _SUBCOMMANDS,
    _cmdline_runs_subcommand,
    _is_hermes_entrypoint,
    _scan_dashboard_processes,
)
from hermes_cli.main import _parse_dashboard_runtime, cmd_dashboard

PROFILE_DASHBOARD = (
    "/opt/hermes/venv/bin/python3 -m hermes_cli.main -p carmelo dashboard "
    "--isolated --port 9119 --host 127.0.0.1 --no-open --skip-build"
)
UNIFIED_DASHBOARD = (
    "/opt/hermes/venv/bin/python3 -m hermes_cli.main -p default dashboard "
    "--port 9119 --host 127.0.0.1 --open-profile carmelo --no-open"
)


def _ns(**kw):
    defaults = dict(
        port=9119, host="127.0.0.1", no_open=False, insecure=False,
        stop=False, status=False,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


class TestCmdlineRunsSubcommand:
    @pytest.mark.parametrize(
        "command",
        [
            PROFILE_DASHBOARD,
            UNIFIED_DASHBOARD,
            "/usr/local/bin/hermes dashboard",
            "/usr/local/bin/hermes -p carmelo dashboard",
            "python3 /opt/hermes/hermes_cli/main.py -p carmelo dashboard --port 9120",
            "/opt/hermes/venv/bin/python3 -m hermes_cli.main -p carmelo serve --port 0",
        ],
    )
    def test_matches_real_launches(self, command):
        assert _cmdline_runs_subcommand(command, _SUBCOMMANDS) is True

    @pytest.mark.parametrize(
        "command",
        [
            # Prose only reaches a cmdline as the value of a long option.
            '/usr/local/bin/hermes -p carmelo chat --query fix the dashboard',
            '/usr/local/bin/hermes chat --query please serve the report',
            "/usr/local/bin/hermes chat",
            # Unrelated program that happens to take a "dashboard" argument.
            "/usr/bin/python3 /home/u/report.py dashboard",
        ],
    )
    def test_ignores_non_launches(self, command):
        assert _cmdline_runs_subcommand(command, _SUBCOMMANDS) is False


class TestParseDashboardRuntime:
    def test_profile_launch_is_parsed(self):
        assert _parse_dashboard_runtime(PROFILE_DASHBOARD) == (
            "dashboard",
            "127.0.0.1",
            9119,
        )

    def test_unified_relaunch_is_parsed(self):
        assert _parse_dashboard_runtime(UNIFIED_DASHBOARD) == (
            "dashboard",
            "127.0.0.1",
            9119,
        )

    def test_profile_serve_is_parsed(self):
        command = (
            "/opt/hermes/venv/bin/python3 -m hermes_cli.main -p carmelo serve "
            "--host 0.0.0.0 --port 9200"
        )
        assert _parse_dashboard_runtime(command) == ("serve", "0.0.0.0", 9200)

    def test_chat_mentioning_dashboard_is_not_a_runtime(self):
        command = "/usr/local/bin/hermes -p carmelo chat --query fix the dashboard"
        assert _parse_dashboard_runtime(command) is None


class TestScanFindsProfileLaunch:
    def _ps_output(self):
        return (
            "  1234 /opt/hermes/venv/bin/python3 -m hermes_cli.main -p carmelo "
            "dashboard --isolated --port 9119 --host 127.0.0.1\n"
            "  5678 /usr/local/bin/hermes -p carmelo chat --query fix the dashboard\n"
        )

    def test_scan_returns_profile_launch_only(self):
        completed = subprocess.CompletedProcess(
            args=["ps"], returncode=0, stdout=self._ps_output(), stderr=""
        )
        with patch("sys.platform", "linux"), \
             patch("hermes_cli.dashboard_procs.subprocess.run", return_value=completed):
            found = _scan_dashboard_processes()
        assert [pid for pid, _cmd in found] == [1234]


class TestStatusReportsProfileLaunch:
    def test_status_lists_profile_started_dashboard(self, capsys):
        processes = [(1234, PROFILE_DASHBOARD)]
        with patch("hermes_cli.main._scan_dashboard_processes", return_value=processes), \
             patch("gateway.status._pid_exists", return_value=True), \
             patch("hermes_cli.main._dashboard_listening", return_value=True), \
             pytest.raises(SystemExit) as exc:
            cmd_dashboard(_ns(status=True))
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "1 hermes dashboard process(es) running" in out
        assert "PID 1234" in out


class TestIsHermesEntrypoint:
    """The entrypoint shapes the launcher produces on each platform."""

    @pytest.mark.parametrize(
        "token",
        [
            "/usr/local/bin/hermes",
            "/home/u/.local/bin/hermes",
            'C:\\Users\\u\\AppData\\Local\\hermes\\hermes.exe',
            '"C:\\Users\\u\\AppData\\Local\\hermes\\hermes.exe"',
            "hermes_cli.main",
            "/opt/hermes/hermes_cli/main.py",
            "C:\\hermes\\hermes_cli\\main.py",
        ],
    )
    def test_recognises_entrypoints(self, token):
        assert _is_hermes_entrypoint(token) is True

    @pytest.mark.parametrize(
        "token",
        ["/usr/bin/python3", "/opt/nothermes", "-m", "--port", "dashboard"],
    )
    def test_rejects_everything_else(self, token):
        assert _is_hermes_entrypoint(token) is False


class TestWindowsCmdlineShape:
    """Regression for the Windows .exe entry-point cmdline (issue #86911)."""

    def test_quoted_exe_launch_is_detected(self):
        command = '"C:\\Users\\u\\AppData\\Local\\hermes\\hermes.exe" dashboard --no-open'
        assert _cmdline_runs_subcommand(command, _SUBCOMMANDS) is True

    def test_quoted_exe_launch_is_parsed(self):
        command = (
            '"C:\\Users\\u\\AppData\\Local\\hermes\\hermes.exe" dashboard '
            "--host 127.0.0.1 --port 9119"
        )
        assert _parse_dashboard_runtime(command) == ("dashboard", "127.0.0.1", 9119)
