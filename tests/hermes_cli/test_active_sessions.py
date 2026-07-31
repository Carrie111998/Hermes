import logging
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hermes_cli import active_sessions


def test_pid_liveness_probe_failure_is_treated_as_potentially_live(monkeypatch):
    import gateway.status

    def fail_probe(_pid):
        raise OSError("process table unavailable")

    monkeypatch.setattr(gateway.status, "_pid_exists", fail_probe)

    assert active_sessions._pid_alive(12345, 100.0)


def test_process_start_probe_non_finite_is_treated_as_potentially_live(monkeypatch):
    import gateway.status

    monkeypatch.setattr(gateway.status, "_pid_exists", lambda _pid: True)
    monkeypatch.setattr(active_sessions, "_process_start_time", lambda _pid: float("nan"))

    assert active_sessions._pid_alive(12345, 100.0)


def test_resolve_max_concurrent_sessions_values(caplog):
    assert active_sessions.resolve_max_concurrent_sessions({}) is None
    assert active_sessions.resolve_max_concurrent_sessions({"max_concurrent_sessions": None}) is None
    assert active_sessions.resolve_max_concurrent_sessions({"max_concurrent_sessions": 0}) is None
    assert active_sessions.resolve_max_concurrent_sessions({"max_concurrent_sessions": -1}) is None
    assert active_sessions.resolve_max_concurrent_sessions({"max_concurrent_sessions": "3"}) == 3
    assert (
        active_sessions.resolve_max_concurrent_sessions(
            {"gateway": {"max_concurrent_sessions": 4}}
        )
        == 4
    )
    assert (
        active_sessions.resolve_max_concurrent_sessions(
            {"max_concurrent_sessions": 2, "gateway": {"max_concurrent_sessions": 4}}
        )
        == 2
    )

    caplog.set_level(logging.WARNING)
    assert active_sessions.resolve_max_concurrent_sessions({"max_concurrent_sessions": "many"}) is None
    assert any(
        "Ignoring invalid max_concurrent_sessions='many'" in record.message
        for record in caplog.records
    )












def test_disabled_cap_still_registers_owner_identity_for_lifecycle_recovery(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    lease, message = active_sessions.try_acquire_active_session(
        session_id="owned-session",
        surface="cli",
        config={"max_concurrent_sessions": 0},
    )

    assert message is None
    assert lease is not None
    entries = active_sessions.active_session_registry_snapshot()
    assert len(entries) == 1
    assert entries[0]["session_id"] == "owned-session"
    assert entries[0]["pid"] == os.getpid()
    assert entries[0]["process_start_time"] is not None

    lease.release()
    assert active_sessions.active_session_registry_snapshot() == []


def test_cross_process_acquire_claims_only_one_last_slot(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo_root = Path(__file__).resolve().parents[2]
    ready_dir = tmp_path / "ready"
    ready_dir.mkdir()
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    go_file = tmp_path / "go"
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["PYTHONPATH"] = str(repo_root)
    script = (
        "import os, time\n"
        "from pathlib import Path\n"
        "from hermes_cli.active_sessions import try_acquire_active_session\n"
        "idx = os.environ['WORKER_INDEX']\n"
        "worker_count = int(os.environ['WORKER_COUNT'])\n"
        "delayed_worker = os.environ.get('DELAYED_WORKER_INDEX')\n"
        "ready_dir = Path(os.environ['READY_DIR'])\n"
        "results_dir = Path(os.environ['RESULTS_DIR'])\n"
        "go_file = Path(os.environ['GO_FILE'])\n"
        "(ready_dir / idx).write_text('ready', encoding='utf-8')\n"
        "deadline = time.time() + 10\n"
        "while not go_file.exists():\n"
        "    if time.time() > deadline:\n"
        "        raise RuntimeError('timed out waiting for go file')\n"
        "    time.sleep(0.01)\n"
        "if idx == delayed_worker:\n"
        "    time.sleep(2.5)\n"
        "lease, message = try_acquire_active_session(\n"
        "    session_id=f'process-{idx}',\n"
        "    surface='cli',\n"
        "    config={'max_concurrent_sessions': 1},\n"
        ")\n"
        "if lease is None:\n"
        "    (results_dir / idx).write_text('BLOCK', encoding='utf-8')\n"
        "    print('BLOCK', flush=True)\n"
        "else:\n"
        "    (results_dir / idx).write_text('OK', encoding='utf-8')\n"
        "    print('OK', flush=True)\n"
        "    deadline = time.time() + 10\n"
        "    while len(list(results_dir.iterdir())) < worker_count:\n"
        "        if time.time() > deadline:\n"
        "            raise RuntimeError('timed out waiting for all workers to attempt acquire')\n"
        "        time.sleep(0.01)\n"
        "    lease.release()\n"
    )
    workers: list[subprocess.Popen[str]] = []
    try:
        for index in range(6):
            worker_env = env.copy()
            worker_env["WORKER_INDEX"] = str(index)
            worker_env["WORKER_COUNT"] = "6"
            worker_env["DELAYED_WORKER_INDEX"] = "5"
            worker_env["READY_DIR"] = str(ready_dir)
            worker_env["RESULTS_DIR"] = str(results_dir)
            worker_env["GO_FILE"] = str(go_file)
            workers.append(
                subprocess.Popen(
                    [sys.executable, "-c", script],
                    env=worker_env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            )

        deadline = time.time() + 10
        while len(list(ready_dir.iterdir())) < len(workers):
            if time.time() > deadline:
                raise AssertionError("workers did not become ready")
            time.sleep(0.01)
        go_file.write_text("go", encoding="utf-8")

        outputs = []
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=10)
            assert worker.returncode == 0, stderr
            outputs.append(stdout.strip())
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                worker.communicate()

    assert outputs.count("OK") == 1
    assert outputs.count("BLOCK") == len(workers) - 1
    assert active_sessions.active_session_registry_snapshot() == []




def test_release_orphaned_leases_reclaims_only_unowned_own_pid_entries(tmp_path, monkeypatch):
    """A long-lived server must reclaim leases whose session skipped teardown.

    ``_prune_dead`` only fires when the owning pid dies, so a ``hermes
    dashboard`` running for days holds a leaked lease until restart. The
    process reconciles against the leases it still owns instead.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    cfg = {"max_concurrent_sessions": 5}
    kept, orphan = (
        active_sessions.try_acquire_active_session(
            session_id=sid, surface="desktop", config=cfg
        )[0]
        for sid in ("kept", "orphaned")
    )
    # Another live process's lease is not ours to reclaim.
    active_sessions._write_entries(
        active_sessions._state_path(),
        active_sessions._read_entries(active_sessions._state_path())
        + [
            {
                "lease_id": "elsewhere",
                "session_id": "other",
                "surface": "cli",
                "pid": os.getpid(),
                "process_start_time": active_sessions._process_start_time(os.getpid()),
            }
        ],
    )

    assert active_sessions.release_orphaned_leases({kept.lease_id, "elsewhere"}) == 1
    assert sorted(
        entry["session_id"]
        for entry in active_sessions.active_session_registry_snapshot()
    ) == ["kept", "other"]
    assert orphan is not None


def test_recovery_uses_locked_live_owner_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    lease, error = active_sessions.try_acquire_active_session(
        config={}, session_id="live-session", surface="cli"
    )
    assert error is None
    assert lease is not None
    captured = {}

    class FakeSessionDB:
        def get_lifecycle_recovery_epoch(self):
            captured["read_epoch"] = True
            return None

        def get_or_create_lifecycle_recovery_epoch(self, *, now=None):
            raise AssertionError("dry run must not create recovery epoch state")

        def recover_abandoned_sessions(self, **kwargs):
            captured.update(kwargs)
            return {"candidate_ids": [], "recovered_ids": [], "excluded": {}}

    try:
        result = active_sessions.recover_abandoned_session_rows(
            FakeSessionDB(), apply=False, now=456.0
        )
    finally:
        lease.release()

    assert result["recovered_ids"] == []
    assert captured["read_epoch"] is True
    assert captured["eligible_started_after"] == 456.0
    assert captured["active_session_ids"] == {"live-session"}
    assert captured["apply"] is False


def test_recovery_fails_closed_on_corrupt_owner_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    registry = active_sessions._state_path()
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text("{not-json", encoding="utf-8")

    class FakeSessionDB:
        def get_or_create_lifecycle_recovery_epoch(self, *, now=None):
            raise AssertionError("database must not be touched")

        def recover_abandoned_sessions(self, **kwargs):
            raise AssertionError("database must not be touched")

    try:
        active_sessions.recover_abandoned_session_rows(
            FakeSessionDB(), apply=True, now=456.0
        )
    except RuntimeError as exc:
        assert "active session registry" in str(exc)
    else:
        raise AssertionError("corrupt owner evidence must block recovery")

    assert registry.read_text(encoding="utf-8") == "{not-json"


def test_recovery_fails_closed_on_ambiguous_owner_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    state_dir = tmp_path / ".hermes" / "runtime"
    state_dir.mkdir(parents=True)
    registry = state_dir / "active_sessions.json"
    registry.write_text('{"entries": [{}]}', encoding="utf-8")

    class FakeDB:
        def recover_abandoned_sessions(self, **kwargs):
            raise AssertionError("ambiguous owner evidence must block classification")

    with pytest.raises(RuntimeError, match="invalid active session registry"):
        active_sessions.recover_abandoned_session_rows(
            FakeDB(), apply=False, now=100.0
        )

    assert registry.read_text(encoding="utf-8") == '{"entries": [{}]}'


@pytest.mark.parametrize("process_start_time", ["nan", "inf", "-inf"])
def test_registry_rejects_non_finite_process_start_time(
    tmp_path, process_start_time
):
    registry = tmp_path / "active_sessions.json"
    registry.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "lease_id": "lease",
                        "session_id": "session",
                        "pid": 123,
                        "process_start_time": process_start_time,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="invalid active session registry"):
        active_sessions._read_entries(registry, strict=True)


def test_startup_recovery_respects_claimed_interval(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    class FakeSessionDB:
        def claim_lifecycle_recovery_attempt(self, *, now=None, interval_seconds=None):
            assert now == 456.0
            assert interval_seconds == 3600.0
            return False

        def get_or_create_lifecycle_recovery_epoch(self, *, now=None):
            raise AssertionError("suppressed recovery must not continue")

        def recover_abandoned_sessions(self, **kwargs):
            raise AssertionError("suppressed recovery must not continue")

    result = active_sessions.recover_abandoned_session_rows(
        FakeSessionDB(),
        apply=True,
        now=456.0,
        respect_interval_seconds=3600.0,
    )

    assert result == {
        "candidate_ids": [],
        "recovered_ids": [],
        "excluded": {},
        "skipped": "interval",
    }


def test_acquire_does_not_overwrite_corrupt_owner_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    registry = active_sessions._state_path()
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text("{not-json", encoding="utf-8")

    with pytest.raises(RuntimeError, match="active session registry"):
        active_sessions.try_acquire_active_session(
            session_id="new-session", surface="cli", config={}
        )
    assert registry.read_text(encoding="utf-8") == "{not-json"


def test_acquire_requires_pid_reuse_resistant_owner_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(active_sessions, "_process_start_time", lambda _pid: None)

    with pytest.raises(RuntimeError, match="process start time missing"):
        active_sessions.try_acquire_active_session(
            session_id="unproven-owner", surface="cli", config={}
        )

    assert not (tmp_path / ".hermes" / "runtime" / "active_sessions.json").exists()
