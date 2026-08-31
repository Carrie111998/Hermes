"""An unprovable serve spawner is not proof of a manual serve (#99450 R2-3).

``collect_runtime_inventory`` classified a serve/dashboard ledger row as
Desktop-supervised only when its spawner was *provably alive*
(``spawner_is_dead(entry) is False``). ``None`` — no spawner recorded, or a
spawner whose ``(pid, create_time)`` cannot be probed — fell through to
``manual-serve``, whose stop is a plain PID kill and whose relaunch is a
respawn. Under a live Desktop that kill is answered by a fresh backend on
pre-update code, inside the mutation window, under a PID the old-PID exit
check never looks at.

Only a *provably dead* spawner licenses the kill. Everything else is
Desktop-supervised as far as the updater is concerned, which means the
quiesce refuses and asks for manual intervention.

These drive the real collector over a real ledger file describing a real
live process, so the classification and the stop are exercised end to end.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

import pytest

import hermes_cli.process_identity as pi
import hermes_cli.update_cmd as update_cmd
import hermes_cli.update_inventory as ui
from hermes_cli import update_quiesce


@pytest.fixture()
def live_backend():
    """A real child process standing in for a `hermes serve` backend."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


@pytest.fixture()
def inventory(tmp_path, monkeypatch):
    """Real ledger file + every other probe answering with nothing."""
    root = tmp_path / "hermes-root"
    root.mkdir()
    monkeypatch.setattr("hermes_constants.get_default_hermes_root", lambda: root)

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("hermes_cli.profiles._get_default_hermes_home", lambda: home)
    monkeypatch.setattr(
        "hermes_cli.profiles._get_profiles_root", lambda: home / "profiles"
    )
    monkeypatch.setattr(
        "hermes_cli.profiles._PROFILE_ID_RE",
        re.compile(r"^[a-z0-9][a-z0-9_-]*$"),
        raising=False,
    )
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: False)
    monkeypatch.setattr(
        "hermes_cli.gateway._get_service_pids", lambda all_profiles=False: set()
    )
    monkeypatch.setattr(
        "hermes_cli.gateway.find_profile_gateway_processes",
        lambda exclude_pids=None: [],
    )
    monkeypatch.setattr("hermes_cli.gateway.find_windows_gateway_services", lambda: [])
    monkeypatch.setattr(
        "hermes_cli.build_info.get_code_identity",
        lambda refresh=False: {"sha": "a" * 40, "version": "1.0"},
    )
    monkeypatch.setattr("hermes_cli.config.detect_install_method", lambda *a, **k: "git")
    monkeypatch.setattr("hermes_cli.config.get_managed_system", lambda: None)
    # The test runner itself may live inside a systemd unit, whose cgroup
    # the identity pass would otherwise attribute to our stand-in backend.
    # Present the host as one with no cgroup/launchd/SCM to read, so the
    # classification under test is the collector's, not the runner's.
    monkeypatch.setattr(ui, "_default_pid_cgroup", lambda pid: None)
    monkeypatch.setattr(ui, "_live_launchd_labels", lambda: {})
    monkeypatch.setattr(ui, "_windows_service_names_by_pid", lambda: {})
    return root / pi.LEDGER_FILENAME


def _write_ledger(path, pid, *, spawner_pid=None, spawner_create=None):
    import psutil

    entry = {
        "pid": pid,
        "create_time": float(psutil.Process(pid).create_time()),
        "purpose": "serve",
        "install": pi.install_id(),
        "spawner_pid": spawner_pid,
        "spawner_create": spawner_create,
        "registered_at": time.time(),
        "argv": "hermes serve --host 127.0.0.1 --port 9119",
        "host": "127.0.0.1",
        "port": 9119,
        "profile": "default",
    }
    path.write_text(json.dumps([entry]), encoding="utf-8")
    return entry


def _serve_row(plan):
    rows = [r for r in plan.runtimes if r.kind == "serve"]
    assert rows, plan.runtimes
    return rows[0]


class TestUnprovableSpawnerIsNotAManualServe:
    def test_missing_spawner_is_treated_as_supervised(
        self, inventory, live_backend
    ):
        """No spawner recorded at all — the commonest legacy Desktop shape."""
        _write_ledger(inventory, live_backend.pid)
        row = _serve_row(ui.collect_runtime_inventory())
        assert row.supervisor == "desktop"
        assert row.restart_via == "desktop"

    def test_unprovable_spawner_is_treated_as_supervised(
        self, inventory, live_backend, monkeypatch
    ):
        """A spawner IS recorded, but the liveness probe cannot answer."""
        _write_ledger(
            inventory, live_backend.pid, spawner_pid=999_000, spawner_create=1.0
        )
        real = pi._pid_alive_matches

        def _unprovable(pid, create_time):
            if int(pid) == 999_000:
                return None  # e.g. psutil.AccessDenied on another user's app
            return real(pid, create_time)

        monkeypatch.setattr(pi, "_pid_alive_matches", _unprovable)
        row = _serve_row(ui.collect_runtime_inventory())
        assert row.supervisor == "desktop"

    def test_live_spawner_is_still_desktop(self, inventory, live_backend):
        _write_ledger(
            inventory,
            live_backend.pid,
            spawner_pid=os.getpid(),
            spawner_create=float(__import__("psutil").Process(os.getpid()).create_time()),
        )
        assert _serve_row(ui.collect_runtime_inventory()).supervisor == "desktop"

    def test_provably_dead_spawner_is_the_only_manual_serve(
        self, inventory, live_backend
    ):
        """The one case a PID stop is safe: nothing is left to respawn it."""
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead_create = float(__import__("psutil").Process(dead.pid).create_time())
        dead.wait(timeout=30)
        _write_ledger(
            inventory,
            live_backend.pid,
            spawner_pid=dead.pid,
            spawner_create=dead_create,
        )
        row = _serve_row(ui.collect_runtime_inventory())
        assert row.supervisor == "manual-serve"
        assert row.restart_via == "respawn-argv"


class TestUnprovableSpawnerNeverGetsKilled:
    def test_the_stop_refuses_instead_of_killing_the_pid(
        self, inventory, live_backend
    ):
        """End to end: inventory → quiesce stop. The backend stays alive."""
        _write_ledger(inventory, live_backend.pid)
        plan = ui.collect_runtime_inventory()
        row = _serve_row(plan)

        with pytest.raises(update_quiesce.QuiesceAbort) as excinfo:
            update_cmd._stop_runtime_for_quiesce(row)

        assert "Desktop" in str(excinfo.value)
        assert live_backend.poll() is None, "the backend must not be signalled"

    def test_the_whole_quiesce_aborts_with_the_fleet_untouched(
        self, inventory, live_backend, monkeypatch
    ):
        _write_ledger(inventory, live_backend.pid)
        plan = ui.collect_runtime_inventory()

        with pytest.raises(update_quiesce.QuiesceAbort) as excinfo:
            update_quiesce.run_pre_mutation_quiesce(
                plan,
                stop_runtime=update_cmd._stop_runtime_for_quiesce,
                pid_alive=update_cmd._runtime_pid_alive,
                assess_isolation=lambda _p: update_quiesce.IsolationResult(
                    isolated=True, reason="test"
                ),
                exit_timeout=1.0,
                poll_interval=0.05,
                persist_state=False,
            )

        assert "Desktop" in str(excinfo.value)
        assert live_backend.poll() is None
        with pytest.raises(update_quiesce.QuiesceAbort):
            update_quiesce.assert_mutation_authorized("git")
