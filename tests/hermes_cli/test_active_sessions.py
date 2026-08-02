import logging
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from hermes_cli import active_sessions


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
        + [{"lease_id": "elsewhere", "session_id": "other", "surface": "cli", "pid": os.getpid() }],
    )

    assert active_sessions.release_orphaned_leases({kept.lease_id, "elsewhere"}) == 1
    assert sorted(
        entry["session_id"]
        for entry in active_sessions.active_session_registry_snapshot()
    ) == ["kept", "other"]
    assert orphan is not None


def test_reclaimed_slot_can_be_acquired(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    lease, message = active_sessions.try_acquire_active_session(
        session_id="new-session",
        surface="cli",
        config={"max_concurrent_sessions": 1},
    )

    assert message is None
    assert lease is not None
    assert [entry["session_id"] for entry in active_sessions.active_session_registry_snapshot()] == [
        "new-session"
    ]
    lease.release()


def _registry_entries_for(home: Path) -> list[dict]:
    """Read the pruned registry for a specific profile home directly."""
    return active_sessions._prune_dead(
        active_sessions._read_entries(home / "runtime" / "active_sessions.json")
    )


def test_lease_pins_registry_paths_at_claim_time(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))

    lease, message = active_sessions.try_acquire_active_session(
        session_id="session-1",
        surface="tui",
        config={"max_concurrent_sessions": 2},
    )

    assert message is None
    assert lease is not None
    expected_state = home / "runtime" / "active_sessions.json"
    expected_lock = home / "runtime" / "active_sessions.lock"
    assert lease.state_path == str(expected_state)
    assert lease.lock_path == str(expected_lock)
    assert lease.registry_state_path() == expected_state
    assert lease.registry_lock_path() == expected_lock
    lease.release()


def test_lease_release_uses_claim_time_registry_under_home_override(tmp_path, monkeypatch):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    claim_home = tmp_path / "profile-a"
    other_home = tmp_path / "profile-b"
    monkeypatch.setenv("HERMES_HOME", str(claim_home))

    # Claim under profile A's ambient home.
    lease, message = active_sessions.try_acquire_active_session(
        session_id="session-1",
        surface="tui",
        config={"max_concurrent_sessions": 1},
    )
    assert message is None
    assert lease is not None
    assert len(_registry_entries_for(claim_home)) == 1

    # Release while a DIFFERENT profile home is the ambient override (as happens
    # when a per-turn set_hermes_home_override for a resumed remote profile is
    # active at teardown). The lease must still touch profile A's registry.
    token = set_hermes_home_override(str(other_home))
    try:
        lease.release()
    finally:
        reset_hermes_home_override(token)

    assert _registry_entries_for(claim_home) == []
    # Profile B's registry was never created/touched by the release.
    assert not (other_home / "runtime" / "active_sessions.json").exists()


def test_lease_transfer_uses_claim_time_registry_under_home_override(tmp_path, monkeypatch):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    claim_home = tmp_path / "profile-a"
    other_home = tmp_path / "profile-b"
    monkeypatch.setenv("HERMES_HOME", str(claim_home))

    lease, message = active_sessions.try_acquire_active_session(
        session_id="session-old",
        surface="tui",
        config={"max_concurrent_sessions": 1},
        metadata={"live_session_id": "ui-1"},
    )
    assert message is None
    assert lease is not None

    token = set_hermes_home_override(str(other_home))
    try:
        transferred = active_sessions.transfer_active_session(
            lease,
            session_id="session-new",
            metadata={"live_session_id": "ui-1"},
        )
    finally:
        reset_hermes_home_override(token)

    assert transferred is True
    assert lease.session_id == "session-new"
    entries = _registry_entries_for(claim_home)
    assert [entry["session_id"] for entry in entries] == ["session-new"]
    # The transfer stayed in the claim-time registry; no stray slot in B.
    assert not (other_home / "runtime" / "active_sessions.json").exists()
    lease.release()
    assert _registry_entries_for(claim_home) == []


def test_cap_is_isolated_per_profile_registry(tmp_path, monkeypatch):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home_a = tmp_path / "profile-a"
    home_b = tmp_path / "profile-b"
    monkeypatch.setenv("HERMES_HOME", str(home_a))
    cfg = {"max_concurrent_sessions": 1}

    # Fill profile A's single slot.
    lease_a, message_a = active_sessions.try_acquire_active_session(
        session_id="a-1", surface="tui", config=cfg
    )
    assert message_a is None and lease_a is not None

    # A concurrent session under profile B has its own isolated registry, so it
    # is NOT blocked by profile A being full.
    token = set_hermes_home_override(str(home_b))
    try:
        lease_b, message_b = active_sessions.try_acquire_active_session(
            session_id="b-1", surface="tui", config=cfg
        )
    finally:
        reset_hermes_home_override(token)
    assert message_b is None
    assert lease_b is not None

    # A second claim under profile A is still blocked by A's cap.
    blocked, blocked_message = active_sessions.try_acquire_active_session(
        session_id="a-2", surface="tui", config=cfg
    )
    assert blocked is None
    assert blocked_message is not None

    assert len(_registry_entries_for(home_a)) == 1
    assert len(_registry_entries_for(home_b)) == 1
    lease_a.release()
    lease_b.release()


def test_transition_reservation_is_atomic_and_released_without_transfer(
    tmp_path, monkeypatch
):
    """Detach admission occupies the last slot only for the transition.

    The reservation is deliberately not transferred to the idle replacement:
    after commit/release, a real turn can acquire that capacity normally.
    """
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    cfg = {"max_concurrent_sessions": 1}

    reservation, message = active_sessions.try_reserve_active_session_transition(
        transition="detach_turn",
        session_id="detach:source",
        surface="desktop",
        config=cfg,
        metadata={"source_session_id": "source"},
    )
    assert message is None
    assert reservation is not None
    entries = active_sessions.active_session_registry_snapshot()
    assert len(entries) == 1
    assert entries[0]["metadata"] == {
        "kind": "transition_reservation",
        "source_session_id": "source",
        "transition": "detach_turn",
    }

    blocked, blocked_message = active_sessions.try_acquire_active_session(
        session_id="concurrent-turn",
        surface="cli",
        config=cfg,
    )
    assert blocked is None
    assert "active session limit (1/1)" in blocked_message

    reservation.release()
    replacement_turn, replacement_message = active_sessions.try_acquire_active_session(
        session_id="replacement-first-turn",
        surface="desktop",
        config=cfg,
    )
    assert replacement_message is None
    assert replacement_turn is not None
    assert [
        entry["session_id"]
        for entry in active_sessions.active_session_registry_snapshot()
    ] == ["replacement-first-turn"]
    replacement_turn.release()
