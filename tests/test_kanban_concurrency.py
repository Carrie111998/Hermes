"""CS-01a SQLite concurrency and atomic cost-tracking acceptance tests."""

from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.cost import config as cost_config
from hermes_cli.cost import ledger
from hermes_cli.programme import gate as programme_gate
from hermes_cli.programme import init as programme_init
from hermes_cli import sqlite_util


@pytest.fixture
def concurrency_env(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    halt_path = tmp_path / "halt"
    monkeypatch.setattr(ledger, "DB_PATH", db_path)
    monkeypatch.setattr(programme_init, "DB_PATH", db_path)
    monkeypatch.setattr(programme_gate, "HALT_SIGNAL_PATH", halt_path)

    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    conn = kb.connect(db_path)
    conn.close()
    programme_init.migrate(db_path)
    ledger.migrate(db_path)
    yield db_path
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))


def _seed_aud(db_path, amount: float) -> None:
    conn = ledger.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO cost_ledger (
                ts, task_id, lane, vendor, model_slug, escalation,
                usd_amount, aud_amount, fx_rate, surcharge_applied
            ) VALUES (?, 'seed', 'platform', 'anthropic', 'seed/model',
                      0, ?, ?, 1.0, 0.0)
            """,
            (ledger.utc_now(), amount, amount),
        )
    finally:
        conn.close()


def _record_half_dollar(index: int):
    return ledger.record_call(
        task_id=f"cost-{index}",
        lane="platform",
        vendor="anthropic",
        model_slug="anthropic/claude-opus-5",
        attempt_number=1,
        rung_id=None,
        escalation=False,
        input_tokens=10,
        output_tokens=5,
        cached_input_tokens=0,
        usd_amount=0.50,
        latency_ms=10,
        request_id=f"concurrency-{index}",
        raw_response_meta=None,
    )


def _set_one_dollar_global_cap(monkeypatch) -> None:
    monkeypatch.setattr(cost_config, "FX_RATE", 1.0)
    monkeypatch.setattr(cost_config, "OPENROUTER_SURCHARGE", 0.0)
    monkeypatch.setattr(cost_config, "GLOBAL_DAILY_CAP_AUD", 1.0)
    monkeypatch.setattr(cost_config, "PER_TASK_CAP_AUD", 100.0)
    monkeypatch.setattr(
        cost_config,
        "LANE_DAILY_CAPS_AUD",
        {lane: 100.0 for lane in cost_config.VALID_LANES},
    )
    monkeypatch.setattr(cost_config, "ESCALATION_DAILY_CAP_AUD", 100.0)


def test_connections_use_wal(concurrency_env):
    connections = [
        kb.connect(concurrency_env),
        programme_init.connect(concurrency_env),
        ledger.connect(concurrency_env),
    ]
    try:
        assert {
            conn.execute("PRAGMA journal_mode").fetchone()[0]
            for conn in connections
        } == {"wal"}
    finally:
        for conn in connections:
            conn.close()


def test_connections_have_at_least_five_second_busy_timeout(concurrency_env):
    kanban = kb.connect(concurrency_env)
    programme = programme_init.connect(concurrency_env)
    cost = ledger.connect(concurrency_env)
    try:
        assert kanban.execute("PRAGMA busy_timeout").fetchone()[0] >= 5_000
        assert programme.execute("PRAGMA busy_timeout").fetchone()[0] == 5_000
        assert cost.execute("PRAGMA busy_timeout").fetchone()[0] == 5_000
    finally:
        kanban.close()
        programme.close()
        cost.close()


def test_connections_enforce_foreign_keys(concurrency_env):
    connections = [
        kb.connect(concurrency_env),
        programme_init.connect(concurrency_env),
        ledger.connect(concurrency_env),
    ]
    try:
        assert [
            conn.execute("PRAGMA foreign_keys").fetchone()[0]
            for conn in connections
        ] == [1, 1, 1]
    finally:
        for conn in connections:
            conn.close()


def test_eight_thread_claim_has_one_winner_and_no_stranded_claiming(
    concurrency_env,
):
    creator = kb.connect(concurrency_env)
    try:
        task_id = kb.create_task(creator, title="one winner")
    finally:
        creator.close()

    barrier = threading.Barrier(8)

    def attempt(index: int):
        conn = kb.connect(concurrency_env)
        try:
            barrier.wait(timeout=5)
            return kb.claim_task(conn, task_id, claimer=f"worker-{index}")
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))

    assert sum(result is not None for result in results) == 1
    conn = kb.connect(concurrency_env)
    try:
        row = conn.execute(
            "SELECT status, claim_lock FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        claiming = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 'claiming'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert row["status"] == "running"
    assert row["claim_lock"] is not None
    assert claiming == 0


def test_concurrent_cost_writes_persist_and_send_task_advisories(
    concurrency_env,
    monkeypatch,
):
    messages = []
    monkeypatch.setattr(
        "hermes_cli.cost.gate_integration.telegram_alert.send_bridge_alert",
        lambda message: messages.append(message),
    )
    _set_one_dollar_global_cap(monkeypatch)
    _seed_aud(concurrency_env, 0.99)

    barrier = threading.Barrier(4)

    def write(index: int):
        barrier.wait(timeout=5)
        return _record_half_dollar(index)

    with ThreadPoolExecutor(max_workers=4) as pool:
        entries = list(pool.map(write, range(4)))

    conn = ledger.connect(concurrency_env)
    try:
        persisted = conn.execute(
            "SELECT COUNT(*) FROM cost_ledger "
            "WHERE request_id LIKE 'concurrency-%'"
        ).fetchone()[0]
        total = conn.execute(
            "SELECT SUM(aud_amount) FROM cost_ledger"
        ).fetchone()[0]
        transitions = conn.execute(
            """
            SELECT COUNT(*)
              FROM programme_state_log
             WHERE state = 'PAUSED' AND changed_by = 'cost_gate'
            """
        ).fetchone()[0]
    finally:
        conn.close()

    assert persisted == 4
    assert total == pytest.approx(2.99)
    assert all(entry.breached_cap == "global" for entry in entries)
    assert not any(entry.transitioned_to_paused for entry in entries)
    assert transitions == 0
    assert len(messages) == 4
    assert all("advisory_only: yes" in message for message in messages)
    assert programme_gate.get_state().state == "RUNNING"


def test_advisory_cap_check_waits_for_external_write_lock(
    concurrency_env,
    monkeypatch,
):
    monkeypatch.setattr(
        "hermes_cli.cost.gate_integration.telegram_alert.send_bridge_alert",
        lambda _message: None,
    )
    _set_one_dollar_global_cap(monkeypatch)
    _seed_aud(concurrency_env, 0.99)

    lock_conn = sqlite3.connect(
        concurrency_env,
        timeout=0,
        isolation_level=None,
    )
    lock_conn.execute("BEGIN IMMEDIATE")
    result = {}

    def write():
        result["entry"] = _record_half_dollar(20)

    thread = threading.Thread(target=write)
    thread.start()
    time.sleep(0.15)
    assert thread.is_alive()
    assert programme_gate.get_state().state == "RUNNING"

    lock_conn.execute("COMMIT")
    lock_conn.close()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert result["entry"].breached_cap == "global"
    assert result["entry"].transitioned_to_paused is False
    assert programme_gate.get_state().state == "RUNNING"


def test_draining_counts_claiming_as_inflight(concurrency_env):
    conn = kb.connect(concurrency_env)
    try:
        task_id = kb.create_task(conn, title="transient claim")
        conn.execute(
            "UPDATE tasks SET status = 'claiming' WHERE id = ?",
            (task_id,),
        )
    finally:
        conn.close()

    programme_gate.set_state("DRAINING", "finish claims", "test")
    assert programme_gate.inflight_count() == 1
    assert programme_gate.check_drain().state == "DRAINING"


def test_busy_retry_raises_twice_then_succeeds(
    concurrency_env,
    monkeypatch,
):
    real = ledger.connect(concurrency_env)

    class TwiceBusy:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.begin_attempts = 0

        def execute(self, sql, parameters=()):
            if sql == "BEGIN IMMEDIATE":
                self.begin_attempts += 1
                if self.begin_attempts <= 2:
                    raise sqlite3.OperationalError("database is locked")
            return self.wrapped.execute(sql, parameters)

        def close(self):
            self.wrapped.close()

    flaky = TwiceBusy(real)
    monkeypatch.setattr(ledger, "connect", lambda *_args, **_kwargs: flaky)
    monkeypatch.setattr(sqlite_util.time, "sleep", lambda _seconds: None)

    entry = _record_half_dollar(30)
    assert entry.id > 0
    assert flaky.begin_attempts == 3
