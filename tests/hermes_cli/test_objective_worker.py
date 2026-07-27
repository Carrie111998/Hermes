from types import SimpleNamespace
import os
import signal
import time

from hermes_cli import objective_worker, objectives_db, operational_control


def test_worker_schema_read_preserves_active_transaction(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    objective_worker.ensure_schema(conn)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO objective_workers "
        "(id, organization_id, role, pid, process_nonce, status, started_at, heartbeat_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("worker_txn", "__unscoped__", "test", 1, "nonce", "running", 1, 1),
    )
    assert objective_worker.worker_health(conn)[0]["id"] == "worker_txn"
    assert conn.in_transaction is True
    conn.rollback()


def test_stale_worker_reconciliation_persists_stop_state(tmp_path):
    conn = objectives_db.connect(tmp_path / "authority.db")
    worker_id = objective_worker.register_worker(conn, worker_id="stale-worker")
    conn.execute(
        "UPDATE objective_workers SET heartbeat_at=1 WHERE id=?", (worker_id,)
    )
    conn.commit()
    assert objective_worker.reconcile_stale_workers(conn, stale_after_seconds=10) == [
        worker_id
    ]
    row = conn.execute(
        "SELECT status,stop_reason FROM objective_workers WHERE id=?", (worker_id,)
    ).fetchone()
    assert tuple(row) == ("stale", "heartbeat_stale")
    intervention = conn.execute(
        "SELECT category FROM intervention_queue WHERE dedupe_key=?",
        ("objective-worker-stale:stale-worker",),
    ).fetchone()
    assert intervention["category"] == "objective_worker_stale"


def test_supervised_worker_persists_cycle_and_graceful_stop(tmp_path):
    path = tmp_path / "authority.db"
    calls = []

    def tick():
        calls.append(True)
        return SimpleNamespace(status="idle")

    assert objective_worker.run_forever(
        db_path=path, interval_seconds=0.001, tick=tick, max_cycles=2
    ) == 0
    conn = objectives_db.connect(path)
    workers = objective_worker.worker_health(conn)
    assert len(calls) == 2
    assert len(workers) == 1
    assert workers[0]["status"] == "stopped"
    assert workers[0]["last_cycle_status"] == "idle"
    assert workers[0]["stopped_at"] is not None
    assert workers[0]["stop_reason"] == "supervisor_exit"


def test_worker_exits_when_autonomy_is_paused(tmp_path):
    path = tmp_path / "authority.db"
    calls = []

    def tick():
        calls.append(True)
        return SimpleNamespace(status="paused")

    assert objective_worker.run_forever(
        db_path=path, interval_seconds=900, tick=tick, max_cycles=10
    ) == 0
    conn = objectives_db.connect(path)
    worker = objective_worker.worker_health(conn)[0]
    assert calls == [True]
    assert worker["status"] == "stopped"
    assert worker["last_cycle_status"] == "paused"
    assert worker["stop_reason"] == "autonomy_paused"


def test_worker_exits_when_autonomy_is_disabled(tmp_path):
    path = tmp_path / "authority.db"
    calls = []

    def tick():
        calls.append(True)
        return SimpleNamespace(status="disabled")

    assert objective_worker.run_forever(
        db_path=path, interval_seconds=900, tick=tick, max_cycles=10
    ) == 0
    conn = objectives_db.connect(path)
    worker = objective_worker.worker_health(conn)[0]
    assert calls == [True]
    assert worker["status"] == "stopped"
    assert worker["last_cycle_status"] == "disabled"
    assert worker["stop_reason"] == "runtime_blocked:disabled"


def test_worker_exits_when_autonomy_is_revoked_during_failure(tmp_path):
    path = tmp_path / "authority.db"
    calls = []

    def tick():
        calls.append(True)
        conn = objectives_db.connect(path)
        try:
            operational_control.set_autonomy_mode(
                conn, mode="paused", actor="human:advisor", reason="emergency stop"
            )
        finally:
            conn.close()
        raise RuntimeError("provider call interrupted")

    assert objective_worker.run_forever(
        db_path=path, interval_seconds=900, tick=tick, max_cycles=10
    ) == 0
    conn = objectives_db.connect(path)
    worker = objective_worker.worker_health(conn)[0]
    assert calls == [True]
    assert worker["status"] == "stopped"
    assert worker["stop_reason"] == "autonomy_paused"
    assert worker["consecutive_failures"] == 0


def test_stale_running_worker_is_reported_unhealthy(tmp_path, monkeypatch):
    conn = objectives_db.connect(tmp_path / "authority.db")
    worker_id = objective_worker.register_worker(conn, worker_id="worker_test")
    conn.execute(
        "UPDATE objective_workers SET heartbeat_at=1 WHERE id=?", (worker_id,)
    )
    conn.commit()
    health = objective_worker.worker_health(conn, stale_after_seconds=10)
    assert health[0]["healthy"] is False
    assert health[0]["effective_status"] == "stale"


def test_worker_heartbeat_continues_during_blocking_cycle(tmp_path):
    path = tmp_path / "authority.db"
    conn = objectives_db.connect(path)
    worker_id = objective_worker.register_worker(conn, worker_id="worker_busy")
    conn.execute(
        "UPDATE objective_workers SET heartbeat_at=1 WHERE id=?", (worker_id,)
    )
    conn.commit()

    with objective_worker.WorkerHeartbeatKeeper(
        conn, worker_id, interval_seconds=0.05
    ) as keeper:
        time.sleep(0.15)
        keeper.assert_healthy()
        row = conn.execute(
            "SELECT heartbeat_at FROM objective_workers WHERE id=?", (worker_id,)
        ).fetchone()
        assert int(row["heartbeat_at"]) > 1


def test_worker_stops_after_its_lease_is_revoked_during_tick(tmp_path):
    path = tmp_path / "authority.db"
    calls = []

    def tick():
        calls.append(True)
        conn = objectives_db.connect(path)
        try:
            conn.execute(
                "UPDATE objective_workers SET status='stale', stop_reason=? "
                "WHERE status='running'",
                ("heartbeat_stale",),
            )
            conn.commit()
        finally:
            conn.close()
        return SimpleNamespace(status="idle")

    assert objective_worker.run_forever(
        db_path=path, interval_seconds=0.01, tick=tick, max_cycles=10
    ) == 1
    conn = objectives_db.connect(path)
    worker = objective_worker.worker_health(conn)[0]
    assert calls == [True]
    assert worker["status"] == "stale"
    assert worker["stop_reason"] == "heartbeat_stale"


def test_repeated_systemic_failures_open_circuit_and_exit_nonzero(
    tmp_path, monkeypatch
):
    path = tmp_path / "authority.db"
    calls = []
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "agentic": {
                "security": {"circuit_breaker_failure_threshold": 2},
                "retry_policy": {"max_backoff_seconds": 1},
            }
        },
    )

    def tick():
        calls.append(True)
        raise RuntimeError("authority database unavailable")

    assert objective_worker.run_forever(
        db_path=path,
        interval_seconds=0.01,
        tick=tick,
        max_cycles=10,
    ) == 1

    conn = objectives_db.connect(path)
    workers = objective_worker.worker_health(conn)
    assert len(calls) == 2
    assert workers[0]["status"] == "circuit_open"
    assert workers[0]["effective_status"] == "circuit_open"
    assert workers[0]["consecutive_failures"] == 2
    interventions = operational_control.list_interventions(conn)
    assert len(interventions) == 1
    assert interventions[0]["category"] == "objective_runtime_unhealthy"
    assert interventions[0]["context"]["last_error"] == (
        "authority database unavailable"
    )


def test_successful_cycle_resets_consecutive_worker_failures(
    tmp_path, monkeypatch
):
    path = tmp_path / "authority.db"
    outcomes = iter(
        [
            RuntimeError("temporary failure"),
            SimpleNamespace(status="idle"),
            RuntimeError("temporary failure"),
        ]
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "agentic": {
                "security": {"circuit_breaker_failure_threshold": 2},
                "retry_policy": {"max_backoff_seconds": 1},
            }
        },
    )
    def tick():
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    assert objective_worker.run_forever(
        db_path=path,
        interval_seconds=0.01,
        tick=tick,
        max_cycles=3,
    ) == 0

    conn = objectives_db.connect(path)
    worker = objective_worker.worker_health(conn)[0]
    assert worker["status"] == "stopped"
    assert worker["consecutive_failures"] == 1
    assert operational_control.list_interventions(conn) == []


def test_sigterm_interrupts_supervisor_backoff_and_persists_stop(tmp_path):
    path = tmp_path / "authority.db"
    calls = []

    def tick():
        calls.append(True)
        os.kill(os.getpid(), signal.SIGTERM)
        return SimpleNamespace(status="idle")

    started = time.monotonic()
    assert objective_worker.run_forever(
        db_path=path,
        interval_seconds=900,
        tick=tick,
    ) == 0
    elapsed = time.monotonic() - started

    conn = objectives_db.connect(path)
    workers = objective_worker.worker_health(conn)
    assert calls == [True]
    assert elapsed < 2
    assert workers[0]["status"] == "stopped"
    assert workers[0]["last_cycle_status"] == "idle"
    assert workers[0]["stop_reason"] == "signal:SIGTERM"
