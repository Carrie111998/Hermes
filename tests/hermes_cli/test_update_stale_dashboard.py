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
import sys
from unittest.mock import patch, MagicMock

import pytest

from hermes_cli.main import (
    _find_stale_dashboard_pids,
    _kill_stale_dashboard_processes,
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
    global _find_stale_dashboard_pids
    global _kill_stale_dashboard_processes
    global _warn_stale_dashboard_processes

    live = sys.modules.get("hermes_cli.main")
    if live is None:
        live = importlib.import_module("hermes_cli.main")

    _find_stale_dashboard_pids = live._find_stale_dashboard_pids
    _kill_stale_dashboard_processes = live._kill_stale_dashboard_processes
    _warn_stale_dashboard_processes = live._warn_stale_dashboard_processes
    yield


class _FakeProc:
    """Stand-in for a ``psutil.Process`` as yielded by ``process_iter``."""

    def __init__(self, pid, cmdline):
        self.info = {"pid": pid, "cmdline": cmdline}


def _fake_process_table(*procs):
    """Patch ``psutil.process_iter`` to yield *procs*.

    The scan reads the process table through psutil on every platform
    (there is no ``ps``/``wmic`` spawn any more), so stubbing psutil is
    how these tests drive it.  ``cmdline`` is an argv list, matching what
    psutil really returns.
    """
    import psutil

    return patch.object(
        psutil, "process_iter", lambda attrs=None, ad_value=None: iter(procs)
    )


class TestFindStaleDashboardPids:
    """Unit tests for the psutil-based detection step.

    Deeper coverage of the command-line matcher and of the scan-failure
    signalling lives in ``test_dashboard_process_scan.py``.
    """

    def test_no_matches_returns_empty(self):
        with _fake_process_table(
            _FakeProc(111, ["/usr/bin/python3", "-m", "some.other.module"]),
            _FakeProc(222, ["/usr/bin/bash"]),
        ):
            assert _find_stale_dashboard_pids() == []

    def test_matches_running_dashboard(self):
        with _fake_process_table(
            _FakeProc(12345, ["python3", "-m", "hermes_cli.main",
                              "dashboard", "--port", "9119"]),
        ):
            assert _find_stale_dashboard_pids() == [12345]

    def test_multiple_matches(self):
        with _fake_process_table(
            _FakeProc(12345, ["python3", "-m", "hermes_cli.main",
                              "dashboard", "--port", "9119"]),
            _FakeProc(12346, ["hermes", "dashboard", "--port", "9120", "--no-open"]),
            _FakeProc(12347, ["python", "/home/x/hermes_cli/main.py", "dashboard"]),
        ):
            assert sorted(_find_stale_dashboard_pids()) == [12345, 12346, 12347]

    def test_matches_dashboard_behind_a_profile_selector(self):
        """The live :9119 server's real argv — ``-p default`` sits between
        the entrypoint and the subcommand, which the old adjacent-words
        substring patterns could not match at all."""
        with _fake_process_table(
            _FakeProc(33940, ["python.exe", "-m", "hermes_cli.main",
                              "-p", "default", "dashboard", "--port", "9119",
                              "--host", "127.0.0.1", "--no-open"]),
        ):
            assert _find_stale_dashboard_pids() == [33940]

    def test_self_pid_excluded(self):
        with _fake_process_table(
            _FakeProc(os.getpid(), ["python3", "-m", "hermes_cli.main", "dashboard"]),
            _FakeProc(12345, ["hermes", "dashboard", "--port", "9119"]),
        ):
            pids = _find_stale_dashboard_pids()
        assert os.getpid() not in pids
        assert 12345 in pids

    def test_scan_failure_is_distinguishable_from_empty(self):
        """A failed scan must NOT read as "no dashboards running".

        This is the defect that made ``--stop`` print a false clean on
        Windows 11, where the old ``wmic`` transport no longer exists.
        """
        import psutil

        def _boom(attrs=None, ad_value=None):
            raise OSError("process table unavailable")

        with patch.object(psutil, "process_iter", _boom):
            pids = _find_stale_dashboard_pids()
        assert list(pids) == []
        assert pids.scan_ok is False

    def test_unrelated_process_containing_word_dashboard_not_matched(self):
        """Guards against greedy pgrep-style matching catching chat sessions
        or unrelated processes whose cmdline happens to contain 'dashboard'.
        """
        with _fake_process_table(
            _FakeProc(12345, ["python3", "-m", "hermes_cli.main",
                              "dashboard", "--port", "9119"]),
            _FakeProc(22222, ["python3", "-m", "hermes_cli.main", "chat",
                              "-q", "rewrite my dashboard"]),
            _FakeProc(33333, ["node", "/opt/grafana/dashboard-server.js"]),
        ):
            pids = _find_stale_dashboard_pids()
        assert pids == [12345]

    def test_grep_for_the_pattern_is_ignored(self):
        with _fake_process_table(
            _FakeProc(99999, ["grep", "hermes", "dashboard"]),
            _FakeProc(12345, ["hermes", "dashboard", "--port", "9119"]),
        ):
            pids = _find_stale_dashboard_pids()
        assert 99999 not in pids
        assert 12345 in pids

    def test_processes_without_a_readable_cmdline_are_skipped(self):
        """Kernel threads and protected processes report an empty/absent
        cmdline; they must be skipped rather than abort the scan."""
        with _fake_process_table(
            _FakeProc(4, []),
            _FakeProc(8, None),
            _FakeProc(None, ["hermes", "dashboard"]),
            _FakeProc(12345, ["hermes", "dashboard", "--port", "9119"]),
        ):
            pids = _find_stale_dashboard_pids()
        assert pids == [12345]

    def test_exclude_pids_filters_specified_pids(self):
        """exclude_pids removes specific PIDs from the result — used by
        the Desktop Electron app to protect its own backend child.  (#37532)
        """
        with _fake_process_table(
            _FakeProc(11111, ["hermes", "dashboard", "--port", "9119"]),
            _FakeProc(22222, ["hermes", "dashboard", "--port", "9120"]),
            _FakeProc(33333, ["hermes", "dashboard", "--port", "9121"]),
        ):
            # Exclude the desktop-managed backend PID
            pids = _find_stale_dashboard_pids(exclude_pids={22222})
        assert 11111 in pids
        assert 22222 not in pids
        assert 33333 in pids

    def test_exclude_pids_none_is_noop(self):
        """Passing exclude_pids=None (the default) changes nothing."""
        with _fake_process_table(
            _FakeProc(12345, ["hermes", "dashboard", "--port", "9119"]),
        ):
            pids = _find_stale_dashboard_pids(exclude_pids=None)
        assert pids == [12345]

    def test_exclude_all_pids_returns_empty(self):
        """If all matched PIDs are excluded, the result is empty."""
        with _fake_process_table(
            _FakeProc(12345, ["hermes", "dashboard", "--port", "9119"]),
        ):
            pids = _find_stale_dashboard_pids(exclude_pids={12345})
        assert pids == []
        # Still a successful scan — the box was searched, matches were
        # found, and the caller asked for them to be filtered out.
        assert pids.scan_ok is True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX kill semantics")
class TestKillStaleDashboardPosix:
    """Kill path on Linux / macOS: SIGTERM then SIGKILL any survivors."""

    def test_no_stale_processes_is_a_noop(self, capsys):
        with patch("hermes_cli.main._find_stale_dashboard_pids", return_value=[]):
            _kill_stale_dashboard_processes()
        assert capsys.readouterr().out == ""

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
            _kill_stale_dashboard_processes()

        # Both got SIGTERM.
        sigterms = [pid for pid, sig in killed_signals if sig == _signal.SIGTERM]
        assert sorted(sigterms) == [12345, 12346]
        # No SIGKILL was needed.
        assert not any(sig == _signal.SIGKILL for _, sig in killed_signals)

        out = capsys.readouterr().out
        assert "Stopping 2 dashboard" in out
        assert "✓ stopped PID 12345" in out
        assert "✓ stopped PID 12346" in out
        assert "Restart the dashboard" in out

    def test_sigkill_fallback_for_survivors(self, capsys):
        """If a process survives SIGTERM + the grace window, SIGKILL is sent."""
        import signal as _signal

        sent: list[tuple[int, int]] = []

        def fake_kill(pid, sig):
            sent.append((pid, sig))
            # Simulate stubborn process: probe (sig 0) always succeeds,
            # SIGTERM does nothing, SIGKILL is where it "dies".
            if sig in {_signal.SIGTERM, 0, _signal.SIGKILL}:
                return
            # Any other signal — also fine.

        with patch("hermes_cli.main._find_stale_dashboard_pids",
                   return_value=[99999]), \
             patch("os.kill", side_effect=fake_kill), \
             patch("time.sleep"), \
             patch("time.monotonic", side_effect=[0.0] + [10.0] * 20):
            # monotonic jumps past the 3s deadline on the second read so the
            # grace loop exits immediately after one iteration.
            _kill_stale_dashboard_processes()

        signals_sent = [sig for _, sig in sent]
        assert _signal.SIGTERM in signals_sent
        assert _signal.SIGKILL in signals_sent

        out = capsys.readouterr().out
        assert "✓ stopped PID 99999" in out

    def test_permission_error_is_reported_not_raised(self, capsys):
        """os.kill raising PermissionError (e.g. another user's process)
        must not abort hermes update — it's reported as a failure and we
        move on."""
        def fake_kill(pid, sig):
            raise PermissionError("Operation not permitted")

        with patch("hermes_cli.main._find_stale_dashboard_pids",
                   return_value=[12345]), \
             patch("os.kill", side_effect=fake_kill), \
             patch("time.sleep"):
            _kill_stale_dashboard_processes()  # must not raise

        out = capsys.readouterr().out
        assert "✗ failed to stop PID 12345" in out
        assert "Operation not permitted" in out

    def test_process_already_gone_counts_as_stopped(self, capsys):
        """ProcessLookupError on the initial SIGTERM means the process
        already exited between detection and the kill — treat as success."""
        def fake_kill(pid, sig):
            raise ProcessLookupError

        with patch("hermes_cli.main._find_stale_dashboard_pids",
                   return_value=[12345]), \
             patch("os.kill", side_effect=fake_kill), \
             patch("time.sleep"):
            _kill_stale_dashboard_processes()

        out = capsys.readouterr().out
        assert "✓ stopped PID 12345" in out
        assert "failed to stop" not in out


class TestKillStaleDashboardWindows:
    """Kill path on Windows: taskkill /F."""

    def test_taskkill_invoked_for_each_pid(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "platform", "win32")

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

    def test_already_gone_process_counts_as_stopped(self, monkeypatch, capsys):
        """A PID that died before its taskkill is a success, not a failure.

        On Windows each dashboard is a parent/child pair — ``hermes.exe``
        spawning ``python.exe ...\\hermes.exe`` — and the scan legitimately
        matches both.  Killing the parent takes the child with it, so the
        child's taskkill then reports 'not found'.  That is the outcome we
        wanted; printing '✗ failed to stop' for it would be alarming noise
        on a perfectly successful stop.
        """
        monkeypatch.setattr(sys, "platform", "win32")

        def fake_run(args, *a, **kw):
            return MagicMock(
                returncode=128, stdout="",
                stderr='ERROR: The process "12346" not found.',
            )

        with patch("hermes_cli.main._find_stale_dashboard_pids",
                   return_value=[12346]), \
             patch("subprocess.run", side_effect=fake_run):
            _kill_stale_dashboard_processes()

        out = capsys.readouterr().out
        assert "✓ stopped PID 12346" in out
        assert "✗ failed" not in out

    def test_taskkill_failure_is_reported(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "platform", "win32")

        def fake_run(args, *a, **kw):
            return MagicMock(returncode=128, stdout="",
                             stderr="ERROR: Access is denied.")

        with patch("hermes_cli.main._find_stale_dashboard_pids",
                   return_value=[12345]), \
             patch("subprocess.run", side_effect=fake_run):
            _kill_stale_dashboard_processes()  # must not raise

        out = capsys.readouterr().out
        assert "✗ failed to stop PID 12345" in out
        assert "Access is denied" in out


class TestBackCompatAlias:
    """``_warn_stale_dashboard_processes`` is kept as an alias for the
    new kill function so old imports don't break."""

    def test_alias_is_the_kill_function(self):
        assert _warn_stale_dashboard_processes is _kill_stale_dashboard_processes


class TestWindowsScanTransport:
    """The scan must not shell out on Windows.

    History: the Windows branch used to run ``wmic``, which brought two
    problems this class now guards the absence of.

    - #17049: wmic emits text in the system code page (cp936 on zh-CN), so
      the call needed ``encoding='utf-8', errors='ignore'`` to stop a
      reader-thread UnicodeDecodeError from leaving ``stdout=None`` and
      turning the later ``.split()`` into an AttributeError.
    - 2026-08-12: Microsoft removed wmic from Windows 11 outright, so the
      spawn raised FileNotFoundError and the scan silently returned [].

    Reading the process table in-process through psutil retires both: there
    is no decoding step and no executable to be missing.  Asserting "no
    subprocess at all" is the strictly stronger contract, and it also
    subsumes the old CREATE_NO_WINDOW requirement (a spawn that never
    happens cannot flash a console window from ``pythonw.exe``).
    """

    def test_scan_spawns_no_subprocess_on_windows(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        with _fake_process_table(
            _FakeProc(12345, ["python", "-m", "hermes_cli.main", "dashboard"]),
        ), patch("subprocess.run") as mock_run:
            assert _find_stale_dashboard_pids() == [12345]
        mock_run.assert_not_called()

    def test_undecodable_cmdline_bytes_do_not_crash_the_scan(self, monkeypatch):
        """A cmdline carrying surrogates (what a non-UTF-8 locale yields)
        must be matched-or-skipped, never raise — `hermes update` used to
        abort here (#17049)."""
        monkeypatch.setattr(sys, "platform", "win32")
        with _fake_process_table(
            _FakeProc(999, ["C:\\\udcff\udcfe\\python.exe", "-m", "weird.module"]),
            _FakeProc(12345, ["hermes", "dashboard"]),
        ):
            assert _find_stale_dashboard_pids() == [12345]
