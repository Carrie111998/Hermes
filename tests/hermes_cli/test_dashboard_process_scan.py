"""Tests for the ``hermes dashboard`` process-table scan.

Two independent defects, both found 2026-08-12 on Windows 11 Pro for
Workstations 10.0.26200 while restarting the :9119 dashboard:

1. The Windows branch shelled out to ``wmic``, which Microsoft has removed
   from Windows 11.  ``FileNotFoundError`` was swallowed by a broad
   ``except`` that returned ``[]`` — indistinguishable from "nothing
   running".  ``hermes dashboard --stop`` printed a false clean and
   ``hermes update`` silently left a stale dashboard serving old Python.

2. Matching was plain substring against patterns like
   ``"hermes_cli.main dashboard"``.  The live server's argv is
   ``python -m hermes_cli.main -p default dashboard --port 9119``: the
   global ``-p default`` selector sits between the two halves, so nothing
   matched.  The console-script form ``"...\\hermes.exe" dashboard`` missed
   too, because ``.exe"`` occupies the byte the pattern expects a space in.

The scan now runs on psutil (already a hard dependency) with a token-aware
matcher, and reports scan failure distinctly from an empty result.
"""

from __future__ import annotations

import argparse
import os
import sys
from unittest.mock import patch

import pytest

from hermes_cli import main


# This file is about the scanner itself, so it opts out of the autouse stub
# that defaults ``_find_stale_dashboard_pids`` to ``[]`` (tests/conftest.py::
# _pid_scan_guard). Opting out is safe here and stays fast: every test below
# fakes the process table one layer *beneath* the scanner — the ``fake_psutil``
# fixture, an explicit ``patch.object(psutil, ...)``, or a ``patch`` of the
# scan function itself — so nothing reaches the host's 1000-odd processes.
pytestmark = pytest.mark.real_dashboard_pid_scan


# ── Fixtures / helpers ───────────────────────────────────────────────

class _FakeProc:
    """Stand-in for a psutil.Process yielded by ``process_iter``."""

    def __init__(self, pid, cmdline, raises=None):
        self._raises = raises
        self.info = {"pid": pid, "cmdline": cmdline}

    @property
    def info(self):
        if self._raises is not None:
            raise self._raises
        return self._info

    @info.setter
    def info(self, value):
        self._info = value


def _iter_returning(*procs):
    """Build a psutil.process_iter stand-in yielding *procs*."""
    def _fake_process_iter(attrs=None, ad_value=None):
        return iter(procs)
    return _fake_process_iter


@pytest.fixture
def fake_psutil(monkeypatch):
    """Patch the real psutil module's process_iter; yields a setter."""
    import psutil

    def _install(*procs):
        monkeypatch.setattr(psutil, "process_iter", _iter_returning(*procs))

    def _install_failing(exc):
        def _boom(attrs=None, ad_value=None):
            raise exc
        monkeypatch.setattr(psutil, "process_iter", _boom)

    _install.failing = _install_failing
    return _install


# ── The matcher ──────────────────────────────────────────────────────

class TestDashboardCommandLineMatcher:
    """``_looks_like_dashboard_command_line`` accepts argv lists or strings."""

    def test_matches_module_form_with_interleaved_profile_flag(self):
        """The exact argv of the live :9119 server (DEFECT 2).

        ``-p default`` sits between ``hermes_cli.main`` and ``dashboard``;
        the old substring pattern ``"hermes_cli.main dashboard"`` missed it.
        """
        argv = [
            r"C:\Python311\python.exe", "-m", "hermes_cli.main",
            "-p", "default", "dashboard", "--port", "9119",
            "--host", "127.0.0.1", "--open-profile", "main", "--no-open",
        ]
        assert main._looks_like_dashboard_command_line(argv) is True

    def test_matches_console_script_form_with_interleaved_profile_flag(self):
        """The wrapper form, as psutil reports it on this box (DEFECT 2)."""
        argv = [
            r"C:\Users\diego\AppData\Local\Packages"
            r"\PythonSoftwareFoundation.Python.3.11_qbz5n2kfra8p0\LocalCache"
            r"\local-packages\Python311\Scripts\hermes.exe",
            "-p", "default", "dashboard", "--port", "9119",
            "--host", "127.0.0.1", "--open-profile", "main", "--no-open",
        ]
        assert main._looks_like_dashboard_command_line(argv) is True

    def test_matches_quoted_windows_path_string_form(self):
        """A raw (unsplit) Windows command line with a quoted exe path."""
        cmd = r'"C:\Program Files\hermes\Scripts\hermes.exe" dashboard --port 9119'
        assert main._looks_like_dashboard_command_line(cmd) is True

    def test_matches_equals_form_profile_selector(self):
        argv = ["python", "-m", "hermes_cli.main", "--profile=main", "dashboard"]
        assert main._looks_like_dashboard_command_line(argv) is True

    def test_matches_bare_dashboard(self):
        assert main._looks_like_dashboard_command_line("hermes dashboard") is True

    def test_matches_script_path_form(self):
        cmd = "python /home/x/hermes_cli/main.py dashboard"
        assert main._looks_like_dashboard_command_line(cmd) is True

    def test_matches_serve_headless_backend(self):
        """`hermes serve` is the same long-lived server under another name."""
        assert main._looks_like_dashboard_command_line("hermes serve") is True

    def test_does_not_match_session_bridge_serve(self):
        """REGRESSION: ``hermes-session-bridge.exe serve`` must NOT match.

        Two of these run on this box.  A tokenizer that accepts any token
        starting with "hermes" plus a bare ``serve`` token would reap the
        session bridge — data loss, not just a nuisance.
        """
        argv = [
            r"C:\Users\diego\.hermes\agent-src\.venv\Scripts\python.exe",
            r"C:\Users\diego\.hermes\agent-src\.venv\Scripts"
            r"\hermes-session-bridge.exe",
            "serve", "--config-home", r"C:\Users\diego\.hermes",
            "--state-db", r"C:\Users\diego\.hermes\state.db",
        ]
        assert main._looks_like_dashboard_command_line(argv) is False

    def test_does_not_match_gateway_subcommand(self):
        argv = ["python", "-m", "hermes_cli.main", "-p", "default", "gateway", "run"]
        assert main._looks_like_dashboard_command_line(argv) is False

    def test_does_not_match_chat_session_mentioning_dashboard(self):
        """A chat prompt containing the word 'dashboard' is not a server."""
        argv = [
            "python3", "-m", "hermes_cli.main", "chat",
            "-q", "rewrite my dashboard",
        ]
        assert main._looks_like_dashboard_command_line(argv) is False

    def test_does_not_match_test_runner_with_dashboard_path(self):
        argv = ["python", "run_tests_parallel.py", "--paths", "tests/dashboard/"]
        assert main._looks_like_dashboard_command_line(argv) is False

    def test_does_not_match_unrelated_dashboard_server(self):
        cmd = "node /opt/grafana/dashboard-server.js"
        assert main._looks_like_dashboard_command_line(cmd) is False

    def test_does_not_match_grep_for_the_pattern(self):
        """`grep hermes dashboard` has both tokens adjacent but isn't ours."""
        assert main._looks_like_dashboard_command_line("grep hermes dashboard") is False

    def test_does_not_match_its_own_status_invocation(self):
        """``hermes dashboard --status`` is a management command, not a
        server.  It must not list itself — and ``--stop`` must not try to
        kill itself, which would abort the stop mid-run."""
        argv = ["hermes", "dashboard", "--status"]
        assert main._looks_like_dashboard_command_line(argv) is False

    def test_does_not_match_its_own_stop_invocation(self):
        argv = [r"C:\Scripts\hermes.exe", "-p", "default", "dashboard", "--stop"]
        assert main._looks_like_dashboard_command_line(argv) is False

    def test_does_not_match_dashboard_as_a_flag_value(self):
        """``--open-profile dashboard`` is a flag VALUE, not the subcommand."""
        argv = ["hermes", "chat", "--open-profile", "dashboard"]
        assert main._looks_like_dashboard_command_line(argv) is False

    def test_empty_and_none_are_not_matches(self):
        assert main._looks_like_dashboard_command_line(None) is False
        assert main._looks_like_dashboard_command_line("") is False
        assert main._looks_like_dashboard_command_line([]) is False


# ── The scan ─────────────────────────────────────────────────────────

class TestScanDashboardProcesses:

    def test_finds_the_live_dashboard(self, fake_psutil):
        fake_psutil(
            _FakeProc(33940, [r"C:\Scripts\hermes.exe", "-p", "default",
                              "dashboard", "--port", "9119"]),
            _FakeProc(1852, [r"C:\python.exe", r"C:\scripts\mempalace.py"]),
        )
        pids = main._find_stale_dashboard_pids()
        assert list(pids) == [33940]
        assert pids.scan_ok is True

    def test_no_subprocess_is_spawned(self, fake_psutil):
        """DEFECT 1: no ``wmic``/``ps`` spawn at all — psutil is in-process."""
        fake_psutil(_FakeProc(33940, ["hermes", "dashboard"]))
        with patch("subprocess.run") as mock_run:
            assert list(main._find_stale_dashboard_pids()) == [33940]
        mock_run.assert_not_called()

    def test_self_pid_excluded(self, fake_psutil):
        fake_psutil(
            _FakeProc(os.getpid(), ["hermes", "dashboard"]),
            _FakeProc(12345, ["hermes", "dashboard"]),
        )
        pids = main._find_stale_dashboard_pids()
        assert os.getpid() not in pids
        assert 12345 in pids

    def test_ancestor_chain_excluded_not_just_self_pid(self, fake_psutil):
        """The console script runs as a parent/child pair on Windows —
        ``hermes.exe`` spawning ``python.exe ...\\hermes.exe`` — so the
        process running this scan is the CHILD.  Excluding only
        ``os.getpid()`` leaves our own wrapper parent in the results, and
        ``--stop`` would then kill the very process invoking it.  Same bug
        the gateway scan fixed in #13242.
        """
        import psutil

        parent_pid = psutil.Process().ppid()
        fake_psutil(
            _FakeProc(parent_pid, ["hermes.exe", "dashboard", "--port", "9119"]),
            _FakeProc(12345, ["hermes", "dashboard", "--port", "9119"]),
        )
        pids = main._find_stale_dashboard_pids()
        assert parent_pid not in pids
        assert 12345 in pids

    def test_exclude_pids_filters_and_keeps_scan_ok(self, fake_psutil):
        """exclude_pids protects the desktop-managed backend (#37532)."""
        fake_psutil(
            _FakeProc(11111, ["hermes", "dashboard", "--port", "9119"]),
            _FakeProc(22222, ["hermes", "dashboard", "--port", "9120"]),
        )
        pids = main._find_stale_dashboard_pids(exclude_pids={22222})
        assert list(pids) == [11111]
        # Filtering must not drop the scan-status metadata.
        assert pids.scan_ok is True

    def test_per_process_access_denied_is_skipped_not_fatal(self, fake_psutil):
        """One unreadable process must not abort the whole scan."""
        import psutil
        fake_psutil(
            _FakeProc(999, None, raises=psutil.AccessDenied(999)),
            _FakeProc(12345, ["hermes", "dashboard"]),
        )
        pids = main._find_stale_dashboard_pids()
        assert list(pids) == [12345]
        assert pids.scan_ok is True

    def test_scan_failure_is_not_reported_as_empty(self, fake_psutil):
        """DEFECT 1's core harm: a failed scan must not read as a clean box."""
        fake_psutil.failing(OSError("process table unavailable"))
        pids = main._find_stale_dashboard_pids()
        assert list(pids) == []
        assert pids.scan_ok is False
        assert pids.scan_error

    def test_missing_psutil_is_a_scan_failure(self, monkeypatch):
        real_import = __builtins__["__import__"] if isinstance(
            __builtins__, dict) else __builtins__.__import__

        def _fake_import(name, *a, **kw):
            if name == "psutil":
                raise ImportError("no psutil")
            return real_import(name, *a, **kw)

        monkeypatch.setitem(sys.modules, "psutil", None)
        with patch("builtins.__import__", side_effect=_fake_import):
            pids = main._find_stale_dashboard_pids()
        assert list(pids) == []
        assert pids.scan_ok is False

    def test_plain_list_from_a_patched_scan_defaults_to_ok(self):
        """Back-compat: callers must tolerate a plain list (test doubles)."""
        assert main._scan_ok([]) is True
        assert main._scan_ok([1, 2]) is True


# ── User-visible paths ───────────────────────────────────────────────

def _ns(**kw):
    defaults = dict(
        port=9119, host="127.0.0.1", no_open=False, insecure=False,
        stop=False, status=False,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def _failed_scan():
    return main._DashboardPids([], scan_ok=False, scan_error="wmic is gone")


class TestScanFailureIsUserVisible:

    def test_stop_reports_scan_failure_instead_of_a_false_clean(self, capsys):
        """``--stop`` must not print 'No hermes dashboard processes running'
        when it never managed to look."""
        with patch("hermes_cli.main._find_stale_dashboard_pids",
                   return_value=_failed_scan()), \
             pytest.raises(SystemExit) as exc:
            main.cmd_dashboard(_ns(stop=True))
        out = capsys.readouterr().out
        assert "No hermes dashboard processes running" not in out
        assert "could not scan" in out.lower()
        # Nothing was verified, so this is not a success.
        assert exc.value.code == 1

    def test_status_reports_scan_failure(self, capsys):
        with patch("hermes_cli.main._find_stale_dashboard_pids",
                   return_value=_failed_scan()), \
             pytest.raises(SystemExit):
            main.cmd_dashboard(_ns(status=True))
        out = capsys.readouterr().out
        assert "No hermes dashboard processes running" not in out
        assert "could not scan" in out.lower()

    def test_update_reaper_warns_instead_of_silently_skipping(self, capsys):
        """`hermes update`'s reaper exists to prevent a stale backend.  If
        the scan fails it must say so, not return as if the box were clean.
        """
        with patch("hermes_cli.main._find_stale_dashboard_pids",
                   return_value=_failed_scan()):
            main._kill_stale_dashboard_processes()
        out = capsys.readouterr().out
        assert "could not scan" in out.lower()

    def test_clean_box_stays_silent_in_the_reaper(self, capsys):
        """A genuinely empty result must remain a silent no-op."""
        with patch("hermes_cli.main._find_stale_dashboard_pids",
                   return_value=main._DashboardPids([], scan_ok=True)):
            main._kill_stale_dashboard_processes()
        assert capsys.readouterr().out == ""
