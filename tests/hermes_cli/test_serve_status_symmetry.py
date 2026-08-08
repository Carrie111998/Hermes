"""``--status`` must report every process ``--stop`` would reap.

``_find_stale_dashboard_pids`` (the --stop path) returns every pid
``_scan_dashboard_processes`` finds, with no mode filter, so it reaps both
``hermes dashboard`` and ``hermes serve``. ``_report_dashboard_status``
dropped anything whose cmdline did not parse to ``mode == "dashboard"``, so a
Desktop-spawned detached ``hermes serve`` backend was invisible to the only
CLI that can clean it up, and --status contradicted its own help text.
"""

from __future__ import annotations

import argparse
from unittest.mock import patch

import pytest

_SERVE = "hermes serve --isolated --host 127.0.0.1 --port 8123 --ssh-owner-nonce abc"
_DASH = "hermes dashboard --port 9119"


def _ns(**kw):
    defaults = dict(port=9119, host="127.0.0.1", no_open=False, insecure=False,
                    stop=False, status=False)
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def _status(processes, capsys):
    # Imported per call: other tests in this package importlib.reload
    # hermes_cli.main, so a module-level binding would go stale and the
    # patches below would target a different module object than the callee.
    from hermes_cli.main import cmd_dashboard

    with patch("hermes_cli.main._scan_dashboard_processes", return_value=processes), \
         patch("gateway.status._pid_exists", return_value=True), \
         patch("hermes_cli.main._dashboard_listening", return_value=True), \
         pytest.raises(SystemExit) as exc:
        cmd_dashboard(_ns(status=True))
    assert exc.value.code == 0
    return capsys.readouterr().out


def test_serve_mode_backend_is_listed(capsys):
    """The bug: a detached serve backend was filtered out of --status."""
    out = _status([(4242, _SERVE)], capsys)

    assert "PID 4242" in out, "a serve-mode backend that --stop would kill was not listed"
    assert "[serve]" in out


def test_both_modes_are_listed_together(capsys):
    out = _status([(1, _DASH), (2, _SERVE)], capsys)

    assert "2 hermes web server process(es) running" in out
    assert "PID 1 [dashboard]" in out
    assert "PID 2 [serve]" in out


def test_dashboard_only_still_reported(capsys):
    """Guard: the pre-existing dashboard case keeps working."""
    out = _status([(1, _DASH)], capsys)

    assert "PID 1 [dashboard]" in out


def test_unparseable_cmdlines_are_still_skipped(capsys):
    """Guard: widening the mode filter must not widen what counts as a match."""
    out = _status([(9, "/usr/bin/some-other-daemon --port 9119")], capsys)

    assert "No hermes web server processes running" in out


def test_dead_or_silent_processes_are_still_skipped(capsys):
    """Guard: liveness and listening checks are unchanged."""
    from hermes_cli.main import cmd_dashboard

    with patch("hermes_cli.main._scan_dashboard_processes", return_value=[(7, _SERVE)]), \
         patch("gateway.status._pid_exists", return_value=True), \
         patch("hermes_cli.main._dashboard_listening", return_value=False), \
         pytest.raises(SystemExit):
        cmd_dashboard(_ns(status=True))

    assert "No hermes web server processes running" in capsys.readouterr().out
