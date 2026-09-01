"""Tests for the launchd-aware dashboard restart on macOS.

Background: on macOS the dashboard can be supervised by launchd
(``ai.hermes.dashboard``, KeepAlive). ``hermes update`` used to
raw-kill its PID and then respawn a detached copy itself — two supervisors
racing for :9119/:9120, the loser exit-75 crash-looping every ~30s.
The fix routes the darwin path through ``launchctl kickstart -k`` and
never falls back to ``os.kill`` for supervised agents.
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli.main import (
    _kill_stale_dashboard_processes,
    _restart_launchd_dashboard_after_update,
    _restart_managed_dashboard_service,
)


@pytest.fixture
def _refresh_bindings():
    """Re-import live bindings (xdist pollution guard, mirrors this file's
    siblings)."""
    global _kill_stale_dashboard_processes
    global _restart_managed_dashboard_service
    import importlib

    live = sys.modules.get("hermes_cli.main")
    if live is None:
        live = importlib.import_module("hermes_cli.main")
    _kill_stale_dashboard_processes = live._kill_stale_dashboard_processes
    _restart_managed_dashboard_service = live._restart_managed_dashboard_service
    yield


def _plist_exists(monkeypatch, tmp_path, exists=True):
    plist_dir = tmp_path / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist = plist_dir / "ai.hermes.dashboard.plist"
    if exists:
        plist.write_text("<plist/>")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    return plist


class TestRestartLaunchdDashboardAfterUpdate:
    def test_kickstart_success_reports_pid(self, tmp_path, monkeypatch, capsys):
        _plist_exists(monkeypatch, tmp_path)
        printed_pid = {"n": 0}

        def fake_run(args, *a, **kw):
            if args[:2] == ["launchctl", "kickstart"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            if args[:2] == ["launchctl", "print"]:
                # first probe: not yet registered; second: supervised
                printed_pid["n"] += 1
                if printed_pid["n"] < 2:
                    return MagicMock(returncode=0, stdout="state = not running\n")
                return MagicMock(
                    returncode=0, stdout="state = running\n\tpid = 4242\n"
                )
            raise AssertionError(f"unexpected subprocess.run call: {args}")

        with patch("subprocess.run", side_effect=fake_run), \
             patch("time.sleep") as sleep:
            ok = _restart_launchd_dashboard_after_update("test")

        assert ok is True
        out = capsys.readouterr().out
        assert "launchd-supervised dashboard" in out
        assert "4242" in out
        # sleep calls in the supervision wait loop are mocked short
        assert sleep.called

    def test_kickstart_failure_is_loud_but_handled(self, tmp_path, monkeypatch, capsys):
        _plist_exists(monkeypatch, tmp_path)

        def fake_run(args, *a, **kw):
            if args[:2] == ["launchctl", "kickstart"]:
                return MagicMock(returncode=1, stdout="", stderr="bootstrap failed")
            raise AssertionError(f"unexpected subprocess.run call: {args}")

        with patch("subprocess.run", side_effect=fake_run):
            ok = _restart_launchd_dashboard_after_update("test")

        assert ok is True  # handled: caller must not raw-kill
        out = capsys.readouterr().out
        assert "launchctl kickstart failed" in out
        assert "launchctl kickstart -k" in out  # manual recovery hint

    def test_no_plist_returns_false(self, tmp_path, monkeypatch):
        _plist_exists(monkeypatch, tmp_path, exists=False)
        with patch("subprocess.run", side_effect=AssertionError("must not run")):
            assert _restart_launchd_dashboard_after_update("test") is False

    def test_supervision_timeout_is_loud(self, tmp_path, monkeypatch, capsys):
        _plist_exists(monkeypatch, tmp_path)

        def fake_run(args, *a, **kw):
            if args[:2] == ["launchctl", "kickstart"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            if args[:2] == ["launchctl", "print"]:
                return MagicMock(returncode=1, stdout="", stderr="not found")
            raise AssertionError(f"unexpected subprocess.run call: {args}")

        monotonic_values = iter([0.0, 100.0])
        monkeypatch.setattr("hermes_cli.main._time.monotonic",
                            lambda: next(monotonic_values))
        with patch("subprocess.run", side_effect=fake_run), \
             patch("time.sleep"):
            ok = _restart_launchd_dashboard_after_update("test")

        assert ok is True
        out = capsys.readouterr().out
        assert "not supervising" in out


class TestManagedDashboardDispatch:
    def test_darwin_routes_to_launchd_path(self, monkeypatch):
        calls = []
        monkeypatch.setattr(sys, "platform", "darwin")
        with patch("hermes_cli.main._restart_launchd_dashboard_after_update",
                   side_effect=lambda reason: calls.append(reason) or True):
            got = _restart_managed_dashboard_service("unit-test")
        assert got is True
        assert calls == ["unit-test"]

    def test_supervised_agent_is_never_raw_killed(self, tmp_path, monkeypatch,
                                                  capsys):
        """The race that started this: update finds a launchd-supervised
        dashboard PID; the correct outcome is a kickstart, not os.kill +
        detached respawn."""
        _plist_exists(monkeypatch, tmp_path)

        def fake_run(args, *a, **kw):
            if args[:2] == ["launchctl", "kickstart"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            if args[:2] == ["launchctl", "print"]:
                return MagicMock(returncode=0, stdout="state = running\n\tpid = 777\n")
            raise AssertionError(f"unexpected subprocess.run call: {args}")

        with patch("subprocess.run", side_effect=fake_run), \
             patch("hermes_cli.main._find_stale_dashboard_pids",
                   return_value=[777]) as find_pids, \
             patch("os.kill") as kill:
            _kill_stale_dashboard_processes(restart_managed=True)

        find_pids.assert_not_called()
        kill.assert_not_called()
        out = capsys.readouterr().out
        assert "777" in out
